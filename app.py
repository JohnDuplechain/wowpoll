"""WowPoll (wowpoll.com) - dead-simple polls for streamers.

Storage:
  - DATABASE_URL set (postgres://...)  -> Postgres (production, e.g. Neon on FastAPI Cloud)
  - otherwise                          -> local SQLite file (development)

Run locally:  python -m uvicorn app:app --reload
"""
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
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

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.environ.get("DB_PATH", str(BASE / "polls.db"))

# How many reverse proxies sit in front of the app. The client IP is taken that many
# entries from the right of X-Forwarded-For (the right side is added by trusted proxies,
# the left side can be faked by the client). Defaults to 1 on FastAPI Cloud, 0 locally.
ON_FASTAPI_CLOUD = "FASTAPICLOUD_DEPLOYMENT_ID" in os.environ
PROXY_HOPS = int(os.environ.get("PROXY_HOPS", "1" if ON_FASTAPI_CLOUD else "0"))
DEBUG_IP = os.environ.get("DEBUG_IP") == "1"

MAX_OPTIONS = 10
MAX_QUESTION = 200
MAX_OPTION = 80
CREATE_LIMIT_PER_HOUR = 30
CACHE_TTL = 1.0  # seconds; results polling is served from memory within this window

ID_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alike chars

app = FastAPI(title="WowPoll", docs_url=None, redoc_url=None)


# ---------- database ----------

if IS_PG:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    _pool = ConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=int(os.environ.get("DB_POOL_SIZE", "10")),
        kwargs={"autocommit": True, "row_factory": dict_row},
        check=ConnectionPool.check_connection,  # survive Neon idling connections
        open=True,
    )
    IntegrityError = psycopg.IntegrityError
else:
    IntegrityError = sqlite3.IntegrityError


class Conn:
    """Tiny wrapper so the same SQL (with ? placeholders) runs on SQLite and Postgres."""

    def __init__(self, raw):
        self.raw = raw

    def q(self, sql: str, params: tuple = ()):
        if IS_PG:
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)

    def one(self, sql: str, params: tuple = ()):
        return self.q(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()):
        return self.q(sql, params).fetchall()


def _dict_factory(cursor, row):
    return {d[0]: row[i] for i, d in enumerate(cursor.description)}


@contextmanager
def db():
    if IS_PG:
        with _pool.connection() as conn:
            yield Conn(conn)
    else:
        conn = sqlite3.connect(SQLITE_PATH, timeout=10, isolation_level=None)
        conn.row_factory = _dict_factory
        try:
            yield Conn(conn)
        finally:
            conn.close()


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS polls (
        id          TEXT PRIMARY KEY,
        question    TEXT NOT NULL,
        options     TEXT NOT NULL,
        admin_token TEXT NOT NULL,
        strict_ip   INTEGER NOT NULL DEFAULT 1,
        closed      INTEGER NOT NULL DEFAULT 0,
        created_at  BIGINT NOT NULL
    )""",
    # ip_key is the IP hash for strict polls and NULL otherwise. NULLs never collide in a
    # UNIQUE constraint, so the database itself enforces "one vote per browser" and
    # (for strict polls) "one vote per network", even with many app replicas.
    """CREATE TABLE IF NOT EXISTS votes (
        poll_id    TEXT NOT NULL,
        option_idx INTEGER NOT NULL,
        voter_id   TEXT NOT NULL,
        ip_key     TEXT,
        created_at BIGINT NOT NULL,
        UNIQUE (poll_id, voter_id),
        UNIQUE (poll_id, ip_key)
    )""",
    "CREATE INDEX IF NOT EXISTS votes_poll_option ON votes (poll_id, option_idx)",
    """CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
]


def init_db() -> str:
    """Create tables and return the shared secret used to hash IPs."""
    with db() as c:
        if IS_PG:
            c.q("SELECT pg_advisory_lock(727001)")  # replicas may boot at the same time
        else:
            c.q("PRAGMA journal_mode=WAL")
        try:
            for stmt in SCHEMA:
                c.q(stmt)
            # One secret shared by every replica, generated on first boot.
            c.q(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
                ("ip_secret", secrets.token_hex(32)),
            )
            secret = c.one("SELECT value FROM settings WHERE key = ?", ("ip_secret",))["value"]
        finally:
            if IS_PG:
                c.q("SELECT pg_advisory_unlock(727001)")
    return os.environ.get("SECRET_KEY") or secret


SECRET = init_db()


# ---------- helpers ----------

def client_ip(req: Request) -> str:
    if PROXY_HOPS > 0:
        fwd = [p.strip() for p in req.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if fwd:
            return fwd[-PROXY_HOPS] if len(fwd) >= PROXY_HOPS else fwd[0]
    return req.client.host if req.client else "unknown"


def ip_hash(poll_id: str, ip: str) -> str:
    return hmac.new(SECRET.encode(), f"{poll_id}|{ip}".encode(), hashlib.sha256).hexdigest()[:32]


def new_id(n: int = 6) -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(n))


_cache: dict[str, tuple[float, dict]] = {}


