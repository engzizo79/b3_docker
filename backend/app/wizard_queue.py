"""Background processor for wizard wallet intents.

The setup wizard gathers wallet choices while the daemon is still DOWN
(deferred until first-run setup finishes - user requirement). Create/load
intents are queued in SQLite with the passphrase Fernet-encrypted (app
vault). This loop waits until the node is reachable, executes them in
order, records the result, and wipes the encrypted blob immediately.

Security properties:
- The passphrase never touches disk in plaintext and never appears in
  logs or audit entries.
- The blob is cleared the moment the intent leaves 'pending'.
- A vault decrypt failure fails the intent (never a plaintext prompt) and
  raises an alert.
"""

import asyncio
import logging
import re

from app import db
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable

logger = logging.getLogger("b3hive.wizard_queue")

_task: asyncio.Task | None = None

POLL_SECONDS = 5

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _validate_name(name: str) -> bool:
    return bool(_NAME_RE.fullmatch(name))


async def process_pending(settings, rpc, vault) -> dict:
    """One pass over pending wallet intents. Never raises.
    Returns {ran, executed, ok, failed}."""
    dbp = settings.db_path
    pending = db.pending_wallet_intents(dbp)
    if not pending:
        return {"ran": False, "executed": 0, "ok": 0, "failed": 0}

    # Node must be up before any wallet action.
    try:
        await rpc.call("getnetworkinfo")
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        return {"ran": False, "executed": 0, "ok": 0, "failed": 0}

    ok = failed = 0
    for intent in pending:
        action = intent["action"]
        name = intent["name"]
        try:
            if action == "create":
                if vault is None:
                    raise RuntimeError("vault unavailable - cannot decrypt passphrase")
                blob = intent.get("passphrase_enc")
                if not blob:
                    raise RuntimeError("missing encrypted passphrase")
                passphrase = vault.decrypt(blob)
                if passphrase is None:
                    raise RuntimeError("vault decrypt failed (key rotated?)")
                # descriptors MUST be true: B3Hive (Core 31.1) rejects legacy
                # wallet creation. Legacy P2PKH addresses are still produced
                # (B3 consensus forces OutputType::LEGACY).
                await rpc.call("createwallet", name, False, False, passphrase,
                               False, True, True, False)
            else:  # load
                await rpc.call("loadwallet", name)
            db.update_wallet_intent(dbp, intent["id"], "done")
            db.audit(dbp, f"wizard_queue.{action}", detail=f"name={name}")
            ok += 1
        except (RPCError, RPCNotAllowed, RPCUnavailable, RuntimeError) as exc:
            msg = str(getattr(exc, "message", exc))
            db.update_wallet_intent(dbp, intent["id"], "failed", msg[:200])
            db.audit(dbp, f"wizard_queue.{action}", success=False,
                     detail=f"name={name} error={msg[:120]}")
            db.alert_add(dbp, "warning",
                         f"Setup wizard could not {action} wallet '{name}': {msg[:160]}")
            failed += 1
    return {"ran": True, "executed": ok + failed, "ok": ok, "failed": failed}


async def _run(settings, rpc, vault) -> None:
    while True:
        try:
            await process_pending(settings, rpc, vault)
        except Exception:  # never die
            logger.exception("wizard queue pass failed")
        await asyncio.sleep(POLL_SECONDS)


def start_wizard_queue(settings, rpc, vault) -> None:
    global _task
    if _task is None:
        _task = asyncio.create_task(_run(settings, rpc, vault))
        logger.info("wizard wallet queue processor started")


async def stop_wizard_queue() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
        logger.info("wizard wallet queue processor stopped")
