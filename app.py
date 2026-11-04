"""WowPoll (wowpoll.com) - dead-simple polls for streamers.

Run locally:  python -m uvicorn app:app --reload
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).parent
STATIC = BASE / "static"
DB_PATH = os.environ.get("DB_PATH", str(BASE / "polls.db"))
# Secret used to hash IPs so raw addresses are never stored.
SECRET = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
# Set TRUST_PROXY=1 when running behind a reverse proxy (Render, Fly, Railway, nginx...).
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"

MAX_OPTIONS = 10
MAX_QUESTION = 200
MAX_OPTION = 80
CREATE_LIMIT_PER_HOUR = 30

ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alike chars

app = FastAPI(title="WowPoll", docs_url=None, redoc_url=None)
_db_lock = threading.Lock()


# ---------- database ----------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS polls (
                id          TEXT PRIMARY KEY,
                question    TEXT NOT NULL,
                options     TEXT NOT NULL,
                admin_token TEXT NOT NULL,
                strict_ip   INTEGER NOT NULL DEFAULT 1,
                closed      INTEGER NOT NULL DEFAULT 0,
                created_at  INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS votes (
                poll_id    TEXT NOT NULL,
                option_idx INTEGER NOT NULL,
                voter_id   TEXT NOT NULL,
                ip_hash    TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE (poll_id, voter_id)
            );
            CREATE INDEX IF NOT EXISTS votes_poll_ip ON votes (poll_id, ip_hash);
            """
        )


init_db()


# ---------- helpers ----------

def client_ip(req: Request) -> str:
    if TRUST_PROXY:
        fwd = req.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return req.client.host if req.client else "unknown"


def ip_hash(poll_id: str, ip: str) -> str:
    return hmac.new(SECRET.encode(), f"{poll_id}|{ip}".encode(), hashlib.sha256).hexdigest()[:32]


def new_id(n: int = 6) -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(n))


def get_poll(c: sqlite3.Connection, poll_id: str) -> sqlite3.Row:
    row = c.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Poll not found")
    return row


def counts_for(c: sqlite3.Connection, poll_id: str, n_options: int) -> list[int]:
    counts = [0] * n_options
    for r in c.execute(
        "SELECT option_idx, COUNT(*) AS n FROM votes WHERE poll_id = ? GROUP BY option_idx",
        (poll_id,),
    ):
        if 0 <= r["option_idx"] < n_options:
            counts[r["option_idx"]] = r["n"]
    return counts


_create_log: dict[str, deque] = defaultdict(deque)


def rate_limited(ip: str) -> bool:
    now = time.time()
    q = _create_log[ip]
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= CREATE_LIMIT_PER_HOUR:
        return True
    q.append(now)
    return False


# Every visitor gets an anonymous, random voter cookie.
@app.middleware("http")
async def voter_cookie(request: Request, call_next):
    vid = request.cookies.get("vid")
    fresh = not vid or len(vid) != 32
    if fresh:
        vid = secrets.token_hex(16)
    request.state.vid = vid
    response = await call_next(request)
    if fresh:
        response.set_cookie(
            "vid", vid, max_age=60 * 60 * 24 * 365 * 2, httponly=True, samesite="lax",
            secure=request.url.scheme == "https",
        )
    return response


# ---------- API ----------

class CreatePoll(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)
    options: list[str]
    strict_ip: bool = True


class Vote(BaseModel):
    option: int


class Admin(BaseModel):
    admin_token: str
    closed: bool


@app.post("/api/polls")
def create_poll(body: CreatePoll, request: Request):
    if rate_limited(client_ip(request)):
        raise HTTPException(429, "Slow down - too many polls created. Try again later.")
    question = body.question.strip()
    options = [o.strip()[:MAX_OPTION] for o in body.options if o.strip()]
    # drop duplicates, keep order
    seen, uniq = set(), []
    for o in options:
        if o.lower() not in seen:
            seen.add(o.lower())
            uniq.append(o)
    if not question:
        raise HTTPException(400, "Question is required")
    if not 2 <= len(uniq) <= MAX_OPTIONS:
        raise HTTPException(400, f"Need between 2 and {MAX_OPTIONS} different options")

    token = secrets.token_urlsafe(24)
    with _db_lock, db() as c:
        for _ in range(10):
            pid = new_id()
            try:
                c.execute(
                    "INSERT INTO polls (id, question, options, admin_token, strict_ip, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, question, json.dumps(uniq), token, int(body.strict_ip), int(time.time())),
                )
                break
            except sqlite3.IntegrityError:
                continue
        else:
            raise HTTPException(500, "Could not allocate poll id")
    return {"id": pid, "admin_token": token}


@app.get("/api/polls/{poll_id}")
def read_poll(poll_id: str, request: Request):
    with db() as c:
        p = get_poll(c, poll_id)
        options = json.loads(p["options"])
        counts = counts_for(c, poll_id, len(options))
        mine = c.execute(
            "SELECT option_idx FROM votes WHERE poll_id = ? AND voter_id = ?",
            (poll_id, request.state.vid),
        ).fetchone()
    return JSONResponse(
        {
            "id": p["id"],
            "question": p["question"],
            "options": options,
            "counts": counts,
            "total": sum(counts),
            "closed": bool(p["closed"]),
            "voted": mine["option_idx"] if mine else None,
        },
        headers={"Cache-Control": "no-store"},
    )


@app.post("/api/polls/{poll_id}/vote")
def vote(poll_id: str, body: Vote, request: Request):
    vid = request.state.vid
    with _db_lock, db() as c:
        p = get_poll(c, poll_id)
        options = json.loads(p["options"])
        if p["closed"]:
            raise HTTPException(410, "Voting is closed")
        if not 0 <= body.option < len(options):
            raise HTTPException(400, "Invalid option")
        h = ip_hash(poll_id, client_ip(request))
        c.execute("BEGIN IMMEDIATE")
        try:
            if c.execute(
                "SELECT 1 FROM votes WHERE poll_id = ? AND voter_id = ?", (poll_id, vid)
            ).fetchone():
                raise HTTPException(409, "You already voted in this poll")
            if p["strict_ip"] and c.execute(
                "SELECT 1 FROM votes WHERE poll_id = ? AND ip_hash = ?", (poll_id, h)
            ).fetchone():
                raise HTTPException(409, "Someone on your network already voted in this poll")
            c.execute(
                "INSERT INTO votes (poll_id, option_idx, voter_id, ip_hash, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (poll_id, body.option, vid, h, int(time.time())),
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return read_poll(poll_id, request)


@app.post("/api/polls/{poll_id}/admin")
def admin(poll_id: str, body: Admin):
    with _db_lock, db() as c:
        p = get_poll(c, poll_id)
        if not hmac.compare_digest(p["admin_token"], body.admin_token):
            raise HTTPException(403, "Not your poll")
        c.execute("UPDATE polls SET closed = ? WHERE id = ?", (int(body.closed), poll_id))
    return {"ok": True, "closed": body.closed}


# ---------- pages ----------

@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")


@app.get("/p/{poll_id}")
def poll_page(poll_id: str):
    return FileResponse(STATIC / "poll.html")


@app.get("/p/{poll_id}/overlay")
def overlay_page(poll_id: str):
    return FileResponse(STATIC / "overlay.html")


@app.get("/healthz")
def health():
    return {"ok": True}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
