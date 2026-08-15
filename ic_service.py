#!/usr/bin/env python3
"""
ic_service.py — runs on the Mac mini. Does two jobs:

  1. Polls Infinite Campus in the background, caches results to grades.json.
     Skips gracefully while the school has the gradebook switched off.

  2. Serves HTTP on port 11435:
       GET  /grades.json  -> the cached scrape
       GET  /ic-status    -> whether grades are live yet
       everything else    -> proxied to Ollama on 11434

Point your existing ngrok tunnel at 11435 instead of 11434 and the dashboard
gets grades on the same origin it already uses for Jarvis. No second tunnel,
no new CORS setup.

  ngrok http 11435

Credentials live in .env next to this file. Never commit it.
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

BASE = "https://paradigmut.infinitecampus.org/campus"
APP_NAME = "paradigm"
OLLAMA = "http://127.0.0.1:11434"
PORT = 11435
POLL_SECONDS = 30 * 60

HERE = Path(__file__).parent
CACHE = HERE / "grades.json"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_state = {"data": None, "error": None, "checked": 0, "live": False}
_lock = threading.Lock()


def load_env():
    env_path = HERE / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    u, p = os.environ.get("IC_USERNAME"), os.environ.get("IC_PASSWORD")
    if not u or not p:
        sys.exit("Missing IC_USERNAME / IC_PASSWORD in .env")
    return u, p


# --------------------------------------------------------------------------
# Infinite Campus
# --------------------------------------------------------------------------

def ic_login(username, password):
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    r = s.get(
        f"{BASE}/verify.jsp",
        params={
            "nonBrowser": "true",
            "username": username,
            "password": password,
            "appName": APP_NAME,
        },
        timeout=20,
    )
    body = r.text.lower()
    if "password-error" in body or "authentication failed" in body:
        raise RuntimeError("bad credentials")
    if not s.cookies.get("JSESSIONID"):
        raise RuntimeError("no session cookie returned")
    return s


def normalise(raw):
    """
    Flatten IC's grades payload into something compact enough to hand to a
    model. Field names below are best-guess until we see a populated
    response -- revisit once gradesEnabled goes true.
    """
    out = {"live": False, "courses": []}
    if not raw:
        return out

    entry = raw[0] if isinstance(raw, list) else raw
    out["live"] = bool(entry.get("gradesEnabled"))
    out["assignmentsLive"] = bool(entry.get("assignmentsEnabled"))

    for course in entry.get("courses") or []:
        tasks = course.get("gradingTasks") or []
        primary = tasks[0] if tasks else {}
        out["courses"].append({
            "name": course.get("courseName"),
            "period": course.get("periodName"),
            "teacher": course.get("teacherDisplay"),
            "roomName": course.get("roomName"),
            "grade": primary.get("progressScore") or primary.get("score"),
            "percent": primary.get("progressPercent") or primary.get("percent"),
            "assignments": [
                {
                    "name": a.get("assignmentName"),
                    "due": a.get("dueDate"),
                    "points": a.get("totalPoints"),
                    "score": a.get("scorePoints"),
                    "missing": bool(a.get("missing")),
                    "late": bool(a.get("late")),
                }
                for a in (course.get("assignments") or [])
            ],
        })
    return out


def poll_once(username, password):
    s = ic_login(username, password)
    r = s.get(f"{BASE}/resources/portal/grades", timeout=20)
    r.raise_for_status()
    data = normalise(r.json())
    data["fetched"] = int(time.time())

    with _lock:
        _state["data"] = data
        _state["error"] = None
        _state["checked"] = int(time.time())
        _state["live"] = data["live"]

    CACHE.write_text(json.dumps(data, indent=2))
    return data


def poller():
    username, password = load_env()

    # Reuse a stale cache on startup so the dashboard has something immediately.
    if CACHE.exists():
        try:
            with _lock:
                _state["data"] = json.loads(CACHE.read_text())
        except Exception:
            pass

    while True:
        try:
            d = poll_once(username, password)
            status = "LIVE" if d["live"] else "gradebook off"
            print(f"[ic] {time.strftime('%H:%M')} ok — {status}, {len(d['courses'])} courses")
        except Exception as e:
            with _lock:
                _state["error"] = str(e)
            print(f"[ic] {time.strftime('%H:%M')} failed — {e}")
            # Back off hard on auth failures; IC shows a CAPTCHA after ~5.
            if "credentials" in str(e):
                print("[ic] auth problem — pausing 6h to avoid a lockout")
                time.sleep(6 * 3600)
                continue
        time.sleep(POLL_SECONDS)


# --------------------------------------------------------------------------
# HTTP: grades endpoints + Ollama passthrough
# --------------------------------------------------------------------------

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, ngrok-skip-browser-warning, Authorization",
    "Access-Control-Max-Age": "86400",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # quiet

    def _send(self, code, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in CORS.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        for k, v in CORS.items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/grades.json":
            with _lock:
                data = _state["data"]
            if data is None:
                return self._send(503, {"error": "no data yet", "live": False})
            return self._send(200, data)

        if path == "/ic-status":
            with _lock:
                return self._send(200, {
                    "live": _state["live"],
                    "lastChecked": _state["checked"],
                    "error": _state["error"],
                })

        self._proxy("GET")

    def do_POST(self):
        self._proxy("POST")

    def _proxy(self, method):
        url = OLLAMA + self.path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}

        try:
            r = requests.request(method, url, data=body, headers=headers, timeout=300)
        except requests.RequestException as e:
            return self._send(502, {"error": f"Ollama unreachable: {e}"})

        self._send(r.status_code, r.content, r.headers.get("Content-Type", "application/json"))


def main():
    threading.Thread(target=poller, daemon=True).start()
    print(f"Serving on http://127.0.0.1:{PORT}")
    print("  /grades.json, /ic-status, everything else -> Ollama:11434")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
