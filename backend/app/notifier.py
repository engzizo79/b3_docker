"""Shared notification pipeline: in-app alerts (always) + push/webhook
(governed by per-type prefs and a smart-coalescing anti-spam window).

Used by both app/monitor.py (chain health: stall/lag/recovery) and
app/wallet_monitor.py (wallet events: received/sent/stake) so there is
exactly one "detect -> alert -> maybe notify" pipeline, and the digest
controls apply uniformly to every event type.

Smart coalescing: the first event of a type fires a push/webhook instantly
AND opens a cooldown window. Any more events of that same type while the
window is open are folded into the window's tally instead of dispatching.
When the window's flush_ts passes (checked once per wallet-monitor tick via
flush_due), a single digest goes out summarizing what was folded in. A type
with cooldown_minutes=0 always dispatches instantly with no window at all -
this is the default for everything except staking rewards, which are the
one event type bursty enough to need it out of the box.
"""

import asyncio
import logging
import time
from decimal import Decimal

import httpx

from app import db

logger = logging.getLogger(__name__)

# id -> {label, default_cooldown_minutes}. default_cooldown_minutes=0 means
# "always instant" unless the user opts into a digest.
NOTIFICATION_TYPES: dict[str, dict] = {
    "received": {"label": "Coins received", "default_cooldown_minutes": 0},
    "sent": {"label": "Send confirmed", "default_cooldown_minutes": 0},
    "stake": {"label": "Staking reward", "default_cooldown_minutes": 15},
    "stall": {"label": "Chain stalled", "default_cooldown_minutes": 0},
    "lag": {"label": "Explorer sync lag", "default_cooldown_minutes": 0},
    "recovery": {"label": "Auto-recovery triggered", "default_cooldown_minutes": 0},
}

_DEFAULT_PREFS = {"enabled": True, "push": True, "webhook": True}


def effective_prefs(db_path: str, event_type: str) -> dict:
    """Registry defaults, overridden by anything the user has stored."""
    reg = NOTIFICATION_TYPES.get(event_type, {"label": event_type.title(),
                                               "default_cooldown_minutes": 0})
    prefs = dict(_DEFAULT_PREFS, cooldown_minutes=reg["default_cooldown_minutes"])
    stored = db.notification_prefs_get(db_path, event_type)
    if stored:
        prefs.update(stored)
    return prefs


def list_prefs(db_path: str) -> list[dict]:
    """All known types with their effective prefs, for the settings UI."""
    overrides = db.notification_prefs_list(db_path)
    out = []
    for event_type, reg in NOTIFICATION_TYPES.items():
        prefs = dict(_DEFAULT_PREFS, cooldown_minutes=reg["default_cooldown_minutes"])
        if event_type in overrides:
            prefs.update(overrides[event_type])
        out.append({"event_type": event_type, "label": reg["label"], **prefs})
    return out


def _pending_key(event_type: str, node_id: int | None) -> str:
    """The coalescing window's storage key. notification_pending's PRIMARY
    KEY is still the plain event_type TEXT column (no schema change) — a
    node id is folded into the VALUE stored there as "type@id" instead, per
    docs/MULTINODE_PLAN.md 3.2's documented alternative to a composite PK.
    Without this, a burst on one validator would silently coalesce with —
    and suppress — a burst on another."""
    return event_type if node_id is None else f"{event_type}@{node_id}"


def _split_pending_key(key: str) -> tuple[str, int | None]:
    event_type, sep, node_part = key.partition("@")
    if not sep:
        return key, None
    try:
        return event_type, int(node_part)
    except ValueError:
        return key, None  # not one of ours (shouldn't happen) - treat as unkeyed


def _named(settings, title: str, body: str, node_name: str) -> tuple[str, str]:
    """Prefix the node name onto the DISPATCHED (push/webhook) title only
    when it would actually disambiguate something — i.e. more than one node
    is registered. The in-app alert list instead carries node_id/node_name
    as its own column (db.alert_list's LEFT JOIN) rather than baking it into
    the text, so it stays clean for the single-node case."""
    if not node_name:
        return title, body
    try:
        multi = len(db.node_list(settings.db_path)) > 1
    except Exception:
        multi = bool(node_name)
    if not multi:
        return title, body
    return f"{title} — {node_name}", body


