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
import time
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


# Last outcome of each pass, exposed by GET /api/logs/status so the UI can
# show "last ran / what happened" without parsing the audit log.
status: dict = {"last_run_ts": None, "last_reason": None, "last_result": None}


def _record(dbp, result, reason, dry_run):
    """Every pass leaves one audit line (skips included) plus a status entry."""
    if not dry_run:
        status.update(last_run_ts=time.time(), last_reason=reason,
                      last_result={k: v for k, v in result.items() if k != "steps"})
    bits = []
    if result.get("ran"):
        bits.append("started" if result.get("started") else "start-failed")
        if result.get("topped_up"):
            bits.append("topped_up=" + str(result["topped_up"]))
        if result.get("skipped"):
            bits.append("skipped=" + str(result["skipped"]))
        if result.get("error"):
            bits.append("error=" + str(result["error"]))
    else:
        bits.append("not run: " + str(result.get("reason")))
    ok = not result.get("error") and (result.get("ran") or result.get("reason") in (
        "unattended staking disabled",))
    db.audit(dbp, "autostake_dry_run" if dry_run else "autostake_run",
             detail=f"reason={reason} " + " ".join(bits), success=bool(ok))
    logger.info("autostake %s (%s): %s", "dry-run" if dry_run else "run", reason,
                " ".join(bits))
    return result


