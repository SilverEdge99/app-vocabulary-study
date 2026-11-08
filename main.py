# -*- coding: utf-8 -*-
"""
本地离线背单词系统 —— 后端（第一版：可运行）

架构
    FastAPI + SQLite（WAL），只监听 127.0.0.1，不对外网暴露。
    前端为同目录 index.html，由本文件托管。

本版范围（第一版）
    ✅ 建表 / 词书 CRUD / 单词 CRUD / CSV 导入 / JSON·CSV 导出
    ✅ 默写模式 A（看中文写英文）与模式 B（看英文写中文）
    ✅ 服务端会话（刷新不丢进度）/ 判分 / 结束报告 / 错题列表
    ✅ 到期复习、错题复习、整本复习
    ✅ 统计：日/周/月 新词·复习数、正确率、连续打卡、每日新词计划完成率
    ⏳ 下一版：FSRS-4.5 真实调度（当前为占位阶梯调度，见 _schedule_simple）
    ⏳ 下一版：PDF 解析 + 可选 Ollama 生成释义

运行
    python main.py                 # 启动并自动打开浏览器
    python main.py --no-browser    # 启动，不打开浏览器
    python main.py --port 8017
    python main.py --selftest      # 跑一遍内置自检（不启动服务）
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# 允许把依赖装在本地 ./pylibs（见 README：pip install -r requirements.txt 或离线引导）
_HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(_HERE, "pylibs")):
    sys.path.insert(0, os.path.join(_HERE, "pylibs"))

# Windows 控制台默认不是 UTF-8，避免中文日志/自检输出时抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

# ============================================================================
# 基础常量
# ============================================================================

DB_PATH = os.path.join(_HERE, "vocab.db")
INDEX_PATH = os.path.join(_HERE, "index.html")
HOST = "127.0.0.1"
LAN_MODE = False          # True = 绑定 0.0.0.0，手机/局域网可访问（需要访问码）
ACCESS_TOKEN = ""         # 局域网模式的访问码
PORT = 8017
LOCAL_TZ = timezone(timedelta(hours=8))  # Asia/Shanghai，无夏令时

MAX_WORDS_PER_SESSION = 200
SESSION_TTL_SECONDS = 30 * 60

DEFAULT_SETTINGS = {
    "daily_new_default": "20",
    "theme": "auto",
    "auto_next": "1",
    "ai_model": "qwen2.5:7b",
}

# ============================================================================
# 数据库
# ============================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS word_books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    word TEXT NOT NULL,
    meaning TEXT NOT NULL,
    is_mastered INTEGER DEFAULT 0,
    is_error INTEGER DEFAULT 0,
    stability REAL DEFAULT 0,
    difficulty REAL DEFAULT 0,
    reps INTEGER DEFAULT 0,
    lapses INTEGER DEFAULT 0,
    next_review_at DATETIME NULL,
    last_review_at DATETIME NULL,
    FOREIGN KEY (book_id) REFERENCES word_books(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS review_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    word_id INTEGER NOT NULL,
    review_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    score INTEGER,
    is_correct INTEGER,
    FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
);
"""

# 索引与设置表（不改动上面三张表的结构）
EXTRA_SQL = """
CREATE INDEX IF NOT EXISTS idx_words_book      ON words(book_id);
CREATE INDEX IF NOT EXISTS idx_words_due       ON words(next_review_at);
CREATE INDEX IF NOT EXISTS idx_logs_word_time  ON review_logs(word_id, review_time);
CREATE INDEX IF NOT EXISTS idx_logs_time       ON review_logs(review_time);
CREATE UNIQUE INDEX IF NOT EXISTS uq_words_book_word ON words(book_id, word);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- 用法 / 例句：单独一张表，不污染给定的三张表结构。
-- phrases / examples 都是「一行一条」的纯文本，前端按行渲染。
CREATE TABLE IF NOT EXISTS word_details (
    word_id INTEGER PRIMARY KEY,
    phrases TEXT,
    examples TEXT,
    update_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
);
-- 「当日记忆」里生成的场景故事 / 对话，留档方便回看
CREATE TABLE IF NOT EXISTS daily_writings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    style TEXT NOT NULL,
    target_len INTEGER,
    content TEXT NOT NULL,
    word_list TEXT,
    create_time DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (book_id) REFERENCES word_books(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_writings_book_day ON daily_writings(book_id, day);
"""


@contextmanager
def db():
    """每次请求开一个连接；PRAGMA 必须逐连接设置（WAL 是持久属性，其余不是）。"""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """启动时自动建表 + 索引 + 设置表，并开启 WAL。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_SQL)
        conn.executescript(EXTRA_SQL)
        _migrate_schema(conn)
        conn.commit()
    finally:
        conn.close()
    with db() as c:
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO app_settings(key, value) VALUES(?,?)", (k, v))


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """给已有数据库补新列（重复执行安全）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(word_details)")}
    if cols and "pos" not in cols:
        conn.execute("ALTER TABLE word_details ADD COLUMN pos TEXT")


