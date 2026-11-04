"""WowPoll (wowpoll.com) - dead-simple polls for streamers.

Storage:
  - DATABASE_URL set (postgres://...)  -> Postgres (production, e.g. Neon on FastAPI Cloud)
  - otherwise                          -> local SQLite file (development)

Run locally:  python -m uvicorn app:app --reload
"""
import hashlib
import hmac
import ipaddress
import json
import os
import re
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


def _load_local_env() -> None:
    """Local dev: read .env.local / .env (written by `neon env pull`). Real environment
    variables always win, so hosted deployments are unaffected."""
    for name in (".env.local", ".env"):
        path = BASE / name
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


_load_local_env()

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))
SQLITE_PATH = os.environ.get("DB_PATH", str(BASE / "polls.db"))

# Client IP detection behind proxies. X-Forwarded-For is built left to right: anything a
# client sends comes first, then each proxy appends the address it received from. So we
# walk it from the RIGHT, skipping our own infrastructure (private addresses and
# Cloudflare, which fronts FastAPI Cloud); the first other address is the real client.
# Set PROXY_HOPS=<n> to instead take the n-th entry from the right.
ON_FASTAPI_CLOUD = "FASTAPICLOUD_DEPLOYMENT_ID" in os.environ
TRUST_PROXY_CHAIN = os.environ.get("TRUST_PROXY_CHAIN", "1" if ON_FASTAPI_CLOUD else "0") == "1"
PROXY_HOPS = int(os.environ.get("PROXY_HOPS", "0"))

