"""SQLite persistence: single user (Argon2id hash, encrypted TOTP secret,
recovery-code hashes) and the audit log. Synchronous sqlite3 with a lock —
single-process, single-user scale is the design point."""

import hashlib
import json
import secrets
import sqlite3
import threading
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_lock = threading.Lock()
_ph = PasswordHasher()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    totp_secret_enc TEXT,
    totp_enabled INTEGER NOT NULL DEFAULT 0,
    recovery_codes TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS batch_recipes (
 id INTEGER PRIMARY KEY,
 name TEXT UNIQUE NOT NULL,
 recipe TEXT NOT NULL,
 created_ts REAL NOT NULL,
 updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    username TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    success INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
 id INTEGER PRIMARY KEY,
 ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 level TEXT NOT NULL,
 message TEXT NOT NULL,
 acknowledged INTEGER NOT NULL DEFAULT 0
);
"""


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with _lock, _connect(db_path) as conn:
        conn.executescript(SCHEMA)


def ensure_user(db_path: str, username: str, password: str) -> None:
    """Create the user if missing, or update the password hash when UI_PASSWORD
    changed. UI_PASSWORD env is the authoritative login password at startup."""
    new_hash = _ph.hash(password)
    with _lock, _connect(db_path) as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, new_hash))
        else:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE username=?",
                (new_hash, username))


def get_user(db_path: str, username: str) -> dict | None:
    with _lock, _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def set_totp_secret(db_path: str, username: str, secret_enc: str, enabled: bool) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET totp_secret_enc=?, totp_enabled=? WHERE username=?",
            (secret_enc, 1 if enabled else 0, username))


def set_recovery_codes(db_path: str, username: str, code_hashes: list[str]) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET recovery_codes=? WHERE username=?",
            (json.dumps(code_hashes), username))


def verify_password(db_path: str, username: str, password: str) -> dict | None:
    """Return the user row on success, None on mismatch."""
    user = get_user(db_path, username)
    if user is None:
        # Burn comparable time to blunt user-enumeration timing.
        _ph.hash("timing-equalizer")
        return None
    try:
        _ph.verify(user["password_hash"], password)
    except VerifyMismatchError:
        return None
    return user


def generate_recovery_codes(db_path: str, username: str) -> list[str]:
    """Generate 8 one-time recovery codes, store their SHA-256 hashes,
    and return the plaintext codes ONCE."""
    codes = [secrets.token_hex(5) for _ in range(8)]
    hashes = [hashlib.sha256(c.encode()).hexdigest() for c in codes]
    set_recovery_codes(db_path, username, hashes)
    return codes


def consume_recovery_code(db_path: str, username: str, code: str) -> bool:
    """Verify and burn a recovery code (single use)."""
    digest = hashlib.sha256(code.strip().encode()).hexdigest()
    user = get_user(db_path, username)
    if not user:
        return False
    hashes = json.loads(user["recovery_codes"] or "[]")
    if digest not in hashes:
        return False
    hashes.remove(digest)
    set_recovery_codes(db_path, username, hashes)
    return True


def audit(db_path: str, action: str, username: str | None = None,
          detail: str | None = None, success: bool = True) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO audit_log (ts, username, action, detail, success) "
            "VALUES (?, ?, ?, ?, ?)",
            (time.time(), username, action, detail, 1 if success else 0))


def alert_add(db_path: str, level: str, message: str) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute("INSERT INTO alerts (level, message) VALUES (?, ?)", (level, message))


def alert_list(db_path: str, unacked_only: bool = False, limit: int = 100) -> list[dict]:
    with _lock, _connect(db_path) as conn:
        q = "SELECT * FROM alerts"
        if unacked_only:
            q += " WHERE acknowledged=0"
        q += " ORDER BY id DESC LIMIT ?"
        rows = conn.execute(q, (limit,)).fetchall()
        return [dict(r) for r in rows]


def alert_ack(db_path: str, alert_id: int) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (alert_id,))


def alert_ack_all(db_path: str) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute("UPDATE alerts SET acknowledged=1 WHERE acknowledged=0")



def audit_list(db_path: str, limit: int = 100) -> list[dict]:
    with _lock, _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Batch recipes (user-definable batch actions). Recipe JSON is validated by
# batch_engine.validate_recipe BEFORE storage and re-validated on every
# load, so a stale or corrupt row can never bypass the engine rails.
# ---------------------------------------------------------------------------


def list_recipes(db_path: str) -> list[dict]:
    with _lock, _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, name, recipe, created_ts, updated_ts "
            "FROM batch_recipes ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def get_recipe(db_path: str, recipe_id: int) -> dict | None:
    with _lock, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT id, name, recipe, created_ts, updated_ts "
            "FROM batch_recipes WHERE id=?", (recipe_id,)).fetchone()
        return dict(row) if row else None


def create_recipe(db_path: str, name: str, recipe_json: str) -> int:
    with _lock, _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO batch_recipes (name, recipe, created_ts, updated_ts) "
            "VALUES (?,?,?,?)",
            (name, recipe_json, time.time(), time.time()))
        return int(cur.lastrowid)


def update_recipe(db_path: str, recipe_id: int, name: str, recipe_json: str) -> bool:
    with _lock, _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE batch_recipes SET name=?, recipe=?, updated_ts=? WHERE id=?",
            (name, recipe_json, time.time(), recipe_id))
        return cur.rowcount > 0


def delete_recipe(db_path: str, recipe_id: int) -> bool:
    with _lock, _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM batch_recipes WHERE id=?", (recipe_id,))
        return cur.rowcount > 0


def user_exists(db_path: str, username: str) -> bool:
    """Setup mode: True once the operator account exists (password set)."""
    with _lock, _connect(db_path) as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        return row is not None


def set_password(db_path: str, username: str, password: str) -> None:
    """Create the operator account or change its password (setup step).
    This exits setup mode: login is required for subsequent starts."""
    new_hash = _ph.hash(password)
    with _lock, _connect(db_path) as conn:
        row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO users (username, password_hash) VALUES (?,?)",
                         (username, new_hash))
        else:
            conn.execute("UPDATE users SET password_hash=? WHERE username=?",
                         (new_hash, username))