async def reconcile(settings, rpc, vault, reason="startup", dry_run=False):
    """One reconcile pass. Never raises; failures are audited and returned.

    dry_run=True performs NO wallet unlock and NO writes: it reads node state
    and reports, step by step, what a real pass would do."""
    dbp = settings.db_path
    cfg = db.get_staking_settings(dbp)
    if not cfg["autostake_enabled"] or not cfg["passphrase_enc"]:
        return _record(dbp, {"ran": False, "reason": "unattended staking disabled"},
                       reason, dry_run)
    if vault is None:
        db.audit(dbp, "autostake", success=False, detail="vault unavailable")
        return _record(dbp, {"ran": False, "reason": "vault unavailable"}, reason, dry_run)

    passphrase = vault.decrypt(cfg["passphrase_enc"])
    if passphrase is None:
        db.audit(dbp, "autostake", success=False,
                 detail="vault decrypt failed - passphrase wiped")
        db.alert_add(dbp, "warning",
                     "Unattended staking disabled: the stored wallet "
                     "passphrase could not be decrypted (vault key changed "
                     "or data corrupted). Re-enter it in Staking settings.")
        db.set_staking_settings(dbp, autostake_enabled=0)
        return _record(dbp, {"ran": False, "reason": "vault decrypt failed"},
                       reason, dry_run)

    steps: list[dict] = []

    def step(name, ok, detail=""):
        steps.append({"step": name, "ok": bool(ok), "detail": str(detail)})

    step("Stored passphrase decrypts", True)
    try:
        wallets = await rpc.call("listwallets")
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        return _record(dbp, {"ran": False, "reason": "node unreachable"}, reason, dry_run)
    if not wallets:
        return _record(dbp, {"ran": False, "reason": "no wallet loaded"}, reason, dry_run)
    step("Wallet loaded", True, ", ".join(w or "(default)" for w in wallets))

    result = {"ran": True, "started": False, "topped_up": None, "steps": steps}
    if dry_run:
        result["dry_run"] = True
    # The node has ONE global lock, not a per-caller lease: if the wallet is
    # already unlocked (a user mid-Send/Unstake, or another scheduled pass),
    # calling walletpassphrase again is harmless, but relocking in `finally`
    # below is NOT - it would cut that other unlock short from under them.
    # Only take the lock/unlock bracket when we are the one closing it.
    took_lock = False
    try:
        if not dry_run:
            try:
                info = await rpc.call("getwalletinfo")
                took_lock = float(info.get("unlocked_until") or 0) <= time.time()
            except (RPCError, RPCNotAllowed, RPCUnavailable):
                took_lock = True  # unknown state - be the one to lock it back up
            if took_lock:
                await rpc.call("walletpassphrase", passphrase, MAX_UNLOCK_SECONDS)
                db.audit(dbp, "autostake_unlock", detail="reason=" + reason)
                step("Wallet unlocked for up to %ds" % MAX_UNLOCK_SECONDS, True)
            else:
                # Already unlocked by someone else - use their window as-is.
                # Re-unlocking here would shorten (or lengthen) it under them.
                step("Wallet already unlocked", True, "reusing the existing window")
            try:
                await rpc.call("startstaking")
                result["started"] = True
                step("Staking loop started", True)
                db.audit(dbp, "autostake_startstaking", detail="reason=" + reason)
            except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
                step("Staking loop started", False, exc)
                db.audit(dbp, "autostake_startstaking", success=False,
                         detail=str(exc))
        else:
            step("Would unlock wallet and start staking", True, "skipped in dry run")

        # Block eligibility needs an on-chain FINALITY_KEY binding
        # (revokefinalitykey removes it; revocation is NEVER automatic).
        # Bind once when none exists; rotations stay operator-only.
        try:
            fin = await rpc.call("getfinalityinfo")
            binding = (fin or {}).get("binding") or {}
            needs_bind = not binding.get("bound") or binding.get("revoked")
            if needs_bind and dry_run:
                step("Finality key binding", True, "not bound - a real run would bind it")
            elif needs_bind:
                await rpc.call("bindfinalitykey")
                result["bound"] = True
                step("Finality key binding", True, "was missing - bound now")
                db.audit(dbp, "autostake_bind_finality", detail="reason=" + reason)
            else:
                step("Finality key binding", True, "already bound")
        except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
            step("Finality key binding", False, exc)
            db.audit(dbp, "autostake_bind_finality", success=False,
                     detail=str(exc))

        target = _dec(cfg["autostake_target"])
        if target > 0:
            info = await rpc.call("getstakinginfo")
            staked = (_dec(info.get("active", 0))
                      + _dec(info.get("pending", 0))
                      + _dec(info.get("unconfirmed", 0)))
            deficit = target - staked
            step("Stake vs target", True,
                 f"staked {format(staked, '.9f')} / target {format(target, '.9f')}")
            if deficit > 0:
                balances = await rpc.call("getbalances")
                avail = _dec((balances.get("mine") or {}).get("trusted", 0))
                reserve = _dec(cfg["autostake_reserve"])
                if avail - reserve >= deficit:
                    amount = format(deficit, ".9f")
                    if dry_run:
                        result["would_top_up"] = amount
                        step("Top-up", True, "a real run would stake " + amount)
                    else:
                        await rpc.call("createstake", amount)
                        result["topped_up"] = amount
                        step("Top-up", True, "staked " + amount)
                        db.audit(dbp, "autostake_createstake",
                                 detail="amount=" + amount + " reason=" + reason)
                else:
                    step("Top-up", False, "deficit %s above liquid %s minus reserve %s"
                         % (format(deficit, ".9f"), format(avail, ".9f"),
                            format(reserve, ".9f")))
                    if not dry_run:
                        db.audit(dbp, "autostake_topup_skipped",
                                 detail="deficit above liquid balance minus reserve")
                    result["skipped"] = "insufficient liquid balance above reserve"
            else:
                step("Top-up", True, "already at or above target")
        else:
            step("Top-up", True, "no stake target set")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        db.audit(dbp, "autostake", success=False, detail=str(exc))
        step("Node call", False, exc)
        result["error"] = str(exc)
    finally:
        if not dry_run and took_lock:
            try:
                await rpc.call("walletlock")
                step("Wallet re-locked", True)
            except (RPCError, RPCNotAllowed, RPCUnavailable):
                step("Wallet re-locked", False)
                logger.warning("autostake: walletlock failed after reconcile")
        elif not dry_run:
            step("Wallet left unlocked", True, "already unlocked by someone else - not ours to lock")
    return _record(dbp, result, reason, dry_run)


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
