from __future__ import annotations

import csv
import io
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from workers import asgi


LOCAL_TZ = timezone(timedelta(hours=8))
MAX_WORDS_PER_SESSION = 200
SESSION_TTL_SECONDS = 30 * 60
AI_MODEL = "@cf/zai-org/glm-4.7-flash"

DEFAULT_SETTINGS = {
    "daily_new_default": "20",
    "theme": "auto",
    "auto_next": "1",
    "ai_model": AI_MODEL,
}


def _to_py(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    try:
        return value.to_py()
    except Exception:
        return value


def _row(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    converted = _to_py(value)
    if isinstance(converted, dict):
        return converted
    try:
        from js import Object

        return dict(Object.entries(value).to_py())
    except Exception as exc:
        raise RuntimeError("无法转换 D1 查询结果") from exc


def _db(request: Request):
    return request.scope["env"].DB


def _stmt(request: Request, sql: str, params: tuple | list = ()):
    stmt = _db(request).prepare(sql)
    return stmt.bind(*params) if params else stmt


async def db_all(request: Request, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
    result = await _stmt(request, sql, params).all()
    return [_row(item) or {} for item in result.results]


async def db_one(request: Request, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
    return _row(await _stmt(request, sql, params).first())


async def db_run(request: Request, sql: str, params: tuple | list = ()) -> Any:
    return await _stmt(request, sql, params).run()


async def db_scalar(request: Request, sql: str, params: tuple | list = (), key: str = "n") -> Any:
    row = await db_one(request, sql, params)
    return row.get(key) if row else None


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_db(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def from_db(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def local_day_start(dt: datetime) -> datetime:
    local = dt.astimezone(LOCAL_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _local_today() -> str:
    return now_utc().astimezone(LOCAL_TZ).strftime("%Y-%m-%d")


def _local_yesterday() -> str:
    return (now_utc().astimezone(LOCAL_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")


_PUNCT_EN = " \t\r\n.,!?;:'\"()[]{}-–—…、，。"
_SPLIT_MEANING = re.compile(r"[;；,，、/|]+")


def _norm_text(value: str) -> str:
    trans = str.maketrans("，。！？；：（）【】　", ",.!?;:()[] ")
    return (value or "").translate(trans).strip()


def _norm_en(value: str) -> str:
    return re.sub(r"[-–—\s]+", "", _norm_text(value).lower().strip(_PUNCT_EN))


def judge_en(answer: str, word: str) -> bool:
    answer_n = _norm_en(answer)
    if not answer_n:
        return False
    choices = re.split(r"[,;/|；，、]+", word or "")
    return any(answer_n == _norm_en(choice) for choice in choices if choice.strip())


def judge_zh(answer: str, meaning: str) -> bool:
    answer_n = re.sub(r"[\W_]+", "", _norm_text(answer), flags=re.UNICODE).lower()
    if not answer_n:
        return False
    choices = _SPLIT_MEANING.split(meaning or "")
    for choice in choices:
        choice = re.sub(r"^(?:n|v|adj|adv|prep|pron|conj|num|art|int)\.\s*", "", choice.strip(), flags=re.I)
        choice_n = re.sub(r"[\W_]+", "", _norm_text(choice), flags=re.UNICODE).lower()
        if len(choice_n) >= 2 and (choice_n in answer_n or answer_n in choice_n):
            return True
    return False


def judge(mode: str, answer: str, word_row: dict[str, Any]) -> bool:
    return judge_en(answer, word_row["word"]) if mode == "A" else judge_zh(answer, word_row["meaning"])


def _schedule_simple(is_correct: bool, reps: int, lapses: int, stability: float, difficulty: float) -> dict:
    base = now_utc()
    if is_correct:
        reps_new = reps + 1
        intervals = [1, 2, 4, 7, 15, 30, 60, 120]
        interval = intervals[min(reps, len(intervals) - 1)]
        diff = max(1.0, (difficulty or 5.0) - 0.15)
        return {
            "reps": reps_new,
            "lapses": lapses,
            "stability": float(interval),
            "difficulty": round(diff, 3),
            "is_mastered": 1 if interval >= 30 and reps_new >= 3 else 0,
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


def _clean_lines(text: str) -> str:
    lines = [line.strip() for line in (text or "").replace("\r", "").split("\n")]
    return "\n".join(line for line in lines if line)[:4000]


def _clean_pos(pos: str) -> str:
    return (pos or "").strip().replace("；", "/").replace(",", "/").replace(" ", "")[:24]


def _pos_from_meaning(meaning: str) -> str:
    found = re.findall(r"(?:^|[；;，,\s])(n|v|adj|adv|prep|pron|conj|num|art|int)\.", meaning or "")
    order = ["n", "v", "adj", "adv", "prep", "pron", "conj", "num", "art", "int"]
    return " / ".join(item + "." for item in order if item in found)[:24]


app = FastAPI(title="Vocab Trainer on Cloudflare", docs_url=None, redoc_url=None)


@app.middleware("http")
async def same_origin_guard(request: Request, call_next):
    if request.url.path.startswith("/api/"):
        host = request.headers.get("host", "")
        origin = request.headers.get("origin", "")
        referer = request.headers.get("referer", "")
        if origin and urlparse(origin).netloc != host:
            return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
        if not origin and referer and urlparse(referer).netloc != host:
            return JSONResponse({"detail": "拒绝跨站请求"}, status_code=403)
    return await call_next(request)


async def _book_or_404(request: Request, book_id: int) -> dict[str, Any]:
    row = await db_one(request, "SELECT * FROM word_books WHERE id=?", (book_id,))
    if not row:
        raise HTTPException(404, "词书不存在")
    return row


async def _get_plan(request: Request, book_id: int) -> int:
    row = await db_one(request, "SELECT value FROM app_settings WHERE key=?", (f"book.{book_id}.daily_new",))
    if not row:
        row = await db_one(request, "SELECT value FROM app_settings WHERE key='daily_new_default'")
    try:
        return max(0, int(row["value"])) if row else 20
    except (ValueError, TypeError):
        return 20


@app.get("/api/health")
async def health(request: Request):
    count = await db_scalar(request, "SELECT COUNT(*) AS n FROM words")
    return {"ok": True, "database": "Cloudflare D1", "word_count": count or 0, "version": 2}


class SettingsIn(BaseModel):
    values: dict[str, str]


@app.get("/api/settings")
async def get_settings(request: Request):
    rows = await db_all(request, "SELECT key, value FROM app_settings")
    data = {row["key"]: row["value"] for row in rows}
    for key, value in DEFAULT_SETTINGS.items():
        data.setdefault(key, value)
    data["ai_model"] = AI_MODEL
    return data


@app.put("/api/settings")
async def put_settings(body: SettingsIn, request: Request):
    for key, value in body.values.items():
        if key == "ai_model":
            value = AI_MODEL
        await db_run(
            request,
            "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
    return await get_settings(request)


class BookIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = None


class PlanIn(BaseModel):
    daily_new: int = Field(ge=0, le=500)


@app.get("/api/books")
async def list_books(request: Request):
    rows = await db_all(
        request,
        """
        SELECT b.*,
          (SELECT COUNT(*) FROM words w WHERE w.book_id=b.id) AS word_count,
          (SELECT COUNT(*) FROM words w WHERE w.book_id=b.id AND w.is_mastered=1) AS mastered_count,
          (SELECT COUNT(*) FROM words w WHERE w.book_id=b.id AND w.is_error=1) AS error_count,
          (SELECT COUNT(*) FROM words w WHERE w.book_id=b.id AND w.next_review_at IS NOT NULL
             AND w.next_review_at<=?) AS due_count
        FROM word_books b ORDER BY b.id
        """,
        (to_db(now_utc()),),
    )
    for row in rows:
        row["daily_new"] = await _get_plan(request, row["id"])
    return rows


@app.post("/api/books")
async def create_book(body: BookIn, request: Request):
    row = await db_one(
        request,
        "INSERT INTO word_books(name,description) VALUES(?,?) RETURNING id",
        (body.name.strip(), (body.description or "").strip() or None),
    )
    return {"id": row["id"]}


@app.put("/api/books/{book_id}")
async def update_book(book_id: int, body: BookIn, request: Request):
    await _book_or_404(request, book_id)
    await db_run(request, "UPDATE word_books SET name=?,description=? WHERE id=?", (body.name.strip(), (body.description or "").strip() or None, book_id))
    return {"ok": True}


@app.delete("/api/books/{book_id}")
async def delete_book(book_id: int, request: Request):
    await _book_or_404(request, book_id)
    await db_run(request, "DELETE FROM word_books WHERE id=?", (book_id,))
    await db_run(request, "DELETE FROM app_settings WHERE key=?", (f"book.{book_id}.daily_new",))
    return {"ok": True}


@app.put("/api/books/{book_id}/plan")
async def set_plan(book_id: int, body: PlanIn, request: Request):
    await _book_or_404(request, book_id)
    await db_run(
        request,
        "INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (f"book.{book_id}.daily_new", str(body.daily_new)),
    )
    return {"ok": True, "daily_new": body.daily_new}


class WordIn(BaseModel):
    book_id: int
    word: str = Field(min_length=1, max_length=120)
    meaning: str = Field(min_length=1, max_length=500)


class WordPatch(BaseModel):
    meaning: str | None = None
    is_mastered: int | None = None
    reset_progress: bool = False


@app.get("/api/books/{book_id}/words")
async def list_words(book_id: int, request: Request, page: int = 1, size: int = 20, q: str = "", filter: str = "all"):
    page, size = max(1, page), min(200, max(1, size))
    where, params = ["book_id=?"], [book_id]
    if q.strip():
        where.append("(word LIKE ? OR meaning LIKE ?)")
        params.extend([f"%{q.strip()}%", f"%{q.strip()}%"])
    if filter == "error":
        where.append("is_error=1")
    elif filter == "mastered":
        where.append("is_mastered=1")
    elif filter == "due":
        where.append("next_review_at IS NOT NULL AND next_review_at<=?")
        params.append(to_db(now_utc()))
    sql_where = " AND ".join(where)
    total = await db_scalar(request, f"SELECT COUNT(*) AS n FROM words WHERE {sql_where}", params)
    rows = await db_all(
        request,
        f"""SELECT words.*, EXISTS(SELECT 1 FROM word_details d WHERE d.word_id=words.id) AS has_detail
            FROM words WHERE {sql_where} ORDER BY id LIMIT ? OFFSET ?""",
        params + [size, (page - 1) * size],
    )
    return {"total": total or 0, "page": page, "size": size, "items": rows}


@app.post("/api/words")
async def create_word(body: WordIn, request: Request):
    await _book_or_404(request, body.book_id)
    try:
        row = await db_one(
            request,
            "INSERT INTO words(book_id,word,meaning) VALUES(?,?,?) RETURNING id",
            (body.book_id, body.word.strip(), body.meaning.strip()),
        )
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(409, "该词书里已有这个词") from exc
        raise
    return {"id": row["id"]}


@app.put("/api/words/{word_id}")
async def patch_word(word_id: int, body: WordPatch, request: Request):
    row = await db_one(request, "SELECT id FROM words WHERE id=?", (word_id,))
    if not row:
        raise HTTPException(404, "单词不存在")
    if body.meaning is not None:
        await db_run(request, "UPDATE words SET meaning=? WHERE id=?", (body.meaning.strip(), word_id))
    if body.is_mastered is not None:
        await db_run(request, "UPDATE words SET is_mastered=? WHERE id=?", (1 if body.is_mastered else 0, word_id))
    if body.reset_progress:
        await db_run(request, """UPDATE words SET is_error=0,is_mastered=0,stability=0,difficulty=0,
            reps=0,lapses=0,next_review_at=NULL,last_review_at=NULL WHERE id=?""", (word_id,))
    return {"ok": True}


@app.delete("/api/words/{word_id}")
async def delete_word(word_id: int, request: Request):
    await db_run(request, "DELETE FROM words WHERE id=?", (word_id,))
    return {"ok": True}


class SessionIn(BaseModel):
    book_id: int
    mode: str = "A"
    scope: str = "due"
    limit: int = Field(default=20, ge=1, le=MAX_WORDS_PER_SESSION)
    ids: list[int] | None = None


class AnswerIn(BaseModel):
    session_id: str
    word_id: int
    answer: str = ""
    elapsed_ms: int = 0
    retry: bool = False


async def _save_session(request: Request, session: dict[str, Any]) -> None:
    expires = to_db(now_utc() + timedelta(seconds=SESSION_TTL_SECONDS))
    await db_run(
        request,
        "INSERT INTO study_sessions(id,payload,expires_at) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,expires_at=excluded.expires_at",
        (session["id"], json.dumps(session, ensure_ascii=False), expires),
    )


async def _load_session(request: Request, session_id: str) -> dict[str, Any] | None:
    row = await db_one(request, "SELECT payload FROM study_sessions WHERE id=? AND expires_at>?", (session_id, to_db(now_utc())))
    if not row:
        return None
    session = json.loads(row["payload"])
    await _save_session(request, session)
    return session


async def _public_session(request: Request, session: dict[str, Any]) -> dict[str, Any]:
    word_map: dict[int, dict[str, Any]] = {}
    queue = session["queue"]
    for start in range(0, len(queue), 90):
        chunk = queue[start:start + 90]
        marks = ",".join("?" for _ in chunk)
        rows = await db_all(request, f"SELECT id,word,meaning FROM words WHERE id IN ({marks})", chunk)
        word_map.update({row["id"]: row for row in rows})
    answered = session.get("answered", {})
    items = []
    for word_id in queue:
        row = word_map.get(word_id)
        if row:
            items.append({
                "word_id": word_id,
                "prompt": row["meaning"] if session["mode"] == "A" else row["word"],
                "answered": str(word_id) in answered,
            })
    return {
        "session_id": session["id"],
        "book_id": session["book_id"],
        "mode": session["mode"],
        "scope": session["scope"],
        "total": len(items),
        "done": len(answered),
        "items": items,
        "results": session.get("results", []),
        "finished": session.get("finished", False),
        "started_at": session["started_at"],
    }


@app.post("/api/session/start")
async def start_session(body: SessionIn, request: Request):
    if body.mode not in ("A", "B"):
        raise HTTPException(400, "mode 只能是 A 或 B")
    if body.scope not in ("due", "error", "book", "new", "yesterday"):
        raise HTTPException(400, "scope 不合法")
    await _book_or_404(request, body.book_id)
    limit, now = min(body.limit, MAX_WORDS_PER_SESSION), to_db(now_utc())
    if body.ids:
        unique_ids = list(dict.fromkeys(body.ids))[:limit]
        rows = []
        for start in range(0, len(unique_ids), 90):
            chunk = unique_ids[start:start + 90]
            marks = ",".join("?" for _ in chunk)
            found = await db_all(request, f"SELECT id FROM words WHERE book_id=? AND id IN ({marks})", [body.book_id] + chunk)
            found_ids = {row["id"] for row in found}
            rows.extend({"id": word_id} for word_id in chunk if word_id in found_ids)
    elif body.scope == "due":
        rows = await db_all(request, "SELECT id FROM words WHERE book_id=? AND next_review_at IS NOT NULL AND next_review_at<=? ORDER BY next_review_at LIMIT ?", (body.book_id, now, limit))
    elif body.scope == "error":
        rows = await db_all(request, "SELECT id FROM words WHERE book_id=? AND is_error=1 ORDER BY id LIMIT ?", (body.book_id, limit))
    elif body.scope == "new":
        daily = await _get_plan(request, body.book_id)
        rows = await db_all(request, "SELECT id FROM words WHERE book_id=? AND next_review_at IS NULL ORDER BY id LIMIT ?", (body.book_id, min(limit, daily) if daily else limit))
    elif body.scope == "yesterday":
        rows = await db_all(request, """SELECT w.id,MAX(l.review_time) AS last_time FROM review_logs l
            JOIN words w ON w.id=l.word_id WHERE w.book_id=? AND date(l.review_time,'+8 hours')=?
            GROUP BY w.id ORDER BY last_time LIMIT ?""", (body.book_id, _local_yesterday(), limit))
    else:
        rows = await db_all(request, "SELECT id FROM words WHERE book_id=? ORDER BY (next_review_at IS NULL) DESC,next_review_at ASC,id ASC LIMIT ?", (body.book_id, limit))
    queue = [row["id"] for row in rows]
    if not queue:
        raise HTTPException(400, "没有可背诵的词：换个范围，或先导入单词")
    session = {
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
    }
    await _save_session(request, session)
    return await _public_session(request, session)


@app.get("/api/session/{session_id}")
async def get_session(session_id: str, request: Request):
    session = await _load_session(request, session_id)
    if not session:
        raise HTTPException(404, "会话已过期，请重新开始")
    return await _public_session(request, session)


@app.post("/api/session/answer")
async def answer(body: AnswerIn, request: Request):
    session = await _load_session(request, body.session_id)
    if not session:
        raise HTTPException(404, "会话已过期，请重新开始")
    if session.get("finished"):
        raise HTTPException(400, "会话已结束")
    if body.word_id not in session["queue"]:
        raise HTTPException(400, "该词不在本次会话里")
    key = str(body.word_id)
    if key in session["answered"]:
        return session["answered"][key]
    row = await db_one(request, "SELECT * FROM words WHERE id=?", (body.word_id,))
    if not row:
        raise HTTPException(404, "单词不存在")
    ok, typed = judge(session["mode"], body.answer, row), (body.answer or "").strip()
    if not ok and body.retry and typed:
        session["tries"][key] = session["tries"].get(key, 0) + 1
        await _save_session(request, session)
        return {"word_id": body.word_id, "correct": False, "retry": True, "tries": session["tries"][key], "canonical": ""}
    patch = _schedule_simple(ok, row["reps"], row["lapses"], row["stability"], row["difficulty"])
    is_error = 1 if not ok else (0 if patch["stability"] >= 7 else row["is_error"])
    await db_run(request, """UPDATE words SET is_mastered=?,is_error=?,stability=?,difficulty=?,
        reps=?,lapses=?,next_review_at=?,last_review_at=? WHERE id=?""", (
        patch["is_mastered"], is_error, patch["stability"], patch["difficulty"], patch["reps"],
        patch["lapses"], patch["next_review_at"], patch["last_review_at"], body.word_id,
    ))
    await db_run(request, "INSERT INTO review_logs(word_id,review_time,score,is_correct) VALUES(?,?,?,?)", (body.word_id, to_db(now_utc()), 3 if ok else 1, 1 if ok else 0))
    detail = await db_one(request, "SELECT phrases,examples FROM word_details WHERE word_id=?", (body.word_id,))
    result = {
        "word_id": body.word_id,
        "correct": ok,
        "retry": False,
        "tries": session["tries"].get(key, 0) + (0 if ok else 1),
        "canonical": row["word"] if session["mode"] == "A" else row["meaning"],
        "word": row["word"],
        "meaning": row["meaning"],
        "phrases": detail["phrases"] if detail else "",
        "examples": detail["examples"] if detail else "",
        "next_review_at": patch["next_review_at"],
        "mastered": bool(patch["is_mastered"]),
        "elapsed_ms": body.elapsed_ms,
    }
    session["answered"][key] = result
    session["results"].append({"word_id": body.word_id, "word": row["word"], "meaning": row["meaning"], "answer": body.answer, "correct": ok})
    await _save_session(request, session)
    return result


@app.post("/api/session/finish")
async def finish_session(body: dict, request: Request):
    session = await _load_session(request, (body or {}).get("session_id", ""))
    if not session:
        raise HTTPException(404, "会话已过期")
    session["finished"] = True
    await _save_session(request, session)
    results = session["results"]
    correct = sum(1 for item in results if item["correct"])
    return {
        "total": len(results),
        "correct": correct,
        "accuracy": round(correct / len(results), 4) if results else 0.0,
        "duration_sec": int(time.time() - session["started_at"]),
        "mode": session["mode"],
        "wrong": [item for item in results if not item["correct"]],
    }


class DetailIn(BaseModel):
    phrases: str = ""
    examples: str = ""
    pos: str = ""


@app.get("/api/words/{word_id}/detail")
async def get_detail(word_id: int, request: Request):
    word = await db_one(request, "SELECT word,meaning FROM words WHERE id=?", (word_id,))
    if not word:
        raise HTTPException(404, "单词不存在")
    detail = await db_one(request, "SELECT phrases,examples,pos FROM word_details WHERE word_id=?", (word_id,))
    return {
        "word_id": word_id,
        "word": word["word"],
        "meaning": word["meaning"],
        "pos": ((detail or {}).get("pos") or _pos_from_meaning(word["meaning"])),
        "phrases": (detail or {}).get("phrases") or "",
        "examples": (detail or {}).get("examples") or "",
    }


@app.put("/api/words/{word_id}/detail")
async def put_detail(word_id: int, body: DetailIn, request: Request):
    if not await db_one(request, "SELECT 1 AS ok FROM words WHERE id=?", (word_id,)):
        raise HTTPException(404, "单词不存在")
    phrases, examples, pos = _clean_lines(body.phrases), _clean_lines(body.examples), _clean_pos(body.pos)
    if not phrases and not examples and not pos:
        await db_run(request, "DELETE FROM word_details WHERE word_id=?", (word_id,))
        return {"ok": True, "cleared": True}
    await db_run(
        request,
        """INSERT INTO word_details(word_id,phrases,examples,pos,update_time)
        VALUES(?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(word_id) DO UPDATE SET
        phrases=excluded.phrases,examples=excluded.examples,pos=excluded.pos,update_time=CURRENT_TIMESTAMP""",
        (word_id, phrases, examples, pos),
    )
    return {"ok": True, "phrases": phrases, "examples": examples, "pos": pos}


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


async def _day_items(request: Request, book_id: int, date: str) -> list[dict[str, Any]]:
    rows = await db_all(
        request,
        """SELECT w.id,w.word,w.meaning,COUNT(*) AS attempts,
        SUM(CASE WHEN l.is_correct=1 THEN 1 ELSE 0 END) AS ok,
        MAX(l.review_time) AS last_time,
        (SELECT MIN(x.review_time) FROM review_logs x WHERE x.word_id=w.id) AS first_time
        FROM review_logs l JOIN words w ON w.id=l.word_id
        WHERE w.book_id=? AND date(l.review_time,'+8 hours')=?
        GROUP BY w.id ORDER BY last_time""",
        (book_id, date),
    )
    details = await db_all(
        request,
        """SELECT d.word_id,d.phrases,d.examples FROM word_details d
        JOIN words w ON w.id=d.word_id WHERE w.book_id=?""",
        (book_id,),
    )
    detail_map = {row["word_id"]: row for row in details}
    items = []
    for row in rows:
        detail = detail_map.get(row["id"])
        first_local = from_db(row["first_time"]).astimezone(LOCAL_TZ).strftime("%Y-%m-%d") if row["first_time"] else ""
        items.append({
            "word_id": row["id"],
            "word": row["word"],
            "meaning": row["meaning"],
            "attempts": row["attempts"],
            "correct": row["ok"] or 0,
            "all_correct": (row["ok"] or 0) == row["attempts"],
            "last_time": row["last_time"],
            "is_new": first_local == date,
            "phrases": detail["phrases"] if detail else "",
            "examples": detail["examples"] if detail else "",
        })
    return items


@app.get("/api/history/days")
async def history_days(book_id: int, request: Request, limit: int = 30):
    await _book_or_404(request, book_id)
    limit = max(1, min(120, limit))
    rows = await db_all(
        request,
        """SELECT date(l.review_time,'+8 hours') AS d,COUNT(*) AS attempts,
        COUNT(DISTINCT l.word_id) AS words,SUM(CASE WHEN l.is_correct=1 THEN 1 ELSE 0 END) AS ok
        FROM review_logs l JOIN words w ON w.id=l.word_id WHERE w.book_id=?
        GROUP BY d ORDER BY d DESC LIMIT ?""",
        (book_id, limit),
    )
    firsts = await db_all(
        request,
        """SELECT date(f.first_time,'+8 hours') AS d,COUNT(*) AS n FROM (
        SELECT l.word_id,MIN(l.review_time) AS first_time FROM review_logs l
        JOIN words w ON w.id=l.word_id WHERE w.book_id=? GROUP BY l.word_id) f GROUP BY d""",
        (book_id,),
    )
    first_map = {row["d"]: row["n"] for row in firsts}
    return [{
        "date": row["d"],
        "words": row["words"],
        "attempts": row["attempts"],
        "correct": row["ok"] or 0,
        "new": first_map.get(row["d"], 0),
        "accuracy": round((row["ok"] or 0) / row["attempts"], 4) if row["attempts"] else 0.0,
    } for row in rows]


def _valid_date(value: str, name: str = "date") -> str:
    if not _DATE_RE.match(value or ""):
        raise HTTPException(400, f"{name} 需要 YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(400, f"{name} 不是合法日期") from exc
    return value


@app.get("/api/history/day")
async def history_day(book_id: int, date: str, request: Request):
    _valid_date(date)
    await _book_or_404(request, book_id)
    items = await _day_items(request, book_id, date)
    return {"date": date, "count": len(items), "items": items}


@app.get("/api/history/today")
async def history_today(book_id: int, request: Request):
    await _book_or_404(request, book_id)
    date = _local_today()
    items = await _day_items(request, book_id, date)
    return {"date": date, "count": len(items), "items": items}


@app.get("/api/history/yesterday")
async def history_yesterday(book_id: int, request: Request):
    await _book_or_404(request, book_id)
    date = _local_yesterday()
    items = await _day_items(request, book_id, date)
    return {"date": date, "count": len(items), "items": items}


@app.get("/api/plan/today")
async def plan_today(book_id: int, request: Request, limit: int = 500):
    await _book_or_404(request, book_id)
    limit = max(1, min(2000, limit))
    date, now_s = _local_today(), to_db(now_utc())
    plan = await _get_plan(request, book_id)
    done_items = await _day_items(request, book_id, date)
    done_map = {item["word_id"]: item for item in done_items}
    new_started = sum(1 for item in done_items if item["is_new"])
    remain_new = max(0, plan - new_started) if plan else limit
    due = await db_all(request, """SELECT id,word,meaning,next_review_at FROM words
        WHERE book_id=? AND next_review_at IS NOT NULL AND next_review_at<=?
        ORDER BY next_review_at,id LIMIT ?""", (book_id, now_s, limit))
    new = await db_all(request, "SELECT id,word,meaning FROM words WHERE book_id=? AND next_review_at IS NULL ORDER BY id LIMIT ?", (book_id, remain_new))
    pending, studied, planned_ids = [], [], set()

    def pack(row: dict, kind: str) -> dict:
        done = done_map.get(row["id"])
        return {
            "word_id": row["id"], "word": row["word"], "meaning": row["meaning"], "kind": kind,
            "done": done is not None, "attempts": done["attempts"] if done else 0,
            "correct": done["correct"] if done else 0, "all_correct": done["all_correct"] if done else False,
            "last_time": done["last_time"] if done else None, "is_new_today": done["is_new"] if done else False,
        }

    for row in due:
        planned_ids.add(row["id"])
        item = pack(row, "due")
        (studied if item["done"] else pending).append(item)
    for row in new:
        planned_ids.add(row["id"])
        item = pack(row, "new")
        (studied if item["done"] else pending).append(item)
    for item in done_items:
        if item["word_id"] not in planned_ids:
            studied.append({
                "word_id": item["word_id"], "word": item["word"], "meaning": item["meaning"],
                "kind": "extra", "done": True, "attempts": item["attempts"], "correct": item["correct"],
                "all_correct": item["all_correct"], "last_time": item["last_time"], "is_new_today": item["is_new"],
            })
    return {
        "date": date,
        "book_id": book_id,
        "daily_new": plan,
        "new_used_today": new_started,
        "counts": {
            "pending_due": sum(1 for item in pending if item["kind"] == "due"),
            "pending_new": sum(1 for item in pending if item["kind"] == "new"),
            "pending": len(pending), "studied": len(studied), "total": len(pending) + len(studied),
        },
        "items": pending + studied,
    }


@app.get("/api/stats")
async def stats(book_id: int, request: Request, period: str = Query(default="day", alias="range")):
    await _book_or_404(request, book_id)
    now, today = now_utc(), local_day_start(now_utc())
    start = today - timedelta(days=6 if period == "week" else 29 if period == "month" else 0)
    start_s, end_s = to_db(start), to_db(today + timedelta(days=1))
    first_rows = await db_all(request, """SELECT w.id,
        (SELECT MIN(l.review_time) FROM review_logs l WHERE l.word_id=w.id) AS first_time
        FROM words w WHERE w.book_id=?""", (book_id,))
    new_ids = [row["id"] for row in first_rows if row["first_time"] and start_s <= row["first_time"] < end_s]
    logs = await db_all(request, """SELECT l.* FROM review_logs l JOIN words w ON w.id=l.word_id
        WHERE w.book_id=? AND l.review_time>=? AND l.review_time<?""", (book_id, start_s, end_s))
    total_words = await db_scalar(request, "SELECT COUNT(*) AS n FROM words WHERE book_id=?", (book_id,)) or 0
    mastered = await db_scalar(request, "SELECT COUNT(*) AS n FROM words WHERE book_id=? AND is_mastered=1", (book_id,)) or 0
    due = await db_scalar(request, "SELECT COUNT(*) AS n FROM words WHERE book_id=? AND next_review_at IS NOT NULL AND next_review_at<=?", (book_id, to_db(now))) or 0
    errors = await db_scalar(request, "SELECT COUNT(*) AS n FROM words WHERE book_id=? AND is_error=1", (book_id,)) or 0
    recent_logs = await db_all(request, """SELECT l.review_time,l.is_correct,l.word_id FROM review_logs l
        JOIN words w ON w.id=l.word_id WHERE w.book_id=? AND l.review_time>=?""", (book_id, to_db(today - timedelta(days=6))))
    log_days: dict[str, dict[str, int]] = {}
    for item in recent_logs:
        day = from_db(item["review_time"]).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
        agg = log_days.setdefault(day, {"new": 0, "review": 0, "correct": 0, "total": 0})
        agg["total"] += 1
        agg["correct"] += 1 if item["is_correct"] else 0
    first_map = {row["id"]: row["first_time"] for row in first_rows if row["first_time"]}
    chart = []
    for offset in range(6, -1, -1):
        day = (today - timedelta(days=offset)).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
        agg = log_days.get(day, {"new": 0, "review": 0, "correct": 0, "total": 0})
        agg["new"] = sum(1 for first_time in first_map.values() if from_db(first_time).astimezone(LOCAL_TZ).strftime("%Y-%m-%d") == day)
        agg["review"] = max(0, agg["total"] - agg["new"])
        chart.append({"date": day, **agg})
    days_rows = await db_all(request, """SELECT DISTINCT date(l.review_time,'+8 hours') AS d
        FROM review_logs l JOIN words w ON w.id=l.word_id WHERE w.book_id=? ORDER BY d DESC""", (book_id,))
    days = [row["d"] for row in days_rows]
    streak, cursor = 0, today.astimezone(LOCAL_TZ).date()
    if days and days[0] != str(cursor):
        cursor -= timedelta(days=1)
    day_set = set(days)
    while str(cursor) in day_set:
        streak += 1
        cursor -= timedelta(days=1)
    plan = await _get_plan(request, book_id)
    target = plan * {"day": 1, "week": 7, "month": 30}.get(period, 1)
    return {
        "range": period, "new": len(new_ids), "review": max(0, len(logs) - len(new_ids)),
        "accuracy": round(sum(1 for item in logs if item["is_correct"]) / len(logs), 4) if logs else 0.0,
        "total_words": total_words, "mastered": mastered, "due": due, "errors": errors,
        "streak": streak, "plan": plan, "target": target,
        "completion": round(len(new_ids) / target, 4) if target else 0.0, "chart": chart,
    }


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _split_lines(value: str) -> str:
    return "\n".join(item.strip() for item in re.split(r"[|｜]", value or "") if item.strip())


@app.post("/api/import/csv")
async def import_csv(request: Request, book_id: int = Form(...), file: UploadFile = File(...)):
    await _book_or_404(request, book_id)
    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(400, "文件超过 20MB")
    parsed, failed, samples = [], 0, []
    first = True
    for line_no, columns in enumerate(csv.reader(io.StringIO(_decode_text(raw))), 1):
        if not columns or all(not (item or "").strip() for item in columns):
            continue
        if len(columns) < 2:
            failed += 1
            if len(samples) < 5:
                samples.append(f"第 {line_no} 行不是 word,meaning 两列")
            continue
        word, meaning = columns[0].strip(), columns[1].strip()
        if first and word.lower() in ("word", "单词", "词"):
            first = False
            continue
        first = False
        if not word or not meaning:
            failed += 1
            continue
        parsed.append((word, meaning, columns[2].strip() if len(columns) > 2 else "", columns[3].strip() if len(columns) > 3 else "", _clean_pos(columns[4].strip() if len(columns) > 4 else "")))
    if len(parsed) > 500:
        raise HTTPException(400, "云端单次最多导入 500 行，请将 CSV 拆分后分批导入")
    existing_rows = await db_all(request, "SELECT id,word FROM words WHERE book_id=?", (book_id,))
    existing = {row["word"]: row["id"] for row in existing_rows}
    unique_new = []
    seen = set(existing)
    for item in parsed:
        if item[0] not in seen:
            seen.add(item[0])
            unique_new.append(item)
    for start in range(0, len(unique_new), 30):
        chunk = unique_new[start:start + 30]
        marks = ",".join("(?,?,?)" for _ in chunk)
        params = [value for item in chunk for value in (book_id, item[0], item[1])]
        await db_run(request, f"INSERT OR IGNORE INTO words(book_id,word,meaning) VALUES {marks}", params)
    current_rows = await db_all(request, "SELECT id,word FROM words WHERE book_id=?", (book_id,))
    current = {row["word"]: row["id"] for row in current_rows}
    detail_items = [(current[item[0]], _split_lines(item[2]), _split_lines(item[3]), item[4]) for item in parsed if (item[2] or item[3] or item[4]) and item[0] in current]
    for start in range(0, len(detail_items), 20):
        chunk = detail_items[start:start + 20]
        marks = ",".join("(?,?,?,?,CURRENT_TIMESTAMP)" for _ in chunk)
        params = [value for item in chunk for value in item]
        await db_run(request, f"""INSERT INTO word_details(word_id,phrases,examples,pos,update_time) VALUES {marks}
            ON CONFLICT(word_id) DO UPDATE SET
            phrases=CASE WHEN COALESCE(word_details.phrases,'')='' THEN excluded.phrases ELSE word_details.phrases END,
            examples=CASE WHEN COALESCE(word_details.examples,'')='' THEN excluded.examples ELSE word_details.examples END,
            pos=CASE WHEN COALESCE(word_details.pos,'')='' THEN excluded.pos ELSE word_details.pos END,
            update_time=CURRENT_TIMESTAMP""", params)
    added = len(unique_new)
    return {"added": added, "skipped": max(0, len(parsed) - added), "updated": len(detail_items), "failed": failed, "messages": samples}


@app.get("/api/export")
async def export_book(book_id: int, request: Request, format: str = "json"):
    book = await _book_or_404(request, book_id)
    words = await db_all(request, "SELECT * FROM words WHERE book_id=? ORDER BY id", (book_id,))
    details = await db_all(request, """SELECT d.word_id,d.phrases,d.examples,d.pos FROM word_details d
        JOIN words w ON w.id=d.word_id WHERE w.book_id=?""", (book_id,))
    detail_map = {row["word_id"]: row for row in details}
    for word in words:
        detail = detail_map.get(word["id"])
        word["phrases"] = detail["phrases"] if detail else ""
        word["examples"] = detail["examples"] if detail else ""
        word["pos"] = ((detail or {}).get("pos") or _pos_from_meaning(word["meaning"]))
    log_count = await db_scalar(request, """SELECT COUNT(*) AS n FROM review_logs l
        JOIN words w ON w.id=l.word_id WHERE w.book_id=?""", (book_id,)) or 0
    if format == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["word", "meaning", "phrases", "examples", "pos", "is_mastered", "is_error", "stability", "difficulty", "reps", "lapses", "next_review_at", "last_review_at"])
        for word in words:
            writer.writerow([word["word"], word["meaning"], (word["phrases"] or "").replace("\n", "|"), (word["examples"] or "").replace("\n", "|"), word["pos"], word["is_mastered"], word["is_error"], word["stability"], word["difficulty"], word["reps"], word["lapses"], word["next_review_at"] or "", word["last_review_at"] or ""])
        return Response(content=buffer.getvalue().encode("utf-8-sig"), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="vocab_book{book_id}.csv"'})
    payload = {"app_version": 2, "exported_at": to_db(now_utc()), "book": book, "words": words, "logs_count": log_count}
    return Response(content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"), media_type="application/json", headers={"Content-Disposition": f'attachment; filename="vocab_book{book_id}.json"'})


AI_PROMPT = """单词：{word}
中文释义：{meaning}

请输出这个词的词性、常用搭配和例句。硬性要求：
1. pos：词性缩写，用 n. / v. / adj. / adv. 等格式
2. phrases：2-3 条常用搭配，每条格式为「英文搭配 —— 中文解释」
3. examples：2-3 条例句，每条格式为「英文句子 —— 中文翻译」
4. 不要编号，不要 Markdown，不要额外解释
5. 只输出 JSON，例如：
{{"pos":"v. / n.","phrases":["look forward to —— 期待"],"examples":["I look forward to seeing you. —— 我期待见到你。"]}}"""


def _ai_result_text(result: Any) -> str:
    data = _to_py(result)
    if not isinstance(data, (str, dict)):
        try:
            from js import JSON

            serialized = JSON.stringify(result)
            if serialized:
                data = json.loads(str(serialized))
        except Exception:
            pass
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("response", "output_text", "text"):
            value = _to_py(data.get(key))
            if isinstance(value, str):
                return value
        output = _to_py(data.get("output"))
        if isinstance(output, str):
            return output
        choices = _to_py(data.get("choices")) or []
        if choices:
            first = _to_py(choices[0])
            if isinstance(first, dict):
                message = _to_py(first.get("message")) or {}
                if isinstance(message, dict):
                    content = _to_py(message.get("content"))
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        parts = []
                        for item in content:
                            item = _to_py(item)
                            if isinstance(item, str):
                                parts.append(item)
                            elif isinstance(item, dict) and isinstance(_to_py(item.get("text")), str):
                                parts.append(_to_py(item.get("text")))
                        if parts:
                            return "\n".join(parts)
                    for key in ("reasoning_content", "reasoning"):
                        fallback = _to_py(message.get(key))
                        if isinstance(fallback, str) and fallback.strip():
                            return fallback
                text = _to_py(first.get("text"))
                if isinstance(text, str):
                    return text
    try:
        if isinstance(result.response, str):
            return result.response
    except Exception:
        pass
    return ""


async def _ai_chat(request: Request, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> str:
    try:
        result = await request.scope["env"].AI.run(AI_MODEL, {
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
            "reasoning_effort": "low",
        })
    except Exception as exc:
        message = str(exc)
        if "3036" in message or "allocation" in message.lower() or "limit" in message.lower():
            raise HTTPException(503, "今天的免费 AI 额度已用完，明天会自动恢复；背词和复习仍可正常使用") from exc
        raise HTTPException(503, f"Workers AI 暂时不可用：{message}") from exc
    text = _ai_result_text(result).strip()
    if not text:
        raise HTTPException(502, "模型返回为空，请稍后重试")
    return text


async def _ai_generate(request: Request, word: str, meaning: str) -> dict[str, Any]:
    text = await _ai_chat(request, [
        {"role": "system", "content": "你是英语学习词典助手，只输出合法 JSON。"},
        {"role": "user", "content": AI_PROMPT.format(word=word, meaning=meaning)},
    ], 1000, 0.2)
    match = re.search(r"\{.*\}", text, re.S)
    try:
        payload = json.loads(match.group(0) if match else text)
    except Exception as exc:
        raise HTTPException(502, "模型没有返回合法 JSON，请重试") from exc

    def clean(key: str) -> list[str]:
        values = payload.get(key) or []
        if isinstance(values, str):
            values = [values]
        return [value.strip()[:240] for value in values if isinstance(value, str) and value.strip()][:3]

    phrases, examples = clean("phrases"), clean("examples")
    raw_pos = payload.get("pos") or ""
    if isinstance(raw_pos, list):
        raw_pos = " / ".join(str(item) for item in raw_pos)
    if not phrases and not examples:
        raise HTTPException(502, "模型返回内容为空，请重试")
    return {"pos": _clean_pos(str(raw_pos)), "phrases": phrases, "examples": examples}


async def _save_detail(request: Request, word_id: int, generated: dict[str, Any]) -> None:
    await db_run(
        request,
        """INSERT INTO word_details(word_id,phrases,examples,pos,update_time)
        VALUES(?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(word_id) DO UPDATE SET
        phrases=excluded.phrases,examples=excluded.examples,pos=excluded.pos,update_time=CURRENT_TIMESTAMP""",
        (word_id, "\n".join(generated["phrases"]), "\n".join(generated["examples"]), generated["pos"]),
    )


@app.get("/api/ai/status")
async def ai_status():
    return {"available": True, "base": "Cloudflare Workers AI", "model": AI_MODEL, "model_ready": True, "models": [AI_MODEL]}


class AIWordIn(BaseModel):
    word_id: int
    save: bool = True


@app.post("/api/ai/usage")
async def ai_usage(body: AIWordIn, request: Request):
    word = await db_one(request, "SELECT id,word,meaning FROM words WHERE id=?", (body.word_id,))
    if not word:
        raise HTTPException(404, "单词不存在")
    generated = await _ai_generate(request, word["word"], word["meaning"])
    if body.save:
        await _save_detail(request, word["id"], generated)
    return {
        "word_id": word["id"], "word": word["word"], "model": AI_MODEL,
        "pos": generated["pos"], "phrases": "\n".join(generated["phrases"]),
        "examples": "\n".join(generated["examples"]), "saved": body.save,
    }


class AIBookIn(BaseModel):
    book_id: int
    limit: int = 30
    only_missing: bool = True
    overwrite: bool = False


@app.post("/api/ai/fill_book")
async def ai_fill_book(body: AIBookIn, request: Request):
    await _book_or_404(request, body.book_id)
    limit = max(1, min(100, body.limit))
    sql = "SELECT w.id FROM words w WHERE w.book_id=? "
    if body.only_missing and not body.overwrite:
        sql += "AND NOT EXISTS (SELECT 1 FROM word_details d WHERE d.word_id=w.id) "
    sql += "ORDER BY w.id LIMIT ?"
    rows = await db_all(request, sql, (body.book_id, limit))
    if not rows:
        raise HTTPException(400, "没有需要生成的词（可能都已经有用法了）")
    job_id = uuid.uuid4().hex
    await db_run(
        request,
        "INSERT INTO ai_jobs(id,book_id,model,target_ids,done,failed,status,message) VALUES(?,?,?,?,0,0,'running','')",
        (job_id, body.book_id, AI_MODEL, json.dumps([row["id"] for row in rows])),
    )
    return {"job_id": job_id, "total": len(rows)}


@app.get("/api/ai/jobs/{job_id}")
async def ai_job(job_id: str, request: Request):
    job = await db_one(request, "SELECT * FROM ai_jobs WHERE id=?", (job_id,))
    if not job:
        raise HTTPException(404, "任务不存在或已过期")
    targets = json.loads(job["target_ids"])
    index = int(job["done"]) + int(job["failed"])
    if job["status"] == "running" and index < len(targets):
        word = await db_one(request, "SELECT id,word,meaning FROM words WHERE id=?", (targets[index],))
        if not word:
            await db_run(request, "UPDATE ai_jobs SET failed=failed+1,message=? WHERE id=?", ("单词已被删除", job_id))
        else:
            await db_run(request, "UPDATE ai_jobs SET message=? WHERE id=?", (word["word"], job_id))
            try:
                generated = await _ai_generate(request, word["word"], word["meaning"])
                await _save_detail(request, word["id"], generated)
                await db_run(request, "UPDATE ai_jobs SET done=done+1,message=? WHERE id=?", (word["word"], job_id))
            except HTTPException as exc:
                await db_run(request, "UPDATE ai_jobs SET failed=failed+1,message=? WHERE id=?", (f"{word['word']}：{exc.detail}", job_id))
        job = await db_one(request, "SELECT * FROM ai_jobs WHERE id=?", (job_id,))
    processed = int(job["done"]) + int(job["failed"])
    if processed >= len(targets) and job["status"] == "running":
        await db_run(request, "UPDATE ai_jobs SET status='finished',message='' WHERE id=?", (job_id,))
        job["status"], job["message"] = "finished", ""
    return {
        "id": job["id"], "book_id": job["book_id"], "total": len(targets),
        "done": int(job["done"]), "failed": int(job["failed"]),
        "status": job["status"], "message": job["message"],
    }


STORY_STYLES = {
    "story": ("一个连贯的生活场景小故事", "请用第三人称写一个小故事，有具体场景和情绪"),
    "dialogue": ("两个人的日常对话", "请写成对话体：每句前面用 A: / B: 标注说话人"),
}


class StoryIn(BaseModel):
    book_id: int
    day: str | None = None
    style: str = "story"
    length: int = Field(default=200, ge=50, le=800)
    save: bool = True


def _story_prompt(words: list[dict], style: str, length: int) -> str:
    style_name, style_hint = STORY_STYLES.get(style, STORY_STYLES["story"])
    listed = "\n".join(f"- {word['word']}：{word['meaning']}" for word in words)
    return f"""下面是学生今天背过的单词：
{listed}

请写一段{style_name}：
1. 英文正文不少于 {length} 个英文单词，目标 {length}–{length + 40} 词
2. 尽量使用全部单词，可以做时态或单复数变化
3. {style_hint}
4. 分成 2–4 段；每段英文后另起一行，以「中文：」开头写整段翻译
5. 不要标题、解释或 Markdown 符号"""


def _story_word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z][A-Za-z'-]*", text))


@app.post("/api/ai/story")
async def ai_story(body: StoryIn, request: Request):
    if body.style not in STORY_STYLES:
        raise HTTPException(400, "style 只能是 story 或 dialogue")
    day = _valid_date(body.day or _local_today(), "day")
    await _book_or_404(request, body.book_id)
    items = await _day_items(request, body.book_id, day)
    if not items:
        raise HTTPException(400, "这一天还没有背过的单词")
    words = [{"word": item["word"], "meaning": item["meaning"]} for item in items[:30]]
    content = await _ai_chat(request, [
        {"role": "system", "content": "你是英语写作老师。正文必须是英文，只在“中文：”行写中文翻译。"},
        {"role": "user", "content": _story_prompt(words, body.style, body.length)},
    ], min(4096, int(body.length * 5) + 800), 0.6)
    if _story_word_count(content) < 20:
        raise HTTPException(502, "模型没有生成有效的英文正文，请重试")
    content = content.replace("【中】", "中文：").replace("中文：中文：", "中文：")
    content = re.sub(r"(?<=\S)中文：", "\n中文：", content)
    used = [word["word"] for word in words]
    if body.save:
        await db_run(request, """INSERT INTO daily_writings(book_id,day,style,target_len,content,word_list)
            VALUES(?,?,?,?,?,?)""", (body.book_id, day, body.style, body.length, content, ",".join(used)))
    return {
        "day": day, "style": body.style, "length": body.length, "model": AI_MODEL,
        "words": used, "content": content, "word_count": _story_word_count(content),
    }


@app.get("/api/ai/story")
async def ai_story_list(book_id: int, request: Request, day: str | None = None, limit: int = 5):
    await _book_or_404(request, book_id)
    day = _valid_date(day or _local_today(), "day")
    return await db_all(request, """SELECT id,day,style,target_len,word_list,create_time,
        substr(content,1,120) AS preview FROM daily_writings WHERE book_id=? AND day=?
        ORDER BY id DESC LIMIT ?""", (book_id, day, max(1, min(20, limit))))


@app.get("/api/ai/story/{writing_id}")
async def ai_story_one(writing_id: int, request: Request):
    row = await db_one(request, "SELECT * FROM daily_writings WHERE id=?", (writing_id,))
    if not row:
        raise HTTPException(404, "找不到这篇短文")
    return row


@app.get("/{path:path}")
async def frontend(path: str, request: Request):
    env = request.scope["env"]
    asset_url = f"https://assets.local/{path}"
    response = await env.ASSETS.fetch(asset_url)
    body = await response.bytes()
    return Response(content=body, status_code=response.status, headers=response.headers)


Default = asgi.entrypoint(app)
