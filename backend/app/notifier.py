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


async def notify(settings, event_type: str, message: str, amount: Decimal | None = None) -> None:
    """Record + (maybe) deliver one event. Never raises - a notification
    failure must never take down the monitor loop that called it."""
    try:
        db.alert_add(settings.db_path, event_type, message)
    except Exception as exc:
        logger.error("notifier: alert_add failed: %s", exc)

    try:
        prefs = effective_prefs(settings.db_path, event_type)
        if not prefs["enabled"]:
            return
        label = NOTIFICATION_TYPES.get(event_type, {}).get("label", event_type.title())
        cooldown = prefs["cooldown_minutes"]
        if cooldown <= 0:
            await _dispatch(settings, prefs, event_type, label, message)
            return

        now = time.time()
        pending = db.notification_pending_get(settings.db_path, event_type)
        if pending is None:
            # First of a burst: dispatch now, open the coalescing window.
            db.notification_pending_open(settings.db_path, event_type, now, now + cooldown * 60)
            await _dispatch(settings, prefs, event_type, label, message)
        else:
            # Already inside a window: fold in, don't dispatch.
            count = pending["count"] + 1
            total = _dec(pending["total_amount"]) + (amount or Decimal(0))
            db.notification_pending_bump(settings.db_path, event_type, count, str(total))
    except Exception as exc:
        logger.error("notifier: dispatch for %s failed: %s", event_type, exc)


async def flush_due(settings) -> None:
    """Close out any coalescing windows whose time has come, sending one
    digest for each that actually accumulated something."""
    try:
        due = db.notification_pending_due(settings.db_path, time.time())
    except Exception as exc:
        logger.error("notifier: flush_due query failed: %s", exc)
        return
    for row in due:
        event_type = row["event_type"]
        try:
            if row["count"] > 0:
                label = NOTIFICATION_TYPES.get(event_type, {}).get("label", event_type.title())
                body = f"{row['count']} more since the last update"
                total = _dec(row["total_amount"])
                if total > 0:
                    body += f", +{total} B3 total"
                prefs = effective_prefs(settings.db_path, event_type)
                if prefs["enabled"]:
                    await _dispatch(settings, prefs, event_type, f"{label} (+{row['count']} more)", body)
            db.notification_pending_close(settings.db_path, event_type)
        except Exception as exc:
            logger.error("notifier: flush of %s failed: %s", event_type, exc)


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