async def notify(settings, event_type: str, message: str, amount: Decimal | None = None,
                 node_id: int | None = None, node_name: str = "") -> None:
    """Record + (maybe) deliver one event. Never raises - a notification
    failure must never take down the monitor loop that called it.

    node_id/node_name (Phase 3): which node this event came from. Callers
    with no node context (tests, anything not yet migrated) simply omit
    them — the alert is recorded with node_id NULL and the coalescing key
    is the bare event_type, exactly like before Phase 3."""
    try:
        db.alert_add(settings.db_path, event_type, message, node_id=node_id)
    except Exception as exc:
        logger.error("notifier: alert_add failed: %s", exc)

    key = _pending_key(event_type, node_id)
    try:
        prefs = effective_prefs(settings.db_path, event_type)
        if not prefs["enabled"]:
            return
        label = NOTIFICATION_TYPES.get(event_type, {}).get("label", event_type.title())
        title, body = _named(settings, label, message, node_name)
        cooldown = prefs["cooldown_minutes"]
        if cooldown <= 0:
            await _dispatch(settings, prefs, event_type, title, body)
            return

        now = time.time()
        pending = db.notification_pending_get(settings.db_path, key)
        if pending is None:
            # First of a burst: dispatch now, open the coalescing window.
            db.notification_pending_open(settings.db_path, key, now, now + cooldown * 60)
            await _dispatch(settings, prefs, event_type, title, body)
        else:
            # Already inside a window: fold in, don't dispatch.
            count = pending["count"] + 1
            total = _dec(pending["total_amount"]) + (amount or Decimal(0))
            db.notification_pending_bump(settings.db_path, key, count, str(total))
    except Exception as exc:
        logger.error("notifier: dispatch for %s failed: %s", event_type, exc)


async def flush_due(settings) -> None:
    """Close out any coalescing windows whose time has come, sending one
    digest for each that actually accumulated something. A window opened
    for node A never folds in or flushes together with node B's — see
    _pending_key."""
    try:
        due = db.notification_pending_due(settings.db_path, time.time())
    except Exception as exc:
        logger.error("notifier: flush_due query failed: %s", exc)
        return
    for row in due:
        key = row["event_type"]  # the composite "type@node_id" (or bare type)
        event_type, node_id = _split_pending_key(key)
        try:
            if row["count"] > 0:
                label = NOTIFICATION_TYPES.get(event_type, {}).get("label", event_type.title())
                node_name = ""
                if node_id is not None:
                    node = db.node_get(settings.db_path, node_id)
                    node_name = node["name"] if node else ""
                body = f"{row['count']} more since the last update"
                total = _dec(row["total_amount"])
                if total > 0:
                    body += f", +{total} B3 total"
                title, body = _named(settings, f"{label} (+{row['count']} more)", body, node_name)
                prefs = effective_prefs(settings.db_path, event_type)
                if prefs["enabled"]:
                    await _dispatch(settings, prefs, event_type, title, body)
            db.notification_pending_close(settings.db_path, key)
        except Exception as exc:
            logger.error("notifier: flush of %s failed: %s", key, exc)


# ---------------------------------------------------------------------------
# Standalone flush loop (Phase 3): previously ridden on WalletMonitor's
# tick, but WalletMonitor became one-instance-per-node — several instances
# all calling flush_due() on their own ticks could race on notification_pending_due()
# for a window neither of them opened (double-dispatching the same digest;
# see docs/MULTINODE_PLAN.md 3.1). Coalescing is a global (not per-node)
# concern, so it gets exactly one ticking source regardless of fleet size.
# ---------------------------------------------------------------------------
_flush_task: asyncio.Task | None = None


async def _flush_loop(settings) -> None:
    while True:
        await asyncio.sleep(settings.notify_interval)
        try:
            await flush_due(settings)
        except Exception as exc:
            logger.error("notifier: flush loop tick failed: %s", exc)


def start_flush_loop(settings) -> None:
    global _flush_task
    if _flush_task is None:
        _flush_task = asyncio.create_task(_flush_loop(settings))
        logger.info("notifier flush loop started (interval=%ds)", settings.notify_interval)


async def stop_flush_loop() -> None:
    global _flush_task
    if _flush_task is not None:
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
        _flush_task = None
        logger.info("notifier flush loop stopped")


async def send_test(settings, event_type: str) -> None:
    """Bypass prefs-enabled/coalescing entirely and dispatch once, so the
    user can verify a channel works. Still respects the per-type push/
    webhook toggles (testing a channel the user turned off would be
    misleading), but not the enabled flag or cooldown."""
    label = NOTIFICATION_TYPES.get(event_type, {}).get("label", event_type.title())
    prefs = effective_prefs(settings.db_path, event_type)
    await _dispatch(settings, prefs, event_type, f"Test: {label}",
                    "This is a test notification from B3 Hive.")


async def _dispatch(settings, prefs: dict, event_type: str, title: str, body: str) -> None:
    if prefs.get("push"):
        await _dispatch_push(settings, event_type, title, body)
    if prefs.get("webhook") and settings.webhook_url:
        await _dispatch_webhook(settings, title, body)


async def _dispatch_push(settings, event_type: str, title: str, body: str) -> None:
    from app import push
    subs = db.push_subscription_list(settings.db_path)
    if not subs:
        return
    payload = {"title": title, "body": body, "tag": event_type}
    for sub in subs:
        result = await asyncio.to_thread(
            push.send, settings.b3_data_dir, settings.vapid_subject, sub, payload)
        if result == "expired":
            db.push_subscription_delete(settings.db_path, endpoint=sub["endpoint"])


async def _dispatch_webhook(settings, title: str, body: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(settings.webhook_url, json={
                "event": title, "title": title, "message": body})
    except Exception as exc:
        logger.debug("notifier: webhook failed: %s", exc)


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(0)
