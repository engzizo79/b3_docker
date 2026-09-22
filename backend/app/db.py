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
CREATE TABLE IF NOT EXISTS staking_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1), -- single row: this node config
  autostake_enabled INTEGER NOT NULL DEFAULT 0,
  autostake_target TEXT NOT NULL DEFAULT '', -- B3 amount, 9dp string
  autostake_reserve TEXT NOT NULL DEFAULT '', -- B3 kept liquid, 9dp string
  passphrase_enc TEXT, -- Fernet blob; NULL = unattended mode off
  updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS consolidation_settings (
 id INTEGER PRIMARY KEY CHECK (id = 1), -- single row: this node config
 enabled INTEGER NOT NULL DEFAULT 0,
 interval_minutes INTEGER NOT NULL DEFAULT 1440, -- 0 = manual only
 destination TEXT NOT NULL DEFAULT '', -- B3 P2PKH address for consolidated output
 min_utxo_value TEXT NOT NULL DEFAULT '0', -- dust threshold, 9dp
 inputs_per_tx INTEGER NOT NULL DEFAULT 50,
 max_batches INTEGER NOT NULL DEFAULT 5,
 min_output TEXT NOT NULL DEFAULT '0.0001', -- skip batches below this
 fee_mode TEXT NOT NULL DEFAULT 'estimate', -- estimate or fixed
 fee_rate TEXT NOT NULL DEFAULT '0.0001', -- used when fixed
 fee_target INTEGER NOT NULL DEFAULT 6,
 fallback_fee_rate TEXT NOT NULL DEFAULT '0.0001',
 restake_after INTEGER NOT NULL DEFAULT 0, -- 1 = createstake the output
 last_run_ts REAL,
 last_run_summary TEXT,
 updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wizard_wallet_queue (
 id INTEGER PRIMARY KEY,
 action TEXT NOT NULL CHECK (action IN ('create','load')),
 name TEXT NOT NULL,
 passphrase_enc TEXT, -- Fernet blob; NULL for load intents / after processing
 status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','done','failed')),
 error TEXT,
 created_ts REAL NOT NULL,
 updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
 id INTEGER PRIMARY KEY,
 ts TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 level TEXT NOT NULL,
 message TEXT NOT NULL,
 acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contacts (
 id INTEGER PRIMARY KEY,
 label TEXT NOT NULL,
 address TEXT NOT NULL UNIQUE,
 created_ts REAL NOT NULL,
 updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS console_settings (
 id INTEGER PRIMARY KEY CHECK (id = 1), -- single row
 networks TEXT NOT NULL DEFAULT '', -- trusted CIDRs, comma-separated; '' = env seed
 remote_full_access INTEGER NOT NULL DEFAULT 1, -- 2FA remotes: full (1) or read-only (0)
 updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS push_subscriptions (
 id INTEGER PRIMARY KEY,
 endpoint TEXT UNIQUE NOT NULL,
 p256dh TEXT NOT NULL,
 auth TEXT NOT NULL,
 user_agent TEXT,
 created_ts REAL NOT NULL,
 last_seen_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_prefs (
 event_type TEXT PRIMARY KEY,
 enabled INTEGER NOT NULL DEFAULT 1,
 cooldown_minutes INTEGER NOT NULL DEFAULT 0, -- 0 = always instant, no coalescing
 push INTEGER NOT NULL DEFAULT 1,
 webhook INTEGER NOT NULL DEFAULT 1,
 updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_seen (
 seen_key TEXT PRIMARY KEY, -- "{txid}:{vout}:{bucket}"
 ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_pending (
 event_type TEXT PRIMARY KEY, -- an open smart-coalescing window
 opened_ts REAL NOT NULL,
 flush_ts REAL NOT NULL,
 count INTEGER NOT NULL DEFAULT 0, -- events folded in since the instant one
 total_amount TEXT NOT NULL DEFAULT '0' -- 9dp string sum, if amounts apply
);
CREATE TABLE IF NOT EXISTS notification_state (
 id INTEGER PRIMARY KEY CHECK (id = 1), -- single row
 seeded INTEGER NOT NULL DEFAULT 0 -- wallet monitor has completed its no-notify seed pass
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


def audit_query(db_path: str, limit: int = 200, prefixes: list[str] | None = None,
                failed_only: bool = False, q: str = "", since: float = 0.0,
                before_id: int = 0) -> list[dict]:
    """Filtered audit read. `prefixes` matches action LIKE 'p%'; `q` is a
    case-insensitive substring over action/username/detail. All values are
    bound parameters (LIKE wildcards in user input are escaped)."""
    def esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    where, args = [], []
    if prefixes:
        where.append("(" + " OR ".join("action LIKE ? ESCAPE '\\'" for _ in prefixes) + ")")
        args += [esc(p) + "%" for p in prefixes]
    if failed_only:
        where.append("success = 0")
    if q:
        where.append("(action LIKE ? ESCAPE '\\' OR IFNULL(username,'') LIKE ? ESCAPE '\\' "
                     "OR IFNULL(detail,'') LIKE ? ESCAPE '\\')")
        args += ["%" + esc(q) + "%"] * 3
    if since:
        where.append("ts >= ?")
        args.append(since)
    if before_id:
        where.append("id < ?")
        args.append(before_id)
    sql = "SELECT * FROM audit_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    with _lock, _connect(db_path) as conn:
        rows = conn.execute(sql, args).fetchall()
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


# Contacts: saved recipients (other people's addresses) for the Send picker.
# Purely a convenience list; sends still go through the full review flow.
# ---------------------------------------------------------------------------

MAX_CONTACTS = 500


def list_contacts(db_path: str) -> list[dict]:
    with _lock, _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, label, address, created_ts FROM contacts "
            "ORDER BY label COLLATE NOCASE, id").fetchall()
        return [dict(r) for r in rows]


def add_contact(db_path: str, label: str, address: str) -> int:
    """Insert a contact. Raises ValueError('duplicate') / ValueError('full')."""
    with _lock, _connect(db_path) as conn:
        if conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] >= MAX_CONTACTS:
            raise ValueError("full")
        try:
            cur = conn.execute(
                "INSERT INTO contacts (label, address, created_ts, updated_ts) "
                "VALUES (?,?,?,?)", (label, address, time.time(), time.time()))
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate") from exc
        return int(cur.lastrowid)


def rename_contact(db_path: str, contact_id: int, label: str) -> bool:
    with _lock, _connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE contacts SET label=?, updated_ts=? WHERE id=?",
            (label, time.time(), contact_id))
        return cur.rowcount > 0


def delete_contact(db_path: str, contact_id: int) -> bool:
    with _lock, _connect(db_path) as conn:
        cur = conn.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
        return cur.rowcount > 0


def get_staking_settings(db_path: str) -> dict:
    """Single-row staking config (autostake + vault blob). The passphrase
    itself is never returned; callers decrypt via the Vault when authorized."""
    with _lock, _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM staking_settings WHERE id=1").fetchone()
        if row is None:
            return {"autostake_enabled": False, "autostake_target": "",
                    "autostake_reserve": "", "passphrase_enc": None}
        return {"autostake_enabled": bool(row["autostake_enabled"]),
                "autostake_target": row["autostake_target"],
                "autostake_reserve": row["autostake_reserve"],
                "passphrase_enc": row["passphrase_enc"]}


def set_staking_settings(db_path: str, **fields) -> None:
    """Upsert the single staking_settings row. Allowed keys are fixed."""
    allowed = {"autostake_enabled", "autostake_target",
               "autostake_reserve", "passphrase_enc"}
    clean = {k: v for k, v in fields.items() if k in allowed}
    with _lock, _connect(db_path) as conn:
        conn.execute("INSERT OR IGNORE INTO staking_settings (id, updated_ts)"
                    " VALUES (1, ?)", (time.time(),))
        if clean:
            sets = ", ".join(f"{k}=?" for k in clean)
            conn.execute(f"UPDATE staking_settings SET {sets}, updated_ts=?"
                        " WHERE id=1", (*clean.values(), time.time()))


_CONS_DEFAULTS = {
    "enabled": False, "interval_minutes": 1440, "destination": "",
    "min_utxo_value": "0", "inputs_per_tx": 50, "max_batches": 5,
    "min_output": "0.0001", "fee_mode": "estimate", "fee_rate": "0.0001",
    "fee_target": 6, "fallback_fee_rate": "0.0001", "restake_after": False,
    "last_run_ts": None, "last_run_summary": None,
}


def get_consolidation_settings(db_path: str) -> dict:
    """Single-row consolidation sweep config."""
    with _lock, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM consolidation_settings WHERE id=1").fetchone()
    if row is None:
        return dict(_CONS_DEFAULTS)
    return {
        "enabled": bool(row["enabled"]),
        "interval_minutes": int(row["interval_minutes"]),
        "destination": row["destination"],
        "min_utxo_value": row["min_utxo_value"],
        "inputs_per_tx": int(row["inputs_per_tx"]),
        "max_batches": int(row["max_batches"]),
        "min_output": row["min_output"],
        "fee_mode": row["fee_mode"],
        "fee_rate": row["fee_rate"],
        "fee_target": int(row["fee_target"]),
        "fallback_fee_rate": row["fallback_fee_rate"],
        "restake_after": bool(row["restake_after"]),
        "last_run_ts": row["last_run_ts"],
        "last_run_summary": row["last_run_summary"],
    }


def set_consolidation_settings(db_path: str, **fields) -> None:
    """Upsert the single consolidation_settings row."""
    allowed = set(_CONS_DEFAULTS.keys()) - {"last_run_ts", "last_run_summary"}
    clean = {k: v for k, v in fields.items() if k in allowed}
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO consolidation_settings (id, updated_ts)"
            " VALUES (1,?)", (time.time(),))
        if clean:
            sets = ", ".join(f"{k}=?" for k in clean)
            conn.execute(
                f"UPDATE consolidation_settings SET {sets}, updated_ts=?"
                " WHERE id=1", (*clean.values(), time.time()))


def record_consolidation_run(db_path: str, summary: str) -> None:
    """Stamp the last run timestamp + short summary for the UI card."""
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "UPDATE consolidation_settings SET last_run_ts=?, last_run_summary=?"
            " WHERE id=1", (time.time(), summary))


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


# --- wizard wallet queue ---------------------------------------------------


def queue_wallet_intent(db_path: str, action: str, name: str,
                        passphrase_enc: str | None) -> int:
    """Store a wallet intent from the setup wizard. Executed later, once the
    daemon is reachable (see app/wizard_queue.py)."""
    now = time.time()
    with _lock, _connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO wizard_wallet_queue (action, name, passphrase_enc, "
            "status, created_ts, updated_ts) VALUES (?,?,?,?,?,?)",
            (action, name, passphrase_enc, "pending", now, now))
        return int(cur.lastrowid)


def list_wallet_queue(db_path: str) -> list[dict]:
    """All queue rows (newest last). passphrase blobs are stripped."""
    with _lock, _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, action, name, status, error, created_ts, updated_ts "
            "FROM wizard_wallet_queue ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def pending_wallet_intents(db_path: str) -> list[dict]:
    with _lock, _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM wizard_wallet_queue WHERE status='pending' "
            "ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def update_wallet_intent(db_path: str, intent_id: int, status: str,
                         error: str | None = None) -> None:
    """Set status/error and clear the passphrase blob (never kept around)."""
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "UPDATE wizard_wallet_queue SET status=?, error=?, passphrase_enc=NULL, "
            "updated_ts=? WHERE id=?",
            (status, error, time.time(), intent_id))


def remove_wallet_intent(db_path: str, intent_id: int) -> bool:
    """Remove a still-pending intent (user changed their mind). True if removed."""
    with _lock, _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM wizard_wallet_queue WHERE id=? AND status='pending'",
            (intent_id,))
        return cur.rowcount > 0

