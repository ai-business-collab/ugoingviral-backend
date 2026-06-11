"""
User store — SQLite backed (phase 1 of the JSON→Postgres migration).

Why SQLite (and not the old users.json):
  - Indexed email lookup — no more O(n) linear scan of every user per request.
  - ACID transactions — no more torn/half-written files or lost writes under
    concurrency (the corruption hazard the load test reproduced).
  - WAL mode — safe concurrent access from FastAPI's threadpool AND from
    multiple uvicorn worker processes on the same host.

Compatibility contract (DO NOT BREAK — ~15 call sites depend on it):
  The original synchronous API (load_users / save_users / get_user_by_email /
  get_user_by_id / create_user / update_user / verify_password / pwd_context)
  keeps the exact same signatures and return shapes, so no caller had to change.
  Sync route handlers run in FastAPI's threadpool, so these sub-millisecond
  indexed SQLite calls never block the event loop.

Async variants (aiosqlite) — aget_user_by_email / aget_user_by_id /
  acreate_user / aupdate_user / aget_all_users — plus async-bcrypt helpers
  (hash_password_async / verify_password_async) are used by the hot async auth
  handlers (register/login/verify) so password hashing and user I/O never block
  the event loop.

Schema (flexible — arbitrary user fields live in the JSON `data` blob):
  users(id TEXT PRIMARY KEY, email TEXT, data TEXT NOT NULL)
  index idx_users_email ON users(lower(email))
"""
import os
import json
import uuid
import shutil
import sqlite3
import asyncio
import threading
from datetime import datetime

import aiosqlite
from passlib.context import CryptContext

_BASE = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.path.join(_BASE, "..", "users.json")   # legacy source for one-time migration
DB_PATH    = os.path.join(_BASE, "..", "users.db")

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_INIT_LOCK = threading.Lock()
_INITIALIZED = False


# ── Connections ────────────────────────────────────────────────────────────────

_BUSY_TIMEOUT_S = 30.0   # wait up to 30s for the write lock instead of erroring


def _connect() -> sqlite3.Connection:
    """A fresh sync connection (cheap for SQLite). Per-call connections sidestep
    sqlite3's cross-thread restriction — sync handlers run across many threads.

    isolation_level=None puts the driver in autocommit mode so our explicit
    `BEGIN IMMEDIATE`/`COMMIT` blocks behave correctly AND the C-level busy
    handler is active — contended writers WAIT (busy_timeout) and queue instead
    of failing with "database is locked"."""
    conn = sqlite3.connect(DB_PATH, timeout=_BUSY_TIMEOUT_S, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _row_to_user(row) -> dict:
    return json.loads(row["data"])


# ── Schema + one-time migration ─────────────────────────────────────────────────

def _ensure_init() -> None:
    """Create the schema and, on first run, migrate users.json → SQLite.

    Idempotent and multi-process safe: the migration runs inside a BEGIN
    IMMEDIATE write transaction, so when several workers start at once only the
    first acquires the write lock and migrates; the rest see a populated table
    and skip. users.json is backed up before any rows are inserted.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _INIT_LOCK:
        if _INITIALIZED:
            return
        conn = _connect()
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                "  id TEXT PRIMARY KEY,"
                "  email TEXT,"
                "  data TEXT NOT NULL"
                ")"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(lower(email))")

            # Migration critical section — serialized across processes.
            conn.execute("BEGIN IMMEDIATE")
            try:
                count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
                if count == 0 and os.path.exists(USERS_FILE):
                    with open(USERS_FILE, "r", encoding="utf-8") as f:
                        legacy = json.load(f)
                    users = legacy.get("users", []) if isinstance(legacy, dict) else []
                    if users:
                        # BACKUP before writing anything.
                        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
                        backup = f"{USERS_FILE}.bak-{ts}"
                        if not os.path.exists(backup):
                            shutil.copy2(USERS_FILE, backup)
                        for u in users:
                            uid = u.get("id") or str(uuid.uuid4())
                            u["id"] = uid
                            conn.execute(
                                "INSERT OR IGNORE INTO users (id, email, data) VALUES (?, ?, ?)",
                                (uid, (u.get("email") or "").lower().strip(),
                                 json.dumps(u, ensure_ascii=False)),
                            )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        _INITIALIZED = True


# ── Sync API (backward compatible — used by threadpool route handlers + jobs) ────

def load_users() -> dict:
    """Return every user as {"users": [<dict>, ...]} — same shape as the old JSON."""
    _ensure_init()
    conn = _connect()
    try:
        rows = conn.execute("SELECT data FROM users").fetchall()
        return {"users": [json.loads(r["data"]) for r in rows]}
    finally:
        conn.close()


def get_all_users() -> list:
    """All users as a flat list (convenience for callers that don't want the wrapper)."""
    return load_users()["users"]


def save_users(data: dict) -> None:
    """Persist the FULL user set, atomically. The provided list becomes the exact
    table contents — rows absent from it are deleted (callers rely on this to
    delete accounts). Runs in a single transaction, so readers never observe a
    half-written state (no torn writes)."""
    _ensure_init()
    users = (data or {}).get("users", [])
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM users")
        conn.executemany(
            "INSERT OR REPLACE INTO users (id, email, data) VALUES (?, ?, ?)",
            [
                (u.get("id") or str(uuid.uuid4()),
                 (u.get("email") or "").lower().strip(),
                 json.dumps(u, ensure_ascii=False))
                for u in users
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_user_by_email(email: str) -> dict | None:
    _ensure_init()
    if not email:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT data FROM users WHERE lower(email) = ? LIMIT 1",
            (email.lower().strip(),),
        ).fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def get_user_by_id(user_id: str) -> dict | None:
    _ensure_init()
    if not user_id:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT data FROM users WHERE id = ? LIMIT 1", (user_id,)
        ).fetchone()
        return _row_to_user(row) if row else None
    finally:
        conn.close()


def _new_user_dict(email, password_hash, name, company, niche) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "email": email.lower().strip(),
        "name": (name or "").strip(),
        "company": (company or "").strip(),
        "niche": (niche or "").strip(),
        "hashed_password": password_hash,
        "created_at": datetime.utcnow().isoformat(),
        "is_active": True,
    }


def create_user(email: str, password: str, name: str = "", company: str = "", niche: str = "") -> dict:
    _ensure_init()
    user = _new_user_dict(email, pwd_context.hash(password), name, company, niche)
    conn = _connect()
    try:
        # Autocommit mode: a single INSERT commits immediately (tiny lock hold).
        conn.execute(
            "INSERT INTO users (id, email, data) VALUES (?, ?, ?)",
            (user["id"], user["email"], json.dumps(user, ensure_ascii=False)),
        )
    finally:
        conn.close()
    return user


def update_user(user_id: str, updates: dict) -> dict | None:
    """Atomic read-modify-write of a single user row (BEGIN IMMEDIATE serializes
    concurrent writers, so no lost-update race on the same row)."""
    _ensure_init()
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT data FROM users WHERE id = ? LIMIT 1", (user_id,)
        ).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        user = json.loads(row["data"])
        user.update(updates)
        conn.execute(
            "UPDATE users SET email = ?, data = ? WHERE id = ?",
            ((user.get("email") or "").lower().strip(),
             json.dumps(user, ensure_ascii=False), user_id),
        )
        conn.execute("COMMIT")
        return user
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


