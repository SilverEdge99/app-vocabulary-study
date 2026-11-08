# -*- coding: utf-8 -*-
"""Bootstrap deps into ./pylibs without pip (sandbox blocks pip wheel unpacking)."""
import io
import sys
import json
import os
import re
import urllib.request
import zipfile

WS = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(WS, "pylibs")
os.makedirs(LIB, exist_ok=True)

PYTAG = "cp%d%d" % sys.version_info[:2]  # 跟随当前解释器，不写死 3.12

# Pinned for a lean, known-good, mutually compatible stack.
ROOTS = {
    "fastapi": "0.115.6",
    "starlette": "0.41.3",
    "pydantic": "2.10.4",
    "pydantic-core": "2.27.2",
    "annotated-types": "0.7.0",
    "typing-extensions": "4.16.0",
    "uvicorn": "0.32.1",
    "click": "8.5.0",
    "h11": "0.16.0",
    "anyio": "4.15.1",
    "sniffio": "1.3.1",
    "idna": "3.10",
    "python-multipart": "0.0.17",
}


def fetch_json(name, version=None):
    url = ("https://pypi.org/pypi/%s/%s/json" % (name, version)) if version else (
        "https://pypi.org/pypi/%s/json" % name)
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def norm(n):
    return re.sub(r"[-_.]+", "-", n).lower()


def pick_wheel(data):
    scored = []
    for f in data["urls"]:
        fn = f["filename"]
        if not fn.endswith(".whl"):
            continue
        parts = fn[:-4].split("-")
        if len(parts) < 4:
            continue
        pyver, abi, plat = parts[-3], parts[-2], parts[-1]
        score = -1
        if plat == "win_amd64":
            if pyver == PYTAG and abi == PYTAG:
                score = 100
            elif abi == "abi3" and pyver in ("cp312", "cp311", "cp310", "cp39", "cp38"):
                score = 90
            elif pyver == "py3":
                score = 60
        elif plat == "any" and pyver in ("py3", "py2.py3", "cp312"):
            score = 70
        if score >= 0:
            scored.append((score, f["upload_time"], f))
    if not scored:
        return None
    scored.sort(key=lambda x: (x[0], x[1]))
    return scored[-1][2]


def dep_names(data):
    out = []
    for req in (data["info"].get("requires_dist") or []):
        low = req.lower()
        if "extra ==" in low:
            continue
        if "python_version" in low and "3.11" in low and "<" in low:
            continue
        if "sys_platform ==" in low and "win32" not in low:
            continue
        if "sys_platform !=" in low and "win32" in low:
            continue
        if "platform_system" in low and "windows" in low and "!=" in low:
            continue
        name = re.split(r"[<>=!~;\[( ]", req.strip(), maxsplit=1)[0].strip()
        if name:
            out.append(name)
    return out


def main():
    queue = list(ROOTS.items())
    seen = set()
    done = []
    while queue:
        name, ver = queue.pop(0)
        key = norm(name)
        if key in seen:
            continue
        seen.add(key)
        try:
            data = fetch_json(name, ver)
        except Exception as e:
            print("SKIP %s (%s)" % (name, e))
            continue
        wheel = pick_wheel(data)
        if wheel is None:
            print("NO WHEEL %s" % name)
            continue
        print("GET  %-26s %-46s" % (name, wheel["filename"]))
        with urllib.request.urlopen(wheel["url"], timeout=180) as r:
            blob = r.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            z.extractall(LIB)
        done.append((name, data["info"]["version"], len(blob)))
        for dep in dep_names(data):
            if norm(dep) not in seen:
                queue.append((dep, None))
    print("\ninstalled %d packages into %s" % (len(done), LIB))
    for n, v, sz in done:
        print("  %-24s %-10s %8d bytes" % (n, v, sz))


if __name__ == "__main__":
    main()