def get_console_settings(db_path: str, seed_networks: str = "") -> dict:
    """Single-row console trust config. An empty networks string means
    "not set yet" -> the env CONSOLE_NETWORKS seed is used. remote_full_access
    only affects non-allowlisted remote sessions (they always need 2FA)."""
    with _lock, _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM console_settings WHERE id=1").fetchone()
    if row is None:
        return {"networks": seed_networks, "remote_full_access": True,
                "seeded": True}
    # Empty networks means "keep the env seed" - but the operator's
    # remote_full_access choice must survive either way.
    return {"networks": row["networks"] or seed_networks,
            "remote_full_access": bool(row["remote_full_access"]),
            "seeded": False}


def set_console_settings(db_path: str, networks: str,
                         remote_full_access: bool) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO console_settings (id, networks, remote_full_access,"
            " updated_ts) VALUES (1,?,?,?) ON CONFLICT(id) DO UPDATE SET"
            " networks=excluded.networks,"
            " remote_full_access=excluded.remote_full_access,"
            " updated_ts=excluded.updated_ts",
            (networks, 1 if remote_full_access else 0, time.time()))


# ---------------------------------------------------------------------------
# Push notifications: subscriptions, per-type prefs, dedupe/coalescing state.
# ---------------------------------------------------------------------------