# ── Async bcrypt (TASK 1 — never block the event loop on hashing) ───────────────

async def hash_password_async(password: str) -> str:
    """bcrypt hash off the event loop (≈250 ms of CPU runs in a worker thread)."""
    return await asyncio.to_thread(pwd_context.hash, password)


async def verify_password_async(plain_password: str, hashed_password: str) -> bool:
    """bcrypt verify off the event loop."""
    return await asyncio.to_thread(pwd_context.verify, plain_password, hashed_password)


# ── Async user API (aiosqlite — used by the hot async auth handlers) ────────────

async def _aensure_init() -> None:
    # Schema/migration are cheap and idempotent; reuse the sync path in a thread.
    if not _INITIALIZED:
        await asyncio.to_thread(_ensure_init)


async def aget_user_by_email(email: str) -> dict | None:
    await _aensure_init()
    if not email:
        return None
    async with aiosqlite.connect(DB_PATH, timeout=30.0, isolation_level=None) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT data FROM users WHERE lower(email) = ? LIMIT 1",
            (email.lower().strip(),),
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row["data"]) if row else None


async def aget_user_by_id(user_id: str) -> dict | None:
    await _aensure_init()
    if not user_id:
        return None
    async with aiosqlite.connect(DB_PATH, timeout=30.0, isolation_level=None) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT data FROM users WHERE id = ? LIMIT 1", (user_id,)
        ) as cur:
            row = await cur.fetchone()
    return json.loads(row["data"]) if row else None


async def aget_all_users() -> list:
    await _aensure_init()
    async with aiosqlite.connect(DB_PATH, timeout=30.0, isolation_level=None) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT data FROM users") as cur:
            rows = await cur.fetchall()
    return [json.loads(r["data"]) for r in rows]


async def acreate_user(email: str, password: str, name: str = "", company: str = "", niche: str = "") -> dict:
    await _aensure_init()
    password_hash = await hash_password_async(password)
    user = _new_user_dict(email, password_hash, name, company, niche)
    async with aiosqlite.connect(DB_PATH, timeout=30.0, isolation_level=None) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            "INSERT INTO users (id, email, data) VALUES (?, ?, ?)",
            (user["id"], user["email"], json.dumps(user, ensure_ascii=False)),
        )
        await db.execute("COMMIT")
    return user


async def aupdate_user(user_id: str, updates: dict) -> dict | None:
    await _aensure_init()
    async with aiosqlite.connect(DB_PATH, timeout=30.0, isolation_level=None) as db:
        await db.execute("PRAGMA busy_timeout=30000")
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute(
            "SELECT data FROM users WHERE id = ? LIMIT 1", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            await db.execute("ROLLBACK")
            return None
        user = json.loads(row["data"])
        user.update(updates)
        await db.execute(
            "UPDATE users SET email = ?, data = ? WHERE id = ?",
            ((user.get("email") or "").lower().strip(),
             json.dumps(user, ensure_ascii=False), user_id),
        )
        await db.execute("COMMIT")
    return user


# Initialize schema + run the one-time migration as soon as the module is imported.
_ensure_init()
