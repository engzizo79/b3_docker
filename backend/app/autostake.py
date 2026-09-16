"""Unattended autostake reconcile (S1 + S5).

Runs at backend startup (and on manual trigger): if the user opted into
unattended staking, unlocks the wallet for a short window via the vault,
starts the staking loop, tops the STAKE total up to the configured target
(capped by available balance minus reserve), then relocks immediately.

Every unlock and action is audit-logged. The stored passphrase is used
NOWHERE else (see app/vault.py docstring). If the passphrase cannot be
decrypted (rotated vault key, corrupted blob), unattended mode disables
itself and raises an alert - never a plaintext prompt.
"""

import asyncio
import logging
from decimal import Decimal, InvalidOperation

from app import db
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable, parse_amount

logger = logging.getLogger(__name__)

MAX_UNLOCK_SECONDS = 30
_task = None


def _dec(value):
    """Node amounts are exact 9dp strings - parse defensively."""
    try:
        return parse_amount(str(value))
    except (ValueError, ArithmeticError, InvalidOperation):
        return Decimal(0)


async def reconcile(settings, rpc, vault, reason="startup"):
    """One reconcile pass. Never raises; failures are audited and returned."""
    dbp = settings.db_path
    cfg = db.get_staking_settings(dbp)
    if not cfg["autostake_enabled"] or not cfg["passphrase_enc"]:
        return {"ran": False, "reason": "unattended staking disabled"}
    if vault is None:
        db.audit(dbp, "autostake", success=False, detail="vault unavailable")
        return {"ran": False, "reason": "vault unavailable"}

    passphrase = vault.decrypt(cfg["passphrase_enc"])
    if passphrase is None:
        db.audit(dbp, "autostake", success=False,
                 detail="vault decrypt failed - passphrase wiped")
        db.alert_add(dbp, "warning",
                     "Unattended staking disabled: the stored wallet "
                     "passphrase could not be decrypted (vault key changed "
                     "or data corrupted). Re-enter it in Staking settings.")
        db.set_staking_settings(dbp, autostake_enabled=0)
        return {"ran": False, "reason": "vault decrypt failed"}

    try:
        wallets = await rpc.call("listwallets")
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        return {"ran": False, "reason": "node unreachable"}
    if not wallets:
        return {"ran": False, "reason": "no wallet loaded"}

    result = {"ran": True, "started": False, "topped_up": None}
    try:
        await rpc.call("walletpassphrase", passphrase, MAX_UNLOCK_SECONDS)
        db.audit(dbp, "autostake_unlock", detail="reason=" + reason)
        try:
            await rpc.call("startstaking")
            result["started"] = True
            db.audit(dbp, "autostake_startstaking", detail="reason=" + reason)
        except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
            db.audit(dbp, "autostake_startstaking", success=False,
                     detail=str(exc))

        target = _dec(cfg["autostake_target"])
        if target > 0:
            info = await rpc.call("getstakinginfo")
            staked = (_dec(info.get("active", 0))
                      + _dec(info.get("pending", 0))
                      + _dec(info.get("unconfirmed", 0)))
            deficit = target - staked
            if deficit > 0:
                balances = await rpc.call("getbalances")
                avail = _dec((balances.get("mine") or {}).get("trusted", 0))
                reserve = _dec(cfg["autostake_reserve"])
                if avail - reserve >= deficit:
                    amount = format(deficit, ".9f")
                    await rpc.call("createstake", amount)
                    result["topped_up"] = amount
                    db.audit(dbp, "autostake_createstake",
                             detail="amount=" + amount + " reason=" + reason)
                else:
                    db.audit(dbp, "autostake_topup_skipped",
                             detail="deficit above liquid balance minus reserve")
                    result["skipped"] = "insufficient liquid balance above reserve"
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        db.audit(dbp, "autostake", success=False, detail=str(exc))
        result["error"] = str(exc)
    finally:
        try:
            await rpc.call("walletlock")
        except (RPCError, RPCNotAllowed, RPCUnavailable):
            logger.warning("autostake: walletlock failed after reconcile")
    return result


async def _startup_loop(settings, rpc, vault):
    """Retry the startup reconcile until the node answers (crash/reboot
    recovery), then keep a slow hourly drift check while enabled."""
    attempt = 0
    while True:
        result = await reconcile(settings, rpc, vault, reason="startup")
        if result.get("ran"):
            break
        if result.get("reason") in ("no wallet loaded",
                                    "unattended staking disabled",
                                    "vault unavailable",
                                    "vault decrypt failed"):
            return
        attempt += 1
        # 1min -> 2 -> 4 -> 8, capped at 10min. The node takes ~5min to boot.
        await asyncio.sleep(min(60 * (2 ** min(attempt, 4)), 600))
    # Node up and reconciled: hourly drift check. Manual top-ups in the UI
    # can raise staked above target; reconcile is a no-op then (deficit <= 0).
    while True:
        await asyncio.sleep(3600)
        cfg = db.get_staking_settings(settings.db_path)
        if not cfg["autostake_enabled"] or not cfg["passphrase_enc"]:
            return
        await reconcile(settings, rpc, vault, reason="hourly")


def start_autostake(settings, rpc, vault):
    """Fire-and-forget the startup reconcile task (called from lifespan)."""
    global _task
    cfg = db.get_staking_settings(settings.db_path)
    if not cfg["autostake_enabled"] or not cfg["passphrase_enc"]:
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.get_event_loop().create_task(
        _startup_loop(settings, rpc, vault))
    logger.info("autostake: startup reconcile scheduled")


async def stop_autostake():
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None