def push_subscription_upsert(db_path: str, endpoint: str, p256dh: str,
                             auth: str, user_agent: str | None = None) -> None:
    """A device re-subscribing (permission re-granted, browser refresh of
    its own push endpoint) just refreshes the same row - endpoint is the
    natural key."""
    now = time.time()
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent,"
            " created_ts, last_seen_ts) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh,"
            " auth=excluded.auth, user_agent=excluded.user_agent,"
            " last_seen_ts=excluded.last_seen_ts",
            (endpoint, p256dh, auth, user_agent, now, now))


def push_subscription_list(db_path: str) -> list[dict]:
    with _lock, _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM push_subscriptions ORDER BY created_ts DESC").fetchall()
        return [dict(r) for r in rows]


def push_subscription_delete(db_path: str, *, endpoint: str | None = None,
                             sub_id: int | None = None) -> None:
    with _lock, _connect(db_path) as conn:
        if endpoint is not None:
            conn.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
        elif sub_id is not None:
            conn.execute("DELETE FROM push_subscriptions WHERE id=?", (sub_id,))


_PREF_FIELDS = {"enabled", "cooldown_minutes", "push", "webhook"}


def notification_prefs_get(db_path: str, event_type: str) -> dict | None:
    """Raw stored override for one type, or None if the caller should fall
    back to the registry default (see app/notifier.py)."""
    with _lock, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM notification_prefs WHERE event_type=?", (event_type,)).fetchone()
    if row is None:
        return None
    return {"enabled": bool(row["enabled"]), "cooldown_minutes": int(row["cooldown_minutes"]),
            "push": bool(row["push"]), "webhook": bool(row["webhook"])}