# ---------------------------------------------------------------- 时间工具

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_db(dt: datetime) -> str:
    """统一存 UTC 文本 'YYYY-MM-DD HH:MM:SS'。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def from_db(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def local_day_start(dt: datetime) -> datetime:
    """返回该时刻所在「本地日」的 00:00（转成 UTC）。"""
    local = dt.astimezone(LOCAL_TZ)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc)


# ============================================================================
# 判分
# ============================================================================
# 规则
#   英文：去首尾空白/标点、忽略大小写、接受 , ; / 分隔的多个答案、连字符可省
#   中文：全角转半角、去标点空白；库里的释义按 ; ； , ， 、 / 拆成义项，
#        用户答案完整包含任一长度>=2 的义项即判对（避免「的」这类单字误判）

_PUNCT_EN = " \t\r\n.,!?;:'\"()[]{}-–—…、，。"
_SPLIT_MEANING = re.compile(r"[;；,，、/|]+")


def _norm_text(s: str) -> str:
    s = (s or "").strip()
    table = str.maketrans({"；": ";", "，": ",", "、": ",", "。": ".", "：": ":", "／": "/",
                           "（": "(", "）": ")", "“": '"', "”": '"', "‘": "'", "’": "'",
                           "　": " "})
    return s.translate(table)


def _norm_en(s: str) -> str:
    s = _norm_text(s).lower()
    s = s.strip(_PUNCT_EN)
    s = re.sub(r"\s+", " ", s)
    return s.replace("-", "")


def judge_en(answer: str, word: str) -> bool:
    """模式 A：用户输入英文单词。"""
    a = _norm_en(answer)
    if not a:
        return False
    candidates = {_norm_en(x) for x in re.split(r"[,;/]", word) if _norm_en(x)}
    return a in candidates


def judge_zh(answer: str, meaning: str) -> bool:
    """模式 B：用户输入中文释义（宽松包含匹配）。"""
    a = _norm_text(answer)
    a = re.sub(r"[\s\t\r\n]+", "", a)
    a = a.translate(str.maketrans("", "", _PUNCT_EN.replace(" ", "")))
    if not a:
        return False
    senses = []
    for part in _SPLIT_MEANING.split(_norm_text(meaning)):
        p = re.sub(r"[\s\t\r\n]+", "", part)
        p = p.translate(str.maketrans("", "", _PUNCT_EN.replace(" ", "")))
        p = re.sub(r"^(n|v|adj|adv|prep|pron|conj|num|art|int)\.", "", p)
        if len(p) >= 2:
            senses.append(p)
    if not senses:
        return False
    return any(s in a or a in s for s in senses)


def judge(mode: str, answer: str, word_row: sqlite3.Row | dict) -> bool:
    if mode == "A":
        return judge_en(answer, word_row["word"])
    return judge_zh(answer, word_row["meaning"])


# ============================================================================
# 调度（占位实现）
# ============================================================================
# 第一版用「阶梯间隔」：答对一次升一级，答错回到 10 分钟。
# 第二版会整体替换成 FSRS-4.5（stability/difficulty 两列已经预留好，
# 这里只是把它们当作「当前间隔天数」和「难度」写进去，便于将来平滑迁移）。

LEVELS = [1, 2, 4, 7, 15, 30, 60, 120]   # 单位：天


def _schedule_simple(is_correct: bool, reps: int, lapses: int,
                     stability: float, difficulty: float
                     ) -> dict[str, Any]:
    """返回需要写回 words 表的字段（不含 review_logs）。"""
    base = now_utc()
    if is_correct:
        reps_new = reps + 1
        interval = LEVELS[min(reps_new, len(LEVELS)) - 1]
        # 难度随答对缓慢下降；稳定度在这一版里直接用间隔天数值
        diff = max(1.0, difficulty - 0.2) if difficulty else 5.0
        mastered = 1 if interval >= 30 and reps_new >= 3 else 0
        return {
            "reps": reps_new,
            "lapses": lapses,
            "stability": float(interval),
            "difficulty": round(diff, 3),
            "is_mastered": mastered,
            "next_review_at": to_db(base + timedelta(days=interval)),
            "last_review_at": to_db(base),
        }
    return {
        "reps": reps + 1,
        "lapses": lapses + 1,
        "stability": 1.0,
        "difficulty": round(min(10.0, (difficulty or 5.0) + 0.3), 3),
        "is_mastered": 0,
        "next_review_at": to_db(base + timedelta(minutes=10)),
        "last_review_at": to_db(base),
    }


# ============================================================================
# 会话（服务端内存，刷新不丢进度）
# ============================================================================

SESSIONS: dict[str, dict[str, Any]] = {}
_SESS_LOCK = threading.Lock()


def _sweep_sessions_locked() -> None:
    """清理过期会话。调用方必须已持有 _SESS_LOCK（threading.Lock 不可重入）。"""
    cutoff = time.time() - SESSION_TTL_SECONDS
    for sid in [k for k, v in SESSIONS.items() if v["touched"] < cutoff]:
        SESSIONS.pop(sid, None)


def _public_session(sess: dict[str, Any]) -> dict[str, Any]:
    """给前端的会话视图：不泄露答案与释义。"""
    items, answered = [], set(sess["answered"].keys())
    with db() as c:
        for wid in sess["queue"]:
            row = c.execute("SELECT id, word, meaning FROM words WHERE id=?", (wid,)).fetchone()
            if not row:
                continue
            items.append({
                "word_id": wid,
                "prompt": row["meaning"] if sess["mode"] == "A" else row["word"],
                "answered": wid in answered,
            })
    return {
        "session_id": sess["id"],
        "book_id": sess["book_id"],
        "mode": sess["mode"],
        "scope": sess["scope"],
        "total": len(items),
        "done": len(sess["answered"]),
        "items": items,
        "results": sess["results"],
        "finished": sess["finished"],
        "started_at": sess["started_at"],
    }


# ============================================================================
# FastAPI
# ============================================================================

app = FastAPI(title="Vocab Trainer (local)", docs_url=None, redoc_url=None)


PUBLIC_PATHS = ("/manifest.webmanifest", "/icon-180.png", "/icon-512.png", "/favicon.ico")


@app.middleware("http")
async def guard_origin(request: Request, call_next):
    """本机模式只允许 localhost；局域网模式要求访问码，并放行局域网来源。"""
    path = request.url.path
    if LAN_MODE and path.startswith("/api/"):
        tok = request.query_params.get("k", "") or request.cookies.get("vocab_token", "")
        if tok != ACCESS_TOKEN:
            return JSONResponse({"detail": "需要访问码：先打开首页输入"}, status_code=401)
    if path in PUBLIC_PATHS:
        return await call_next(request)
    origin = request.headers.get("origin") or ""
    referer = request.headers.get("referer") or ""
    # 注意：Referer 会带路径（http://host:port/page），所以结尾要允许 /path
    allowed = r"^https?://(127\.0\.0\.1|localhost)(:\d+)?(/.*)?$"
    if LAN_MODE:
        allowed = r"^https?://[\w.\-]+(:\d+)?(/.*)?$"
    if origin and not re.match(allowed, origin):
        return JSONResponse({"detail": "拒绝非本机来源的请求"}, status_code=403)
    if not origin and referer and not re.match(allowed, referer):
        return JSONResponse({"detail": "拒绝非本机来源的请求"}, status_code=403)
    return await call_next(request)


def _load_token() -> str:
    """局域网访问码：存 app_settings.lan_token，没有就生成 8 位。"""
    with db() as c:
        row = c.execute("SELECT value FROM app_settings WHERE key='lan_token'").fetchone()
        if row and row["value"]:
            return row["value"]
        tok = uuid.uuid4().hex[:8]
        c.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('lan_token', ?)", (tok,))
        return tok


def _lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vocab | 访问码</title>
<style>
body{font-family:system-ui,"Microsoft YaHei UI",sans-serif;background:#F7F5F1;color:#1A1917;
margin:0;min-height:100vh;display:grid;place-items:center}
form{background:#fff;border:1px solid #E7E2DA;border-radius:14px;padding:26px 22px;
width:min(320px,86vw);box-shadow:0 8px 24px rgba(0,0,0,.06)}
h1{font-size:17px;margin:0 0 8px}p{color:#6F6A62;font-size:13px;margin:0 0 16px;line-height:1.6}
input{width:100%;box-sizing:border-box;font-size:16px;padding:11px 12px;
border:1.5px solid #E7E2DA;border-radius:10px;letter-spacing:2px}
button{margin-top:12px;width:100%;padding:11px;border:0;border-radius:10px;
background:#B4552D;color:#fff;font-size:15px}
.err{color:#C0392B;font-size:12.5px;margin-top:10px}
</style></head><body>
<form method="get" action="/">
<h1>Vocab 背单词</h1>
<p>局域网访问：请输入电脑窗口里显示的<br><b>8 位访问码</b></p>
<input name="k" autocomplete="off" autocapitalize="off" placeholder="如 3f9a1c2b" autofocus>
<button type="submit">进入</button>__ERR__
</form></body></html>"""


# ------------------------------------------------------------------ 页面