def poll_state(poll_id: str, fresh: bool = False) -> dict:
    """Public poll data + counts, cached briefly so thousands of viewers polling
    for live results don't each hit the database."""
    now = time.monotonic()
    hit = _cache.get(poll_id)
    if hit and not fresh and now - hit[0] < CACHE_TTL:
        return hit[1]
    with db() as c:
        p = c.one("SELECT id, question, options, closed FROM polls WHERE id = ?", (poll_id,))
        if not p:
            raise HTTPException(404, "Poll not found")
        options = json.loads(p["options"])
        counts = [0] * len(options)
        for r in c.all(
            "SELECT option_idx, COUNT(*) AS n FROM votes WHERE poll_id = ? GROUP BY option_idx",
            (poll_id,),
        ):
            if 0 <= r["option_idx"] < len(options):
                counts[r["option_idx"]] = int(r["n"])
    state = {
        "id": p["id"],
        "question": p["question"],
        "options": options,
        "counts": counts,
        "total": sum(counts),
        "closed": bool(p["closed"]),
    }
    if len(_cache) > 10000:
        _cache.clear()
    _cache[poll_id] = (now, state)
    return state


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
            secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
        )
    return response


def no_store(data: dict) -> JSONResponse:
    return JSONResponse(data, headers={"Cache-Control": "no-store"})


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
    seen, options = set(), []
    for o in body.options:
        o = o.strip()[:MAX_OPTION]
        if o and o.lower() not in seen:
            seen.add(o.lower())
            options.append(o)
    if not question:
        raise HTTPException(400, "Question is required")
    if not 2 <= len(options) <= MAX_OPTIONS:
        raise HTTPException(400, f"Need between 2 and {MAX_OPTIONS} different options")

    token = secrets.token_urlsafe(24)
    for _ in range(10):
        pid = new_id()
        try:
            with db() as c:
                c.q(
                    "INSERT INTO polls (id, question, options, admin_token, strict_ip, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (pid, question, json.dumps(options), token, int(body.strict_ip), int(time.time())),
                )
            return {"id": pid, "admin_token": token}
        except IntegrityError:
            continue
    raise HTTPException(500, "Could not allocate poll id")


@app.get("/api/polls/{poll_id}")
def read_poll(poll_id: str, request: Request, me: int = 0):
    """me=1 also returns which option this browser voted for (one extra query).
    Live-result polling leaves it off so it's served from cache."""
    state = poll_state(poll_id)
    if not me:
        return no_store(state)
    with db() as c:
        mine = c.one(
            "SELECT option_idx FROM votes WHERE poll_id = ? AND voter_id = ?",
            (poll_id, request.state.vid),
        )
    return no_store({**state, "voted": mine["option_idx"] if mine else None})


@app.post("/api/polls/{poll_id}/vote")
def vote(poll_id: str, body: Vote, request: Request):
    vid = request.state.vid
    with db() as c:
        p = c.one("SELECT options, closed, strict_ip FROM polls WHERE id = ?", (poll_id,))
    if not p:
        raise HTTPException(404, "Poll not found")
    if p["closed"]:
        raise HTTPException(410, "Voting is closed")
    if not 0 <= body.option < len(json.loads(p["options"])):
        raise HTTPException(400, "Invalid option")

    ip_key = ip_hash(poll_id, client_ip(request)) if p["strict_ip"] else None
    try:
        with db() as c:
            c.q(
                "INSERT INTO votes (poll_id, option_idx, voter_id, ip_key, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (poll_id, body.option, vid, ip_key, int(time.time())),
            )
    except IntegrityError:
        with db() as c:
            same_browser = c.one(
                "SELECT 1 AS x FROM votes WHERE poll_id = ? AND voter_id = ?", (poll_id, vid)
            )
        if same_browser:
            raise HTTPException(409, "You already voted in this poll")
        raise HTTPException(409, "Someone on your network already voted in this poll")

    return no_store({**poll_state(poll_id, fresh=True), "voted": body.option})


@app.post("/api/polls/{poll_id}/admin")
def admin(poll_id: str, body: Admin):
    with db() as c:
        p = c.one("SELECT admin_token FROM polls WHERE id = ?", (poll_id,))
        if not p:
            raise HTTPException(404, "Poll not found")
        if not hmac.compare_digest(p["admin_token"], body.admin_token):
            raise HTTPException(403, "Not your poll")
        c.q("UPDATE polls SET closed = ? WHERE id = ?", (int(body.closed), poll_id))
    _cache.pop(poll_id, None)
    return {"ok": True, "closed": body.closed}


if DEBUG_IP:
    @app.get("/api/debug/ip")
    def debug_ip(request: Request):
        """Temporary: set DEBUG_IP=1 to check which IP the app sees behind the proxy."""
        return {
            "chosen_ip": client_ip(request),
            "proxy_hops": PROXY_HOPS,
            "socket_ip": request.client.host if request.client else None,
            "x_forwarded_for": request.headers.get("x-forwarded-for"),
            "x_real_ip": request.headers.get("x-real-ip"),
        }


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
    return {"ok": True, "db": "postgres" if IS_PG else "sqlite"}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