def notification_prefs_list(db_path: str) -> dict[str, dict]:
    """All stored overrides, keyed by event_type."""
    with _lock, _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM notification_prefs").fetchall()
    return {r["event_type"]: {"enabled": bool(r["enabled"]),
                              "cooldown_minutes": int(r["cooldown_minutes"]),
                              "push": bool(r["push"]), "webhook": bool(r["webhook"])}
            for r in rows}


def notification_prefs_set(db_path: str, event_type: str, **fields) -> None:
    """Upsert one type's prefs row, seeded from defaults for any field not
    given (so a partial update never clobbers the others with column
    defaults). Callers pass a complete dict — see routers/notifications.py."""
    clean = {k: v for k, v in fields.items() if k in _PREF_FIELDS}
    if not clean:
        return
    cols = ", ".join(clean.keys())
    placeholders = ", ".join("?" for _ in clean)
    updates = ", ".join(f"{k}=excluded.{k}" for k in clean)
    with _lock, _connect(db_path) as conn:
        conn.execute(
            f"INSERT INTO notification_prefs (event_type, {cols}, updated_ts)"
            f" VALUES (?, {placeholders}, ?)"
            f" ON CONFLICT(event_type) DO UPDATE SET {updates}, updated_ts=excluded.updated_ts",
            (event_type, *clean.values(), time.time()))


def notification_seen_has(db_path: str, key: str) -> bool:
    with _lock, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM notification_seen WHERE seen_key=?", (key,)).fetchone()
    return row is not None


def notification_seen_add(db_path: str, key: str) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO notification_seen (seen_key, ts) VALUES (?, ?)",
            (key, time.time()))


def notification_seen_prune(db_path: str, older_than_ts: float) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute("DELETE FROM notification_seen WHERE ts < ?", (older_than_ts,))


def notification_seeded(db_path: str) -> bool:
    with _lock, _connect(db_path) as conn:
        row = conn.execute("SELECT seeded FROM notification_state WHERE id=1").fetchone()
    return bool(row and row["seeded"])


def notification_mark_seeded(db_path: str) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO notification_state (id, seeded) VALUES (1, 1)"
            " ON CONFLICT(id) DO UPDATE SET seeded=1")


def notification_pending_get(db_path: str, event_type: str) -> dict | None:
    with _lock, _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM notification_pending WHERE event_type=?", (event_type,)).fetchone()
    return dict(row) if row else None


def notification_pending_open(db_path: str, event_type: str,
                              opened_ts: float, flush_ts: float) -> None:
    """Open a fresh coalescing window (count starts at 0 — the triggering
    event was already dispatched instantly, not folded)."""
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO notification_pending (event_type, opened_ts, flush_ts, count, total_amount)"
            " VALUES (?, ?, ?, 0, '0')"
            " ON CONFLICT(event_type) DO UPDATE SET opened_ts=excluded.opened_ts,"
            " flush_ts=excluded.flush_ts, count=0, total_amount='0'",
            (event_type, opened_ts, flush_ts))


def notification_pending_bump(db_path: str, event_type: str,
                              count: int, total_amount: str) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute(
            "UPDATE notification_pending SET count=?, total_amount=? WHERE event_type=?",
            (count, total_amount, event_type))


def notification_pending_due(db_path: str, now_ts: float) -> list[dict]:
    with _lock, _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM notification_pending WHERE flush_ts <= ?", (now_ts,)).fetchall()
        return [dict(r) for r in rows]


def notification_pending_close(db_path: str, event_type: str) -> None:
    with _lock, _connect(db_path) as conn:
        conn.execute("DELETE FROM notification_pending WHERE event_type=?", (event_type,))