@app.get("/")
def index(request: Request):
    if not os.path.exists(INDEX_PATH):
        raise HTTPException(500, "index.html 缺失")
    if LAN_MODE:
        k = request.query_params.get("k", "")
        if k and k == ACCESS_TOKEN:
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie("vocab_token", ACCESS_TOKEN, max_age=90 * 24 * 3600,
                            httponly=True, samesite="lax")
            return resp
        if request.cookies.get("vocab_token", "") != ACCESS_TOKEN:
            err = '<div class="err">访问码不对，再试一次</div>' if k else ""
            return HTMLResponse(LOGIN_HTML.replace("__ERR__", err), status_code=401 if k else 200)
    # 关掉缓存：改完页面刷新就能看到，不用一直 Ctrl+F5
    return FileResponse(INDEX_PATH, media_type="text/html; charset=utf-8",
                        headers={"Cache-Control": "no-store, max-age=0"})


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "Vocab 背单词",
        "short_name": "Vocab",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#F7F5F1",
        "theme_color": "#B4552D",
        "icons": [
            {"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    }, media_type="application/manifest+json")


@app.get("/icon-180.png")
@app.get("/icon-512.png")
def icon(request: Request):
    p = os.path.join(_HERE, request.url.path.lstrip("/"))
    if not os.path.exists(p):
        raise HTTPException(404, "icon 缺失")
    return FileResponse(p, media_type="image/png")


@app.get("/api/health")
def health():
    with db() as c:
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    return {"ok": True, "journal_mode": mode, "db": os.path.basename(DB_PATH), "version": 1}


# ------------------------------------------------------------------ 设置

class SettingsIn(BaseModel):
    values: dict[str, str]


@app.get("/api/settings")
def get_settings():
    with db() as c:
        rows = c.execute("SELECT key, value FROM app_settings").fetchall()
    data = {r["key"]: r["value"] for r in rows}
    for k, v in DEFAULT_SETTINGS.items():
        data.setdefault(k, v)
    return data


@app.put("/api/settings")
def put_settings(body: SettingsIn):
    with db() as c:
        for k, v in body.values.items():
            c.execute("INSERT INTO app_settings(key, value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
    return get_settings()


def _get_plan(book_id: int) -> int:
    with db() as c:
        row = c.execute("SELECT value FROM app_settings WHERE key=?", (f"book.{book_id}.daily_new",)).fetchone()
        if row:
            try:
                return max(0, int(row["value"]))
            except ValueError:
                pass
        row = c.execute("SELECT value FROM app_settings WHERE key='daily_new_default'").fetchone()
    try:
        return max(0, int(row["value"])) if row else 20
    except ValueError:
        return 20


# ------------------------------------------------------------------ 词书

class BookIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None


def _book_or_404(c: sqlite3.Connection, book_id: int) -> sqlite3.Row:
    row = c.execute("SELECT * FROM word_books WHERE id=?", (book_id,)).fetchone()
    if not row:
        raise HTTPException(404, "词书不存在")
    return row


@app.get("/api/books")
def list_books():
    with db() as c:
        rows = c.execute("""
            SELECT b.*,
                   (SELECT COUNT(*) FROM words w WHERE w.book_id=b.id) AS word_count,
                   (SELECT COUNT(*) FROM words w WHERE w.book_id=b.id AND w.is_mastered=1) AS mastered_count,
                   (SELECT COUNT(*) FROM words w WHERE w.book_id=b.id AND w.is_error=1) AS error_count,
                   (SELECT COUNT(*) FROM words w WHERE w.book_id=b.id
                        AND w.next_review_at IS NOT NULL AND w.next_review_at<=?) AS due_count
            FROM word_books b ORDER BY b.id
        """, (to_db(now_utc()),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["daily_new"] = _get_plan(r["id"])
        out.append(d)
    return out


@app.post("/api/books")
def create_book(body: BookIn):
    with db() as c:
        cur = c.execute("INSERT INTO word_books(name, description) VALUES(?,?)",
                        (body.name.strip(), (body.description or "").strip() or None))
        return {"id": cur.lastrowid}


@app.put("/api/books/{book_id}")
def update_book(book_id: int, body: BookIn):
    with db() as c:
        _book_or_404(c, book_id)
        c.execute("UPDATE word_books SET name=?, description=? WHERE id=?",
                  (body.name.strip(), (body.description or "").strip() or None, book_id))
    return {"ok": True}


@app.delete("/api/books/{book_id}")
def delete_book(book_id: int):
    with db() as c:
        _book_or_404(c, book_id)
        c.execute("DELETE FROM word_books WHERE id=?", (book_id,))
        c.execute("DELETE FROM app_settings WHERE key=?", (f"book.{book_id}.daily_new",))
    return {"ok": True}


class PlanIn(BaseModel):
    daily_new: int = Field(ge=0, le=500)


@app.put("/api/books/{book_id}/plan")
def set_plan(book_id: int, body: PlanIn):
    with db() as c:
        _book_or_404(c, book_id)
        c.execute("INSERT INTO app_settings(key, value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (f"book.{book_id}.daily_new", str(body.daily_new)))
    return {"ok": True, "daily_new": body.daily_new}


# ------------------------------------------------------------------ 单词

class WordIn(BaseModel):
    book_id: int
    word: str = Field(min_length=1, max_length=120)
    meaning: str = Field(min_length=1, max_length=500)


class WordPatch(BaseModel):
    meaning: str | None = None
    is_mastered: int | None = None
    reset_progress: bool = False


@app.get("/api/books/{book_id}/words")
def list_words(book_id: int, page: int = 1, size: int = 20, q: str = "", filter: str = "all"):
    page = max(1, page)
    size = min(200, max(1, size))
    where, params = ["book_id=?"], [book_id]
    if q.strip():
        where.append("(word LIKE ? OR meaning LIKE ?)")
        params += [f"%{q.strip()}%", f"%{q.strip()}%"]
    if filter == "error":
        where.append("is_error=1")
    elif filter == "mastered":
        where.append("is_mastered=1")
    elif filter == "due":
        where.append("next_review_at IS NOT NULL AND next_review_at<=?")
        params.append(to_db(now_utc()))
    sql_where = " AND ".join(where)
    with db() as c:
        total = c.execute(f"SELECT COUNT(*) AS n FROM words WHERE {sql_where}", params).fetchone()["n"]
        rows = c.execute(
            f"""SELECT words.*, EXISTS(SELECT 1 FROM word_details d WHERE d.word_id=words.id) AS has_detail
                FROM words WHERE {sql_where} ORDER BY id LIMIT ? OFFSET ?""",
            params + [size, (page - 1) * size]).fetchall()
    return {"total": total, "page": page, "size": size, "items": [dict(r) for r in rows]}


@app.post("/api/words")
def create_word(body: WordIn):
    with db() as c:
        _book_or_404(c, body.book_id)
        try:
            cur = c.execute("INSERT INTO words(book_id, word, meaning) VALUES(?,?,?)",
                            (body.book_id, body.word.strip(), body.meaning.strip()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "该词书里已有这个词")
    return {"id": cur.lastrowid}


@app.put("/api/words/{word_id}")
def patch_word(word_id: int, body: WordPatch):
    with db() as c:
        row = c.execute("SELECT * FROM words WHERE id=?", (word_id,)).fetchone()
        if not row:
            raise HTTPException(404, "单词不存在")
        if body.meaning is not None:
            c.execute("UPDATE words SET meaning=? WHERE id=?", (body.meaning.strip(), word_id))
        if body.is_mastered is not None:
            c.execute("UPDATE words SET is_mastered=? WHERE id=?",
                      (1 if body.is_mastered else 0, word_id))
        if body.reset_progress:
            c.execute("""UPDATE words SET is_error=0, is_mastered=0, stability=0, difficulty=0,
                         reps=0, lapses=0, next_review_at=NULL, last_review_at=NULL WHERE id=?""",
                      (word_id,))
    return {"ok": True}


@app.delete("/api/words/{word_id}")
def delete_word(word_id: int):
    with db() as c:
        c.execute("DELETE FROM words WHERE id=?", (word_id,))
    return {"ok": True}


# ------------------------------------------------------------------ 背诵会话

class SessionIn(BaseModel):
    book_id: int
    mode: str = "A"                       # A: 中文->英文 ; B: 英文->中文
    scope: str = "due"                    # due | error | book | new
    limit: int = Field(default=20, ge=1, le=MAX_WORDS_PER_SESSION)
    ids: list[int] | None = None          # 指定词（今日计划「开始这一组」用）


class AnswerIn(BaseModel):
    session_id: str
    word_id: int
    answer: str = ""
    elapsed_ms: int = 0
    retry: bool = False     # True = 答错时留在原地重试（不落库）；空提交/看答案时忽略


@app.post("/api/session/start")
def start_session(body: SessionIn):
    if body.mode not in ("A", "B"):
        raise HTTPException(400, "mode 只能是 A 或 B")
    if body.scope not in ("due", "error", "book", "new", "yesterday"):
        raise HTTPException(400, "scope 不合法")
    now = to_db(now_utc())
    limit = min(body.limit, MAX_WORDS_PER_SESSION)
    with db() as c:
        _book_or_404(c, body.book_id)
        if body.ids:
            # 保持前端给的顺序；只取属于本词书、且仍存在的词
            wanted = []
            seen = set()
            for wid in body.ids:
                if wid in seen:
                    continue
                seen.add(wid)
                row = c.execute("SELECT id FROM words WHERE id=? AND book_id=?",
                                (wid, body.book_id)).fetchone()
                if row:
                    wanted.append(wid)
                if len(wanted) >= limit:
                    break
            rows = [{"id": w} for w in wanted]
        elif body.scope == "due":
            rows = c.execute("""SELECT id FROM words WHERE book_id=? AND next_review_at IS NOT NULL
                                AND next_review_at<=? ORDER BY next_review_at LIMIT ?""",
                             (body.book_id, now, limit)).fetchall()
        elif body.scope == "error":
            rows = c.execute("SELECT id FROM words WHERE book_id=? AND is_error=1 ORDER BY id LIMIT ?",
                             (body.book_id, limit)).fetchall()
        elif body.scope == "new":
            daily = _get_plan(body.book_id)
            take = min(limit, daily) if daily else limit
            rows = c.execute("SELECT id FROM words WHERE book_id=? AND next_review_at IS NULL "
                             "ORDER BY id LIMIT ?", (body.book_id, take)).fetchall()
        elif body.scope == "yesterday":
            # 昨天背过的词，无视 next_review_at（强制过一遍）
            yesterday = _local_yesterday()
            rows = c.execute("""SELECT w.id, MAX(l.review_time) AS last_time
                                FROM review_logs l JOIN words w ON w.id=l.word_id
                                WHERE w.book_id=? AND date(l.review_time, '+8 hours')=?
                                GROUP BY w.id ORDER BY last_time LIMIT ?""",
                             (body.book_id, yesterday, limit)).fetchall()
        else:  # book：先到期，再没学过的，最后其他，保证覆盖
            rows = c.execute("""SELECT id FROM words WHERE book_id=?
                                ORDER BY (next_review_at IS NULL) DESC,
                                         next_review_at ASC, id ASC LIMIT ?""",
                             (body.book_id, limit)).fetchall()
    queue = [r["id"] for r in rows]
    if not queue:
        raise HTTPException(400, "没有可背诵的词：换个范围，或先导入单词")
    sess = {
        "id": uuid.uuid4().hex,
        "book_id": body.book_id,
        "mode": body.mode,
        "scope": body.scope,
        "queue": queue,
        "answered": {},
        "tries": {},
        "results": [],
        "finished": False,
        "started_at": time.time(),
        "touched": time.time(),
    }
    with _SESS_LOCK:
        _sweep_sessions_locked()
        SESSIONS[sess["id"]] = sess
    return _public_session(sess)


@app.get("/api/session/{session_id}")
def get_session(session_id: str):
    with _SESS_LOCK:
        sess = SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(404, "会话已过期，请重新开始")
    sess["touched"] = time.time()
    return _public_session(sess)


@app.post("/api/session/answer")
def answer(body: AnswerIn):
    with _SESS_LOCK:
        sess = SESSIONS.get(body.session_id)
    if not sess:
        raise HTTPException(404, "会话已过期，请重新开始")
    if sess["finished"]:
        raise HTTPException(400, "会话已结束")
    if body.word_id not in sess["queue"]:
        raise HTTPException(400, "该词不在本次会话里")
    if body.word_id in sess["answered"]:          # 幂等：重复提交直接回放结果
        return sess["answered"][body.word_id]
    sess["touched"] = time.time()

    with db() as c:
        row = c.execute("SELECT * FROM words WHERE id=?", (body.word_id,)).fetchone()
        if not row:
            raise HTTPException(404, "单词不存在")
        ok = judge(sess["mode"], body.answer, row)
        typed = (body.answer or "").strip()
        # 答错且允许重试（且不是空提交/看答案）→ 只判分，不落库，让前端停在原地重来
        if not ok and body.retry and typed:
            sess["tries"][body.word_id] = sess["tries"].get(body.word_id, 0) + 1
            return {
                "word_id": body.word_id,
                "correct": False,
                "retry": True,
                "tries": sess["tries"][body.word_id],
                "canonical": "",
            }
        patch = _schedule_simple(ok, row["reps"], row["lapses"],
                                 row["stability"], row["difficulty"])
        # 答对时：已标记错题的词，间隔到 7 天以上自动清除错题标记
        is_error = 1 if not ok else (0 if patch["stability"] >= 7 else row["is_error"])
        c.execute("""UPDATE words SET is_mastered=?, is_error=?, stability=?, difficulty=?,
                     reps=?, lapses=?, next_review_at=?, last_review_at=? WHERE id=?""",
                  (patch["is_mastered"], is_error, patch["stability"], patch["difficulty"],
                   patch["reps"], patch["lapses"], patch["next_review_at"],
                   patch["last_review_at"], body.word_id))
        c.execute("INSERT INTO review_logs(word_id, review_time, score, is_correct) VALUES(?,?,?,?)",
                  (body.word_id, to_db(now_utc()), 3 if ok else 1, 1 if ok else 0))
        det = c.execute("SELECT phrases, examples FROM word_details WHERE word_id=?",
                        (body.word_id,)).fetchone()

    result = {
        "word_id": body.word_id,
        "correct": ok,
        "retry": False,
        "tries": sess["tries"].get(body.word_id, 0) + (0 if ok else 1),
        "canonical": row["word"] if sess["mode"] == "A" else row["meaning"],
        "word": row["word"],
        "meaning": row["meaning"],
        "phrases": det["phrases"] if det else "",
        "examples": det["examples"] if det else "",
        "next_review_at": patch["next_review_at"],
        "mastered": bool(patch["is_mastered"]),
        "elapsed_ms": body.elapsed_ms,
    }
    sess["answered"][body.word_id] = result
    sess["results"].append({
        "word_id": body.word_id,
        "word": row["word"],
        "meaning": row["meaning"],
        "answer": body.answer,
        "correct": ok,
    })
    return result


@app.post("/api/session/finish")
def finish_session(body: dict):
    sid = (body or {}).get("session_id", "")
    with _SESS_LOCK:
        sess = SESSIONS.get(sid)
    if not sess:
        raise HTTPException(404, "会话已过期")
    sess["finished"] = True
    results = sess["results"]
    total = len(results)
    correct = sum(1 for r in results if r["correct"])
    wrong = [r for r in results if not r["correct"]]
    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "duration_sec": int(time.time() - sess["started_at"]),
        "mode": sess["mode"],
        "wrong": wrong,
    }


# ------------------------------------------------------------------ 用法 / 例句

class DetailIn(BaseModel):
    phrases: str = ""
    examples: str = ""
    pos: str = ""


def _clean_lines(text: str) -> str:
    """去掉行尾空白与空行，最多保留 12 行。"""
    lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n")]
    return "\n".join([ln for ln in lines if ln][:12])


def _clean_pos(pos: str) -> str:
    """词性：只留常见缩写，最长 24 字符。"""
    p = (pos or "").strip().replace("；", "/").replace(",", "/").replace(" ", "")
    return p[:24]


@app.get("/api/words/{word_id}/detail")
def get_detail(word_id: int):
    with db() as c:
        row0 = c.execute("SELECT word, meaning FROM words WHERE id=?", (word_id,)).fetchone()
        if not row0:
            raise HTTPException(404, "单词不存在")
        row = c.execute("SELECT phrases, examples, pos FROM word_details WHERE word_id=?",
                        (word_id,)).fetchone()
    pos = (row["pos"] if row and row["pos"] else "") or _pos_from_meaning(row0["meaning"])
    return {
        "word_id": word_id,
        "word": row0["word"],
        "meaning": row0["meaning"],
        "pos": pos,
        "phrases": row["phrases"] if row else "",
        "examples": row["examples"] if row else "",
    }


def _pos_from_meaning(meaning: str) -> str:
    """释义里如果自带 n./v./adj. 之类，就抽出来当词性。"""
    found = re.findall(r"(?:^|[；;，,\s])(n|v|adj|adv|prep|pron|conj|num|art|int)\.", meaning or "")
    order = ["n", "v", "adj", "adv", "prep", "pron", "conj", "num", "art", "int"]
    uniq = [x for x in order if x in found]
    return " / ".join(x + "." for x in uniq[:3])


@app.put("/api/words/{word_id}/detail")
def put_detail(word_id: int, body: DetailIn):
    phrases = _clean_lines(body.phrases)
    examples = _clean_lines(body.examples)
    pos = _clean_pos(body.pos)
    with db() as c:
        if not c.execute("SELECT 1 FROM words WHERE id=?", (word_id,)).fetchone():
            raise HTTPException(404, "单词不存在")
        if not phrases and not examples and not pos:
            c.execute("DELETE FROM word_details WHERE word_id=?", (word_id,))
            return {"ok": True, "cleared": True}
        c.execute("""INSERT INTO word_details(word_id, phrases, examples, pos, update_time)
                     VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                     ON CONFLICT(word_id) DO UPDATE SET
                       phrases=excluded.phrases,
                       examples=excluded.examples,
                       pos=excluded.pos,
                       update_time=CURRENT_TIMESTAMP""",
                  (word_id, phrases, examples, pos))
    return {"ok": True, "phrases": phrases, "examples": examples, "pos": pos}


# ------------------------------------------------------------------ 每日记录

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@app.get("/api/history/days")
def history_days(book_id: int, limit: int = 30):
    """按本地日期汇总：每天学了几个词、几次作答、正确率、其中新词多少。"""
    limit = max(1, min(120, limit))
    with db() as c:
        _book_or_404(c, book_id)
        rows = c.execute("""
            SELECT date(l.review_time, '+8 hours') AS d,
                   COUNT(*) AS attempts,
                   COUNT(DISTINCT l.word_id) AS words,
                   SUM(CASE WHEN l.is_correct=1 THEN 1 ELSE 0 END) AS ok
            FROM review_logs l JOIN words w ON w.id=l.word_id
            WHERE w.book_id=?
            GROUP BY d ORDER BY d DESC LIMIT ?
        """, (book_id, limit)).fetchall()
        firsts = c.execute("""
            SELECT date(f.first_time, '+8 hours') AS d, COUNT(*) AS n FROM (
                SELECT l.word_id, MIN(l.review_time) AS first_time
                FROM review_logs l JOIN words w ON w.id=l.word_id
                WHERE w.book_id=? GROUP BY l.word_id
            ) f GROUP BY d
        """, (book_id,)).fetchall()
    first_map = {r["d"]: r["n"] for r in firsts}
    return [{
        "date": r["d"],
        "words": r["words"],
        "attempts": r["attempts"],
        "correct": r["ok"] or 0,
        "new": first_map.get(r["d"], 0),
        "accuracy": round((r["ok"] or 0) / r["attempts"], 4) if r["attempts"] else 0.0,
    } for r in rows]


def _day_items(conn: sqlite3.Connection, book_id: int, date: str) -> list[dict[str, Any]]:
    """某天背过的词（按词去重，含答错次数、是否新词、用法）。"""
    rows = conn.execute("""
        SELECT w.id, w.word, w.meaning,
               COUNT(*) AS attempts,
               SUM(CASE WHEN l.is_correct=1 THEN 1 ELSE 0 END) AS ok,
               MAX(l.review_time) AS last_time,
               (SELECT MIN(x.review_time) FROM review_logs x WHERE x.word_id=w.id) AS first_time
        FROM review_logs l JOIN words w ON w.id=l.word_id
        WHERE w.book_id=? AND date(l.review_time, '+8 hours')=?
        GROUP BY w.id ORDER BY last_time
    """, (book_id, date)).fetchall()
    dets = conn.execute("""
        SELECT d.word_id, d.phrases, d.examples FROM word_details d
        JOIN words w ON w.id=d.word_id WHERE w.book_id=?
    """, (book_id,)).fetchall()
    det_map = {r["word_id"]: r for r in dets}
    items = []
    for r in rows:
        d = det_map.get(r["id"])
        first_local = from_db(r["first_time"]).astimezone(LOCAL_TZ).strftime("%Y-%m-%d") if r["first_time"] else ""
        items.append({
            "word_id": r["id"],
            "word": r["word"],
            "meaning": r["meaning"],
            "attempts": r["attempts"],
            "correct": r["ok"] or 0,
            "all_correct": (r["ok"] or 0) == r["attempts"],
            "last_time": r["last_time"],
            "is_new": first_local == date,
            "phrases": d["phrases"] if d else "",
            "examples": d["examples"] if d else "",
        })
    return items


@app.get("/api/history/day")
def history_day(book_id: int, date: str):
    """某一天背过的词（按词去重，含答错次数、是否新词）。"""
    if not _DATE_RE.match(date or ""):
        raise HTTPException(400, "date 需要 YYYY-MM-DD")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "date 不是合法日期")
    with db() as c:
        _book_or_404(c, book_id)
        items = _day_items(c, book_id, date)
    return {"date": date, "count": len(items), "items": items}


def _local_today() -> str:
    return now_utc().astimezone(LOCAL_TZ).strftime("%Y-%m-%d")

def _local_yesterday() -> str:
    return (now_utc().astimezone(LOCAL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")


@app.get("/api/history/today")
def history_today(book_id: int):
    """「当日记忆」：今天（本地时区）背过的词。"""
    date = _local_today()
    with db() as c:
        _book_or_404(c, book_id)
        items = _day_items(c, book_id, date)
    return {"date": date, "count": len(items), "items": items}


@app.get("/api/history/yesterday")
def history_yesterday(book_id: int):
    """昨日回顾：昨天（本地时区）背过的词，无视是否到期。"""
    date = _local_yesterday()
    with db() as c:
        _book_or_404(c, book_id)
        items = _day_items(c, book_id, date)
    return {"date": date, "count": len(items), "items": items}


@app.get("/api/plan/today")
def plan_today(book_id: int, limit: int = 500):
    """「今日计划」：今天该学的（到期复习 + 今日新词额度）+ 今天已经学过的。

    和「当日记忆」的区别：当日记忆只列已经背过的；今日计划把**还没学的**也列出来，
    所以能看到"今天一共要背多少、还剩多少"。
    """
    limit = max(1, min(2000, limit))
    date = _local_today()
    now_s = to_db(now_utc())
    with db() as c:
        _book_or_404(c, book_id)
        plan = _get_plan(book_id)
        done_items = _day_items(c, book_id, date)
        done_map = {x["word_id"]: x for x in done_items}
        new_started = sum(1 for x in done_items if x["is_new"])
        remain_new = max(0, plan - new_started) if plan else limit

        due = c.execute("""SELECT id, word, meaning, next_review_at FROM words
                           WHERE book_id=? AND next_review_at IS NOT NULL AND next_review_at<=?
                           ORDER BY next_review_at, id LIMIT ?""",
                        (book_id, now_s, limit)).fetchall()
        new = c.execute("""SELECT id, word, meaning FROM words
                           WHERE book_id=? AND next_review_at IS NULL ORDER BY id LIMIT ?""",
                        (book_id, remain_new)).fetchall()

    pending: list[dict[str, Any]] = []
    studied: list[dict[str, Any]] = []
    planned_ids: set[int] = set()

    def pack(row, kind: str) -> dict[str, Any]:
        d = done_map.get(row["id"])
        return {
            "word_id": row["id"],
            "word": row["word"],
            "meaning": row["meaning"],
            "kind": kind,                       # due | new
            "done": d is not None,
            "attempts": d["attempts"] if d else 0,
            "correct": d["correct"] if d else 0,
            "all_correct": d["all_correct"] if d else False,
            "last_time": d["last_time"] if d else None,
            "is_new_today": d["is_new"] if d else False,
        }

    for r in due:
        planned_ids.add(r["id"])
        item = pack(r, "due")
        (studied if item["done"] else pending).append(item)
    for r in new:
        planned_ids.add(r["id"])
        item = pack(r, "new")
        (studied if item["done"] else pending).append(item)
    for x in done_items:                        # 今天学了但不在计划内的（例如临时整本复习）
        if x["word_id"] in planned_ids:
            continue
        studied.append({
            "word_id": x["word_id"], "word": x["word"], "meaning": x["meaning"],
            "kind": "extra", "done": True, "attempts": x["attempts"], "correct": x["correct"],
            "all_correct": x["all_correct"], "last_time": x["last_time"], "is_new_today": x["is_new"],
        })

    n_due = sum(1 for x in pending if x["kind"] == "due")
    n_new = sum(1 for x in pending if x["kind"] == "new")
    return {
        "date": date,
        "book_id": book_id,
        "daily_new": plan,
        "new_used_today": new_started,
        "counts": {
            "pending_due": n_due,
            "pending_new": n_new,
            "pending": len(pending),
            "studied": len(studied),
            "total": len(pending) + len(studied),
        },
        "items": pending + studied,             # 待学在前，已学在后
    }


# ------------------------------------------------------------------ 统计

@app.get("/api/stats")
def stats(book_id: int, period: str = Query(default="day", alias="range")):
    # 参数名避开内置 range()，对外仍用 ?range=day|week|month
    now = now_utc()
    today = local_day_start(now)
    if period == "week":
        start = today - timedelta(days=6)
    elif period == "month":
        start = today - timedelta(days=29)
    else:
        start = today
    start_s, end_s = to_db(start), to_db(today + timedelta(days=1))

    with db() as c:
        # 新词 = 该区间内首次被复习的词；复习 = 区间内日志数 - 新词数
        first_rows = c.execute("""
            SELECT w.id,
                   (SELECT MIN(l.review_time) FROM review_logs l WHERE l.word_id=w.id) AS first_time
            FROM words w WHERE w.book_id=?
        """, (book_id,)).fetchall()
        new_ids = [r["id"] for r in first_rows if r["first_time"] and start_s <= r["first_time"] < end_s]
        logs = c.execute("""
            SELECT l.* FROM review_logs l JOIN words w ON w.id=l.word_id
            WHERE w.book_id=? AND l.review_time>=? AND l.review_time<?
        """, (book_id, start_s, end_s)).fetchall()
        total_words = c.execute("SELECT COUNT(*) AS n FROM words WHERE book_id=?", (book_id,)).fetchone()["n"]
        mastered = c.execute("SELECT COUNT(*) AS n FROM words WHERE book_id=? AND is_mastered=1",
                             (book_id,)).fetchone()["n"]
        due = c.execute("""SELECT COUNT(*) AS n FROM words WHERE book_id=? AND next_review_at IS NOT NULL
                           AND next_review_at<=?""", (book_id, to_db(now))).fetchone()["n"]
        errors = c.execute("SELECT COUNT(*) AS n FROM words WHERE book_id=? AND is_error=1",
                           (book_id,)).fetchone()["n"]

        # 近 7 天柱状图
        chart = []
        log_days = {}
        for l in c.execute("""SELECT l.review_time, l.is_correct, l.word_id FROM review_logs l
                              JOIN words w ON w.id=l.word_id
                              WHERE w.book_id=? AND l.review_time>=?""",
                           (book_id, to_db(today - timedelta(days=6)))):
            d = from_db(l["review_time"]).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
            cur = log_days.setdefault(d, {"new": 0, "review": 0, "correct": 0, "total": 0})
            cur["total"] += 1
            cur["correct"] += 1 if l["is_correct"] else 0
        first_map = {r["id"]: r["first_time"] for r in first_rows if r["first_time"]}
        for day in range(6, -1, -1):
            d = (today - timedelta(days=day)).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
            agg = log_days.get(d, {"new": 0, "review": 0, "correct": 0, "total": 0})
            agg["new"] = sum(
                1 for w, ft in first_map.items()
                if ft and from_db(ft).astimezone(LOCAL_TZ).strftime("%Y-%m-%d") == d)
            agg["review"] = max(0, agg["total"] - agg["new"])
            chart.append({"date": d, **agg})

        # 连续打卡（含今天，只算有日志的日子）
        days_rows = c.execute("""SELECT DISTINCT date(l.review_time, '+8 hours') AS d
                                 FROM review_logs l JOIN words w ON w.id=l.word_id
                                 WHERE w.book_id=? ORDER BY d DESC""", (book_id,)).fetchall()
    days = [r["d"] for r in days_rows]
    streak = 0
    cursor = today.astimezone(LOCAL_TZ).date()
    if days and days[0] != str(cursor):
        # 今天还没学，从昨天开始算
        cursor = cursor - timedelta(days=1)
    day_set = set(days)
    while str(cursor) in day_set:
        streak += 1
        cursor = cursor - timedelta(days=1)

    plan = _get_plan(book_id)
    days_count = {"day": 1, "week": 7, "month": 30}.get(period, 1)
    target = plan * days_count
    return {
        "range": period,
        "new": len(new_ids),
        "review": max(0, len(logs) - len(new_ids)),
        "accuracy": round(sum(1 for l in logs if l["is_correct"]) / len(logs), 4) if logs else 0.0,
        "total_words": total_words,
        "mastered": mastered,
        "due": due,
        "errors": errors,
        "streak": streak,
        "plan": plan,
        "target": target,
        "completion": round(len(new_ids) / target, 4) if target else 0.0,
        "chart": chart,
    }


# ------------------------------------------------------------------ 导入导出

def _decode_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


@app.post("/api/import/csv")
async def import_csv(book_id: int = Form(...), file: UploadFile = File(...)):
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(400, "文件超过 20MB")
    text = _decode_text(raw)
    reader = csv.reader(io.StringIO(text))
    added = skipped = failed = updated = 0
    samples: list[str] = []
    with db() as c:
        _book_or_404(c, book_id)
        first = True
        for lineno, row in enumerate(reader, 1):
            if not row or all(not (x or "").strip() for x in row):
                continue
            if len(row) < 2:
                failed += 1
                if len(samples) < 5:
                    samples.append(f"第 {lineno} 行不是 word,meaning 两列")
                continue
            word, meaning = row[0].strip(), row[1].strip()
            # 可选列：3=词组用法 4=例句 5=词性（多条用「|」分隔）
            phrases = (row[2].strip() if len(row) > 2 else "")
            examples = (row[3].strip() if len(row) > 3 else "")
            pos = _clean_pos(row[4].strip() if len(row) > 4 else "")
            if first and word.lower() in ("word", "单词", "词"):
                first = False
                continue
            first = False
            if not word or not meaning:
                failed += 1
                continue
            try:
                cur = c.execute("INSERT INTO words(book_id, word, meaning) VALUES(?,?,?)",
                                (book_id, word, meaning))
                added += 1
                if phrases or examples or pos:
                    c.execute("INSERT OR REPLACE INTO word_details(word_id, phrases, examples, pos) "
                              "VALUES(?,?,?,?)",
                              (cur.lastrowid, _split_lines(phrases), _split_lines(examples), pos))
            except sqlite3.IntegrityError:
                skipped += 1
                # 已存在的词：只补空的用法，不覆盖手写内容
                if phrases or examples or pos:
                    row2 = c.execute("SELECT id FROM words WHERE book_id=? AND word=?",
                                     (book_id, word)).fetchone()
                    if row2:
                        old = c.execute("SELECT phrases, examples FROM word_details WHERE word_id=?",
                                        (row2["id"],)).fetchone()
                        if not old or not (old["phrases"] or old["examples"]):
                            c.execute("INSERT OR REPLACE INTO word_details(word_id, phrases, examples, pos) "
                                      "VALUES(?,?,?,?)",
                                      (row2["id"], _split_lines(phrases), _split_lines(examples), pos))
                            updated += 1
    return {"added": added, "skipped": skipped, "updated": updated,
            "failed": failed, "messages": samples}


def _split_lines(s: str) -> str:
    """把 CSV 里的「|」或「；」分隔的多条内容统一成换行文本。"""
    if not s:
        return ""
    parts = re.split(r"[|｜]", s)
    return "\n".join(p.strip() for p in parts if p.strip())


@app.get("/api/export")
def export(book_id: int, format: str = "json"):
    with db() as c:
        book = _book_or_404(c, book_id)
        words = [dict(r) for r in c.execute("SELECT * FROM words WHERE book_id=? ORDER BY id",
                                            (book_id,)).fetchall()]
        dets = {r["word_id"]: r for r in c.execute("""
            SELECT d.word_id, d.phrases, d.examples, d.pos FROM word_details d
            JOIN words w ON w.id=d.word_id WHERE w.book_id=?""", (book_id,)).fetchall()}
        for x in words:
            d = dets.get(x["id"])
            x["phrases"] = d["phrases"] if d else ""
            x["examples"] = d["examples"] if d else ""
            x["pos"] = (d["pos"] if d and d["pos"] else "") or _pos_from_meaning(x["meaning"])
        log_count = c.execute("""SELECT COUNT(*) AS n FROM review_logs l JOIN words w ON w.id=l.word_id
                                 WHERE w.book_id=?""", (book_id,)).fetchone()["n"]
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["word", "meaning", "phrases", "examples", "pos", "is_mastered", "is_error",
                    "stability", "difficulty", "reps", "lapses", "next_review_at", "last_review_at"])
        for x in words:
            w.writerow([x["word"], x["meaning"],
                        (x["phrases"] or "").replace("\n", "|"),
                        (x["examples"] or "").replace("\n", "|"),
                        x["pos"] or "",
                        x["is_mastered"], x["is_error"], x["stability"],
                        x["difficulty"], x["reps"], x["lapses"], x["next_review_at"] or "",
                        x["last_review_at"] or ""])
        return Response(content=buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                        headers={"Content-Disposition":
                                 f'attachment; filename="vocab_book{book_id}.csv"'})
    payload = {
        "app_version": 1,
        "exported_at": to_db(now_utc()),
        "book": dict(book),
        "words": words,
        "logs_count": log_count,
    }
    return Response(content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
                    media_type="application/json",
                    headers={"Content-Disposition":
                             f'attachment; filename="vocab_book{book_id}.json"'})


# ------------------------------------------------------------------ 本地 AI（可选）
# 走本机 Ollama 的 /api/chat，只为「生成词组 + 例句」服务。
# Ollama 没装 / 没启动 / 没模型时，所有接口都返回「不可用」，不影响基础背诵。

OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434")
AI_MODEL_DEFAULT = "qwen2.5:7b"
AI_LOCK = threading.Lock()          # 单卡串行，避免批量任务把显存打爆
AI_JOBS: dict[str, dict[str, Any]] = {}
_AI_JOB_LOCK = threading.Lock()


def _ai_model() -> str:
    with db() as c:
        row = c.execute("SELECT value FROM app_settings WHERE key='ai_model'").fetchone()
    return (row["value"] if row else "") or AI_MODEL_DEFAULT


def _http_json(url: str, payload: dict | None = None, timeout: float = 10.0) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _ollama_chat(payload: dict, timeout: float = 180.0) -> dict:
    """调 Ollama 的 /api/chat，把常见错误翻成人话（尤其是「模型不存在」）。"""
    try:
        return _http_json(OLLAMA_BASE + "/api/chat", payload, timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        model = payload.get("model") or ""
        if e.code == 404:
            raise HTTPException(
                503,
                f"Ollama 里没有模型「{model}」（{body or 'not found'}）。"
                f"先在终端执行：ollama pull {model}",
            )
        raise HTTPException(503, f"Ollama 返回 {e.code}：{body or e.reason}")
    except Exception as e:
        raise HTTPException(503, f"连不上本地 Ollama（{OLLAMA_BASE}）：{e}")


def _ollama_models() -> list[str]:
    try:
        data = _http_json(OLLAMA_BASE + "/api/tags", None, 5.0)
        return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _ollama_alive() -> bool:
    try:
        _http_json(OLLAMA_BASE + "/api/version", None, 3.0)
        return True
    except Exception:
        return False


AI_PROMPT = """单词：{word}
中文释义：{meaning}

请输出这个词的词性、常用搭配和例句。硬性要求：
1. pos：这个词在这条释义下的词性缩写，用 n. / v. / adj. / adv. 这类写法（多个用 " / " 连接，如 "n. / v."）
2. phrases：2-3 条常用词组/搭配，每条格式为「英文搭配 —— 中文解释」
3. examples：2-3 条例句，每条格式为「英文句子 —— 中文翻译」
4. 每条都必须同时含英文和中文，用 " —— " 分隔；不要编号，不要 markdown，不要额外解释
5. 只输出 JSON

示例（照这个格式）：
{{"pos": "v. / n.", "phrases": ["look forward to —— 期待", "in advance —— 提前"], "examples": ["I look forward to seeing you. —— 我期待见到你。"]}}"""


def _ai_generate(word: str, meaning: str, model: str) -> dict[str, list[str]]:
    """调用 Ollama 生成；返回 {'phrases': [...], 'examples': [...]}。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是英语学习词典助手，只输出 JSON。"},
            {"role": "user", "content": AI_PROMPT.format(word=word, meaning=meaning)},
        ],
        "stream": False,
        "format": "json",
        "keep_alive": "30m",          # 批量生成时别让模型每词重载
        "options": {"temperature": 0.2, "num_predict": 400},
    }
    try:
        data = _ollama_chat(payload, 180.0)
    except Exception as e:
        raise HTTPException(503, f"连不上本地 Ollama（{OLLAMA_BASE}）：{e}")
    text = (data.get("message") or {}).get("content", "") or ""
    obj = None
    try:
        obj = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
            except Exception:
                obj = None
    if not isinstance(obj, dict):
        raise HTTPException(502, "模型没有返回合法 JSON，换个模型或重试")

    def clean(key: str) -> list[str]:
        vals = obj.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        out = []
        for v in vals:
            if isinstance(v, str) and v.strip():
                out.append(v.strip()[:200])
        return out[:3]

    phrases, examples = clean("phrases"), clean("examples")
    pos_raw = obj.get("pos") or ""
    if isinstance(pos_raw, list):
        pos_raw = " / ".join(str(x) for x in pos_raw)
    pos = _clean_pos(str(pos_raw))
    if not phrases and not examples:
        raise HTTPException(502, "模型返回内容为空")
    return {"pos": pos, "phrases": phrases, "examples": examples}


def _save_detail(word_id: int, phrases: list[str], examples: list[str], pos: str = "") -> None:
    with db() as c:
        c.execute("""INSERT INTO word_details(word_id, phrases, examples, pos, update_time)
                     VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                     ON CONFLICT(word_id) DO UPDATE SET
                       phrases=excluded.phrases, examples=excluded.examples,
                       pos=excluded.pos,
                       update_time=CURRENT_TIMESTAMP""",
                  (word_id, "\n".join(phrases), "\n".join(examples), _clean_pos(pos)))


@app.get("/api/ai/status")
def ai_status():
    models = _ollama_models()
    alive = _ollama_alive()
    model = _ai_model()
    return {
        "available": alive,
        "base": OLLAMA_BASE,
        "model": model,
        "model_ready": model in models or any(m.split(":")[0] == model for m in models),
        "models": models,
    }


class AIWordIn(BaseModel):
    word_id: int
    save: bool = True


@app.post("/api/ai/usage")
def ai_usage(body: AIWordIn):
    """为一个词生成词组/例句（可只预览不保存）。"""
    with db() as c:
        row = c.execute("SELECT id, word, meaning FROM words WHERE id=?", (body.word_id,)).fetchone()
    if not row:
        raise HTTPException(404, "单词不存在")
    model = _ai_model()
    with AI_LOCK:
        gen = _ai_generate(row["word"], row["meaning"], model)
    if body.save:
        _save_detail(row["id"], gen["phrases"], gen["examples"], gen["pos"])
    return {"word_id": row["id"], "word": row["word"], "model": model,
            "pos": gen["pos"],
            "phrases": "\n".join(gen["phrases"]), "examples": "\n".join(gen["examples"]),
            "saved": body.save}


class AIBookIn(BaseModel):
    book_id: int
    limit: int = 30
    only_missing: bool = True
    overwrite: bool = False


@app.post("/api/ai/fill_book")
def ai_fill_book(body: AIBookIn):
    """批量补齐一个词书的用法（后台任务，进度可查）。"""
    if not _ollama_alive():
        raise HTTPException(503, "本地 Ollama 没在运行")
    limit = max(1, min(200, body.limit))
    with db() as c:
        _book_or_404(c, body.book_id)
        sql = ("SELECT w.id, w.word, w.meaning FROM words w "
               "WHERE w.book_id=? ")
        if body.only_missing and not body.overwrite:
            sql += "AND NOT EXISTS (SELECT 1 FROM word_details d WHERE d.word_id=w.id) "
        sql += "ORDER BY w.id LIMIT ?"
        rows = c.execute(sql, (body.book_id, limit)).fetchall()
    if not rows:
        raise HTTPException(400, "没有需要生成的词（可能都已经有用法了）")
    job_id = uuid.uuid4().hex
    job = {"id": job_id, "book_id": body.book_id, "total": len(rows), "done": 0,
           "failed": 0, "status": "running", "message": "", "started": time.time()}
    with _AI_JOB_LOCK:
        AI_JOBS[job_id] = job
    targets = [(r["id"], r["word"], r["meaning"]) for r in rows]
    model = _ai_model()

    def worker() -> None:
        for wid, word, meaning in targets:
            job["message"] = word
            try:
                with AI_LOCK:
                    gen = _ai_generate(word, meaning, model)
                _save_detail(wid, gen["phrases"], gen["examples"], gen.get("pos", ""))
                job["done"] += 1
            except Exception as e:          # 单个失败不影响整批
                job["failed"] += 1
                job["message"] = f"{word} 失败：{e}"
        job["status"] = "finished"

    threading.Thread(target=worker, daemon=True).start()
    return {"job_id": job_id, "total": job["total"]}


@app.get("/api/ai/jobs/{job_id}")
def ai_job(job_id: str):
    with _AI_JOB_LOCK:
        job = AI_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在或已过期")
    return {k: v for k, v in job.items() if k != "started"}


# ------------------------------------------------------------------ 当日记忆 → 场景短文

STORY_STYLES = {
    "story": ("一个连贯的生活场景小故事", "请用第三人称写一个小故事，有具体场景和情绪"),
    "dialogue": ("两个人的日常对话", "请写成对话体：每句前面用 A: / B: 标注说话人"),
}


class StoryIn(BaseModel):
    book_id: int
    day: str | None = None            # 默认今天（本地时区）
    style: str = "story"              # story | dialogue
    length: int = Field(default=200, ge=50, le=800)
    save: bool = True


def _story_prompt(words: list[dict], style: str, length: int, strict: bool = False) -> str:
    style_name, style_hint = STORY_STYLES.get(style, STORY_STYLES["story"])
    listed = "\n".join(f"- {w['word']}：{w['meaning']}" for w in words)
    head = "CRITICAL: the main text MUST be written in English. Never write the story body in Chinese.\n" if strict else ""
    return f"""{head}下面是一个学生今天背过的单词（含中文释义）：
{listed}

请写一段{style_name}，要求：
1. 正文必须全部是英文（English only），**长度不少于 {length} 个英文单词**（目标 {length}–{length + 40} 词，写不满就继续写，不要提前收尾）
2. 尽量把这些单词都用上（可以变形，如复数、过去式）
3. {style_hint}
4. 分成 2-4 段；每段英文写完后，**另起一行**以「中文：」开头写这段的中文翻译（「中文：」必须单独位于行首）
5. 不要标题、不要解释、不要 markdown 符号、不要重复同一句话

正确的格式示例：
Tom had to negotiate the budget with his team.
中文：汤姆必须和团队协商预算。

现在开始写："""


def _story_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z'-]*", text))


@app.post("/api/ai/story")
def ai_story(body: StoryIn):
    """按某天背过的词，生成场景故事 / 日常对话。"""
    if body.style not in STORY_STYLES:
        raise HTTPException(400, "style 只能是 story 或 dialogue")
    day = body.day or _local_today()
    if not _DATE_RE.match(day):
        raise HTTPException(400, "day 需要 YYYY-MM-DD")
    with db() as c:
        _book_or_404(c, body.book_id)
        items = _day_items(c, body.book_id, day)
    if not items:
        raise HTTPException(400, "这一天还没有背过的单词")
    if not _ollama_alive():
        raise HTTPException(503, "本地 Ollama 没在运行")

    words = [{"word": x["word"], "meaning": x["meaning"]} for x in items][:30]
    model = _ai_model()

    def _ask(strict: bool) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system",
                 "content": "你是英语写作老师。正文必须是英文，只在「中文：」行写中文翻译。"},
                {"role": "user", "content": _story_prompt(words, body.style, body.length, strict)},
            ],
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0.7, "num_predict": min(4096, int(body.length * 5) + 700)},
        }
        with AI_LOCK:
            data = _ollama_chat(payload, 300.0)
        return ((data.get("message") or {}).get("content") or "").strip()

    def _extend(text: str) -> str:
        """模型写得太短时，让它按原文继续扩写。"""
        ask = (f"下面这篇英文短文的正文只有约 {_story_word_count(text)} 个单词，"
               f"请把它扩写到不少于 {body.length} 个英文单词：保留原有情节和用词，补充细节、"
               f"动作或对话，不要改变人物设定。\n"
               f"输出规则：英文正文 + 每段后用「中文：」开头的整段中文翻译，不要标题。\n\n"
               f"原文：\n{text}")
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是英语写作老师，擅长把短文扩写得更丰满。"},
                {"role": "user", "content": ask},
            ],
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0.6, "num_predict": min(4096, int(body.length * 6) + 800)},
        }
        with AI_LOCK:
            data = _ollama_chat(payload, 300.0)
        return ((data.get("message") or {}).get("content") or "").strip()

    content = ""
    try:
        for attempt in (1, 2):
            content = _ask(strict=(attempt == 2))
            # 英文词数太少 = 模型用中文写了正文，重试一次
            if content and _story_word_count(content) >= 20:
                break
    except Exception as e:
        raise HTTPException(503, f"生成失败（Ollama）：{e}")
    if not content:
        raise HTTPException(502, "模型返回为空，重试一次试试")
    if _story_word_count(content) < 20:
        raise HTTPException(502, "模型没用英文写正文，换个模型或再试一次")

    # 明显没写够长度 → 让它扩写一次
    if _story_word_count(content) < int(body.length * 0.8):
        try:
            longer = _extend(content)
            if _story_word_count(longer) > _story_word_count(content):
                content = longer
        except Exception:
            pass

    # 统一标记：把模型可能写出的【中】换成「中文：」，并保证「中文：」单独成行
    content = content.replace("【中】", "中文：").replace("中文：中文：", "中文：")
    content = re.sub(r"(?<=\S)中文：", "\n中文：", content)
    lines = [ln.rstrip() for ln in content.split("\n")]
    content = "\n".join([ln for ln in lines if ln.strip() not in ("中文：", "中文")])

    used = [w["word"] for w in words]
    if body.save:
        with db() as c:
            c.execute("""INSERT INTO daily_writings(book_id, day, style, target_len, content, word_list)
                         VALUES(?,?,?,?,?,?)""",
                      (body.book_id, day, body.style, body.length, content, ",".join(used)))
    return {
        "day": day,
        "style": body.style,
        "length": body.length,
        "model": model,
        "words": used,
        "content": content,
        "word_count": _story_word_count(content),
    }


@app.get("/api/ai/story")
def ai_story_list(book_id: int, day: str | None = None, limit: int = 5):
    """回看之前生成过的短文。"""
    day = day or _local_today()
    with db() as c:
        _book_or_404(c, book_id)
        rows = c.execute("""SELECT id, day, style, target_len, word_list, create_time,
                                   substr(content, 1, 120) AS preview
                            FROM daily_writings WHERE book_id=? AND day=?
                            ORDER BY id DESC LIMIT ?""", (book_id, day, max(1, min(20, limit)))).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/ai/story/{writing_id}")
def ai_story_one(writing_id: int):
    with db() as c:
        row = c.execute("SELECT * FROM daily_writings WHERE id=?", (writing_id,)).fetchone()
    if not row:
        raise HTTPException(404, "找不到这篇短文")
    return dict(row)


# ------------------------------------------------------------------ 自检

def _selftest() -> int:
    """不启动服务，验证判分与调度逻辑。"""
    global DB_PATH
    tmp = os.path.join(_HERE, "_selftest.db")
    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    DB_PATH = tmp
    init_db()

    checks: list[tuple[str, bool]] = []
    checks.append(("英文判分：大小写/空格", judge_en("  Apple ", "apple")))
    checks.append(("英文判分：多答案", judge_en("autumn", "fall, autumn")))
    checks.append(("英文判分：错误答案", not judge_en("apples", "apple")))
    checks.append(("中文判分：完整义项", judge_zh("苹果", "n. 苹果；苹果树")))
    checks.append(("中文判分：全角标点", judge_zh("苹果。", "苹果")))
    checks.append(("中文判分：错误答案", not judge_zh("香蕉", "苹果；苹果树")))

    ok_sched = _schedule_simple(True, 0, 0, 0.0, 0.0)
    bad_sched = _schedule_simple(False, 2, 1, 4.0, 4.5)
    checks.append(("答对：间隔 1 天", ok_sched["stability"] == 1.0 and ok_sched["reps"] == 1))
    checks.append(("答错：10 分钟后再来", bad_sched["stability"] == 1.0 and bad_sched["lapses"] == 2))

    with db() as c:
        c.execute("INSERT INTO word_books(name) VALUES('t')")
        c.execute("INSERT INTO words(book_id, word, meaning) VALUES(1,'apple','苹果')")
        c.execute("SELECT 1")

    print("\n===== SELF TEST =====")
    for name, ok in checks:
        print(("  PASS  " if ok else "  FAIL  ") + name)
    with db() as c:
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
    print("  journal_mode =", mode)
    for p in (tmp, tmp + "-wal", tmp + "-shm"):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass
    passed = all(ok for _, ok in checks)
    print("=====", "ALL PASS" if passed else "SOME FAILED", "=====\n")
    return 0 if passed else 1


# ============================================================================
# 入口
# ============================================================================

def _free_port(start: int, tries: int = 10) -> int:
    """端口被占（比如上次的窗口没关干净）就自动往后顺延，避免启动直接失败。"""
    import socket
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((HOST, p))
                return p
            except OSError:
                continue
    return start


def main() -> int:
    global DB_PATH, HOST, LAN_MODE, ACCESS_TOKEN
    parser = argparse.ArgumentParser(description="本地离线背单词系统")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--lan", action="store_true",
                        help="允许手机/局域网访问（绑定 0.0.0.0，需要访问码）")
    parser.add_argument("--token", default="", help="自定义局域网访问码")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--db", default=None, help="指定数据库路径（默认同目录 vocab.db）")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()

    if args.db:
        DB_PATH = os.path.abspath(args.db)
    if args.selftest:
        return _selftest()

    init_db()
    LAN_MODE = bool(args.lan)
    if LAN_MODE:
        HOST = "0.0.0.0"
        ACCESS_TOKEN = args.token or _load_token()
    port = _free_port(args.port)
    if port != args.port:
        print(f"[vocab] 端口 {args.port} 已被占用，改用 {port}")
    url = f"http://{HOST}:{port}"
    print(f"[vocab] 数据库: {DB_PATH}")
    if LAN_MODE:
        lan = _lan_ip()
        print(f"[vocab] 本机: http://127.0.0.1:{port}")
        print(f"[vocab] 手机: http://{lan}:{port}    访问码: {ACCESS_TOKEN}")
        print("[vocab] 手机需与电脑同一 WiFi；首次打开输入访问码")
        if not args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    else:
        print(f"[vocab] 打开: {url}   (仅本机可访问，Ctrl+C 退出)")
        if not args.no_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    import uvicorn
    uvicorn.run(app, host=HOST, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