# https://www.cloudflare.com/ips/
CLOUDFLARE_NETS = [ipaddress.ip_network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
)]
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
        # DATABASE_URL is Neon's pooled (PgBouncer, transaction mode) URL: no
        # server-side prepared statements, no session state.
        kwargs={"autocommit": True, "row_factory": dict_row, "prepare_threshold": None},
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

    @contextmanager
    def tx(self):
        """All-or-nothing block of writes."""
        if IS_PG:
            with self.raw.transaction():
                yield
        else:
            self.q("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.q("ROLLBACK")
                raise
            self.q("COMMIT")


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
    # Tier lists: chat ranks items into tiers once each; results are average placement.
    """CREATE TABLE IF NOT EXISTS tierlists (
        id          TEXT PRIMARY KEY,
        title       TEXT NOT NULL,
        items       TEXT NOT NULL,
        tiers       TEXT NOT NULL,
        admin_token TEXT NOT NULL,
        closed      INTEGER NOT NULL DEFAULT 0,
        created_at  BIGINT NOT NULL
    )""",
    # One row per voter; the UNIQUE constraint is what enforces "one ranking each".
    """CREATE TABLE IF NOT EXISTS tier_voters (
        list_id    TEXT NOT NULL,
        voter_id   TEXT NOT NULL,
        created_at BIGINT NOT NULL,
        UNIQUE (list_id, voter_id)
    )""",
    """CREATE TABLE IF NOT EXISTS tier_placements (
        list_id  TEXT NOT NULL,
        voter_id TEXT NOT NULL,
        item_idx INTEGER NOT NULL,
        tier_idx INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS tier_placements_list ON tier_placements (list_id, item_idx)",
    "CREATE INDEX IF NOT EXISTS tier_placements_voter ON tier_placements (list_id, voter_id)",
]


def _setup(c: Conn) -> str:
    for stmt in SCHEMA:
        c.q(stmt)
    # One secret shared by every replica, generated on first boot.
    c.q(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
        ("ip_secret", secrets.token_hex(32)),
    )
    return c.one("SELECT value FROM settings WHERE key = ?", ("ip_secret",))["value"]


def init_db() -> str:
    """Create tables and return the shared secret used to hash IPs."""
    if IS_PG:
        # Schema changes go over the direct connection (Neon's advice); falls back to
        # DATABASE_URL when no unpooled URL is provided (e.g. some host integrations).
        url = os.environ.get("DATABASE_URL_UNPOOLED") or DATABASE_URL
        with psycopg.connect(url, row_factory=dict_row, prepare_threshold=None) as raw:
            with raw.transaction():
                # Transaction-scoped lock: replicas booting together run setup one at a
                # time, and it is safe even through a transaction-mode pooler.
                raw.execute("SELECT pg_advisory_xact_lock(727001)")
                secret = _setup(Conn(raw))
    else:
        with db() as c:
            c.q("PRAGMA journal_mode=WAL")
            secret = _setup(c)
    return os.environ.get("SECRET_KEY") or secret


SECRET = init_db()


# ---------- helpers ----------

def _is_infrastructure(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or any(addr in net for net in CLOUDFLARE_NETS)
    )


def client_ip(req: Request) -> str:
    fwd = [p.strip() for p in req.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if fwd and PROXY_HOPS > 0:
        return fwd[-PROXY_HOPS] if len(fwd) >= PROXY_HOPS else fwd[0]
    if fwd and TRUST_PROXY_CHAIN:
        for ip in reversed(fwd):
            if not _is_infrastructure(ip):
                return ip
        return fwd[0]
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
    strict_ip: bool = False  # one vote per network; off by default, not exposed in the UI


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


# ---------- tier lists ----------

DEFAULT_TIERS = ["S", "A", "B", "C", "D", "F"]
MAX_TIER_ITEMS = 40
MAX_ITEM_NAME = 40
MAX_TITLE = 120
ICON_RE = re.compile(r"^[a-z0-9_\-]{1,100}$")
try:
    ICONS = set(json.loads((STATIC / "wow-icons" / "index.json").read_text()))
except FileNotFoundError:
    ICONS = set()


class TierItem(BaseModel):
    name: str = Field(max_length=200)
    icon: str | None = None


class CreateTierList(BaseModel):
    title: str = Field(min_length=1, max_length=MAX_TITLE)
    items: list[TierItem]


class Placement(BaseModel):
    item: int
    tier: int


class TierVote(BaseModel):
    placements: list[Placement]


def tier_state(list_id: str, fresh: bool = False) -> dict:
    """Public tier list + community result, cached briefly like poll_state."""
    key = "t:" + list_id
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and not fresh and now - hit[0] < CACHE_TTL:
        return hit[1]
    with db() as c:
        t = c.one("SELECT id, title, items, tiers, closed FROM tierlists WHERE id = ?", (list_id,))
        if not t:
            raise HTTPException(404, "Tier list not found")
        items, tiers = json.loads(t["items"]), json.loads(t["tiers"])
        stats = {
            r["item_idx"]: (float(r["avg"]), int(r["n"]))
            for r in c.all(
                "SELECT item_idx, AVG(tier_idx * 1.0) AS avg, COUNT(*) AS n"
                " FROM tier_placements WHERE list_id = ? GROUP BY item_idx",
                (list_id,),
            )
        }
        voters = int(c.one("SELECT COUNT(*) AS n FROM tier_voters WHERE list_id = ?", (list_id,))["n"])
    results = []
    for i in range(len(items)):
        if i in stats:
            avg, n = stats[i]
            results.append({"avg": round(avg, 3), "votes": n,
                            "tier": min(len(tiers) - 1, max(0, int(avg + 0.5)))})
        else:
            results.append(None)
    state = {"id": t["id"], "title": t["title"], "items": items, "tiers": tiers,
             "closed": bool(t["closed"]), "voters": voters, "results": results}
    if len(_cache) > 10000:
        _cache.clear()
    _cache[key] = (now, state)
    return state


@app.post("/api/tierlists")
def create_tierlist(body: CreateTierList, request: Request):
    if rate_limited(client_ip(request)):
        raise HTTPException(429, "Slow down - too many lists created. Try again later.")
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "Title is required")
    items, seen = [], set()
    for it in body.items:
        name = it.name.strip()[:MAX_ITEM_NAME]
        icon = (it.icon or "").strip().lower() or None
        if icon is not None and (not ICON_RE.match(icon) or (ICONS and icon not in ICONS)):
            icon = None
        if not name and not icon:
            continue
        k = (name.lower(), icon)
        if k in seen:
            continue
        seen.add(k)
        items.append({"name": name, "icon": icon})
    if not 2 <= len(items) <= MAX_TIER_ITEMS:
        raise HTTPException(400, f"Need between 2 and {MAX_TIER_ITEMS} items")

    token = secrets.token_urlsafe(24)
    for _ in range(10):
        tid = new_id()
        try:
            with db() as c:
                c.q(
                    "INSERT INTO tierlists (id, title, items, tiers, admin_token, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (tid, title, json.dumps(items), json.dumps(DEFAULT_TIERS), token, int(time.time())),
                )
            return {"id": tid, "admin_token": token}
        except IntegrityError:
            continue
    raise HTTPException(500, "Could not allocate id")


def _my_placements(list_id: str, vid: str):
    with db() as c:
        if not c.one("SELECT 1 AS x FROM tier_voters WHERE list_id = ? AND voter_id = ?", (list_id, vid)):
            return None
        rows = c.all(
            "SELECT item_idx, tier_idx FROM tier_placements WHERE list_id = ? AND voter_id = ?",
            (list_id, vid),
        )
    return {str(r["item_idx"]): r["tier_idx"] for r in rows}


@app.get("/api/tierlists/{list_id}")
def read_tierlist(list_id: str, request: Request, me: int = 0):
    state = tier_state(list_id)
    if not me:
        return no_store(state)
    return no_store({**state, "mine": _my_placements(list_id, request.state.vid)})


@app.post("/api/tierlists/{list_id}/vote")
def vote_tierlist(list_id: str, body: TierVote, request: Request):
    vid = request.state.vid
    state = tier_state(list_id)
    if state["closed"]:
        raise HTTPException(410, "Voting is closed")
    n_items, n_tiers = len(state["items"]), len(state["tiers"])
    placed = {}
    for p in body.placements:
        if not (0 <= p.item < n_items and 0 <= p.tier < n_tiers):
            raise HTTPException(400, "Invalid placement")
        placed[p.item] = p.tier
    if not placed:
        raise HTTPException(400, "Place at least one item")

    now = int(time.time())
    try:
        with db() as c, c.tx():
            c.q("INSERT INTO tier_voters (list_id, voter_id, created_at) VALUES (?, ?, ?)",
                (list_id, vid, now))
            for item, tier in placed.items():
                c.q("INSERT INTO tier_placements (list_id, voter_id, item_idx, tier_idx) VALUES (?, ?, ?, ?)",
                    (list_id, vid, item, tier))
    except IntegrityError:
        raise HTTPException(409, "You already submitted a ranking for this list")
    mine = {str(k): v for k, v in placed.items()}
    return no_store({**tier_state(list_id, fresh=True), "mine": mine})


@app.post("/api/tierlists/{list_id}/admin")
def admin_tierlist(list_id: str, body: Admin):
    with db() as c:
        t = c.one("SELECT admin_token FROM tierlists WHERE id = ?", (list_id,))
        if not t:
            raise HTTPException(404, "Tier list not found")
        if not hmac.compare_digest(t["admin_token"], body.admin_token):
            raise HTTPException(403, "Not your tier list")
        c.q("UPDATE tierlists SET closed = ? WHERE id = ?", (int(body.closed), list_id))
    _cache.pop("t:" + list_id, None)
    return {"ok": True, "closed": body.closed}


if DEBUG_IP:
    @app.get("/api/debug/ip")
    def debug_ip(request: Request):
        """Temporary: set DEBUG_IP=1 to check which IP the app sees behind the proxy."""
        return {
            "chosen_ip": client_ip(request),
            "mode": f"hops={PROXY_HOPS}" if PROXY_HOPS else ("chain" if TRUST_PROXY_CHAIN else "socket"),
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


@app.get("/tier")
def tier_create_page():
    return FileResponse(STATIC / "tier_create.html")


@app.get("/t/{list_id}")
def tier_page(list_id: str):
    return FileResponse(STATIC / "tier.html")


@app.get("/t/{list_id}/overlay")
def tier_overlay_page(list_id: str):
    return FileResponse(STATIC / "tier_overlay.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "icons" / "favicon.ico", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/healthz")
def health():
    return {"ok": True, "db": "postgres" if IS_PG else "sqlite"}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
