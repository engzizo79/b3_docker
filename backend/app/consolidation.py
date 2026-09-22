"""Scheduled plain-P2PKH consolidation sweep (S2).

Reuses the verified rails from batch_engine (P2PKH-only filter, Decimal 9dp,
fee floor clamped to minrelaytxfee, testmempoolaccept-gated broadcast) and the
autostake scheduling pattern (vault unlock -> action -> relock, audit-logged,
startup retry then interval). Carriers (B3S1/B3A1/B3MC) are NEVER spent - the
same P2PKH_SCRIPT_RE filter enforces it.

Two entry points:
- manual: plan() + execute() behind a confirm token (like batch execute)
- scheduled: _loop() runs every interval_minutes when enabled + vault stored

Optional restake: after a successful sweep, createstake the consolidated
output so it moves straight into staking (Advanced-only feature).
"""

import asyncio
import hashlib
import hmac
import logging
import time
from decimal import ROUND_DOWN, Decimal

from app import db
from app.batch_engine import (
    BatchEngine, P2PKH_SCRIPT_RE, Q9, compute_fee, validate_b3_address,
)
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable, parse_amount

logger = logging.getLogger(__name__)

CONS_RECIPE_ID = -1
MAX_UNLOCK_SECONDS = 30
_task = None


def _dec(value):
    try:
        return parse_amount(str(value))
    except (ValueError, ArithmeticError):
        return Decimal(0)


def _recipe_from_settings(cfg: dict) -> dict:
    """Map consolidation settings to the BatchEngine recipe shape (only the
    fees + limits blocks are consumed by the reused fee_rate() helper)."""
    return {
        "name": "consolidation",
        "filters": {
            "sources": [], "min_utxo_value": str(cfg["min_utxo_value"]),
            "min_conf": 1, "max_conf": 9_999_999, "sort": "smallest",
        },
        "action": {"type": "consolidate", "destination": cfg["destination"]},
        "limits": {
            "inputs_per_tx": int(cfg["inputs_per_tx"]),
            "max_batches": int(cfg["max_batches"]),
            "min_output": str(cfg["min_output"]),
        },
        "fees": {
            "mode": str(cfg["fee_mode"]),
            "fee_rate": str(cfg["fee_rate"]),
            "fee_target": int(cfg["fee_target"]),
            "fallback_fee_rate": str(cfg["fallback_fee_rate"]),
        },
    }


async def _fetch_consolidation_utxos(rpc, cfg: dict):
    """List ALL spendable plain-P2PKH UTXOs in the loaded wallet (no address
    filter - a sweep is whole-wallet by design). Carriers are skipped by the
    same regex the batch engine uses."""
    min_val = _dec(cfg["min_utxo_value"])
    utxos = await rpc.call("listunspent", 1, 9_999_999, [])
    if not isinstance(utxos, list):
        return [], {"script": 0, "unspendable": 0, "value": 0}
    kept = []
    skipped = {"script": 0, "unspendable": 0, "value": 0}
    for u in utxos:
        if not u.get("spendable", False):
            skipped["unspendable"] += 1
            continue
        spk = str(u.get("scriptPubKey", "")).lower()
        if not P2PKH_SCRIPT_RE.match(spk):
            skipped["script"] += 1
            continue
        val = u["amount"] if isinstance(u["amount"], Decimal) else Decimal(str(u["amount"]))
        if not val.is_finite() or val <= 0 or val < min_val:
            skipped["value"] += 1
            continue
        kept.append({
            "txid": u["txid"], "vout": int(u["vout"]),
            "address": u.get("address", ""), "amount": val,
            "confirmations": int(u.get("confirmations", 0)),
        })
    kept.sort(key=lambda x: x["amount"])
    return kept, skipped


async def plan(settings, rpc, secret: str) -> dict:
    """Build a consolidation plan WITHOUT signing. Returns the user-facing
    preview plus a confirm token bound to the exact UTXO set + fee +
    destination. Reuses BatchEngine.fee_rate() for the relay-floor clamp."""
    cfg = db.get_consolidation_settings(settings.db_path)
    if not cfg["destination"]:
        raise ValueError("consolidation destination not configured")
    if not validate_b3_address(cfg["destination"]):
        raise ValueError("consolidation destination is not a valid B3 P2PKH address")
    recipe = _recipe_from_settings(cfg)
    engine = BatchEngine(rpc)
    rate, fee_source = await engine.fee_rate(recipe)
    utxos, skipped = await _fetch_consolidation_utxos(rpc, cfg)
    limits = recipe["limits"]
    per = int(limits["inputs_per_tx"])
    chunks = [utxos[i:i + per] for i in range(0, len(utxos), per)][: int(limits["max_batches"])]
    batches = []
    for chunk in chunks:
        total_in = sum((u["amount"] for u in chunk), Decimal(0))
        fee = compute_fee(len(chunk), rate)
        out = (total_in - fee).quantize(Q9, ROUND_DOWN)
        batches.append({
            "inputs": len(chunk),
            "total_input": str(total_in),
            "fee": str(fee),
            "output": str(out),
            "below_min_output": out < Decimal(limits["min_output"]),
        })
    hasher = hashlib.sha256()
    for chunk in chunks:
        for u in chunk:
            hasher.update(f"{u['txid']}:{u['vout']}:{u['amount']}".encode())
    hasher.update(str(rate).encode())
    hasher.update(cfg["destination"].encode())
    plan_key = hasher.hexdigest()
    total_output = sum(
        (Decimal(b["output"]) for b in batches if not b["below_min_output"]), Decimal(0))
    total_fee = sum((Decimal(b["fee"]) for b in batches), Decimal(0))
    token = hmac.new(secret.encode(), f"{CONS_RECIPE_ID}:{plan_key}".encode(),
                    hashlib.sha256).hexdigest()
    return {
        "destination": cfg["destination"],
        "eligible_utxos": len(utxos),
        "skipped": skipped,
        "fee_rate": str(rate),
        "fee_source": fee_source,
        "batches": batches,
        "total_output": str(total_output),
        "total_fee": str(total_fee),
        "restake_after": bool(cfg["restake_after"]),
        "confirm_token": token,
        "_chunks": chunks,
        "_rate": rate,
        "_plan_key": plan_key,
    }


def _verify_token(plan_obj, secret, token):
    expected = hmac.new(secret.encode(),
                       f"{CONS_RECIPE_ID}:{plan_obj['_plan_key']}".encode(),
                       hashlib.sha256).hexdigest()
    return hmac.compare_digest(token, expected)


async def execute(settings, rpc, secret, token, username="scheduled"):
    """Sign + testmempoolaccept + broadcast each batch. Re-derives the plan so
    a stale token (UTXO set changed) cannot execute. Optionally restakes the
    output via createstake. Never raises on RPC availability; audits failures."""
    dbp = settings.db_path
    cfg = db.get_consolidation_settings(dbp)
    if not cfg["destination"]:
        return {"ok": False, "error": "destination not configured"}
    plan_obj = await plan(settings, rpc, secret)
    if not _verify_token(plan_obj, secret, token):
        db.audit(dbp, "consolidation_denied", username, success=False,
                 detail="confirm token mismatch (plan changed or stale)")
        return {"ok": False, "error": "plan changed since preview - run preview again"}
    destination = cfg["destination"]
    results = []
    for idx, chunk in enumerate(plan_obj["_chunks"]):
        bmeta = plan_obj["batches"][idx]
        if bmeta["below_min_output"]:
            results.append({"batch": idx, "skipped": True, "reason": "below min_output"})
            continue
        inputs = [{"txid": u["txid"], "vout": u["vout"]} for u in chunk]
        outputs = {destination: bmeta["output"]}
        raw = await rpc.call("createrawtransaction", inputs, outputs)
        signed = await rpc.call("signrawtransactionwithwallet", raw)
        if not signed.get("complete"):
            db.audit(dbp, "consolidation_sign_failed", username, success=False,
                     detail=f"batch={idx} signing incomplete")
            return {"ok": False, "error": f"batch {idx} signing incomplete - wallet may be locked"}
        tx_hex = signed["hex"]
        accepted = await rpc.call("testmempoolaccept", [tx_hex])
        if not (accepted and accepted[0].get("allowed")):
            reason = accepted[0].get("reject-reason") if accepted else "rejected"
            db.audit(dbp, "consolidation_tx_denied", username, success=False,
                     detail=f"batch={idx} reason={reason}")
            return {"ok": False, "error": f"batch {idx} rejected: {reason}"}
        txid = await rpc.call("sendrawtransaction", tx_hex)
        results.append({"batch": idx, "txid": txid,
                        "output": bmeta["output"], "fee": bmeta["fee"]})
        if cfg["restake_after"]:
            try:
                await rpc.call("createstake", bmeta["output"])
                db.audit(dbp, "consolidation_restake", username,
                         detail=f"amount={bmeta['output']}")
            except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
                db.audit(dbp, "consolidation_restake_failed", username, success=False,
                         detail=str(exc))
    summary = (f"batches={len([r for r in results if not r.get('skipped')])} "
               f"utxos={plan_obj['eligible_utxos']} fee={plan_obj['total_fee']}")
    db.audit(dbp, "consolidation_execute", username, detail=summary)
    db.record_consolidation_run(dbp, summary)
    return {"ok": True, "results": results}


status: dict = {"last_run_ts": None, "last_reason": None, "last_result": None}


async def _run_once(settings, rpc, vault, reason="scheduled", dry_run=False):
    """One sweep pass with an audit line + status entry for EVERY outcome
    (skips included), so "did it run?" is always answerable from the log."""
    result = await _pass(settings, rpc, vault, reason, dry_run)
    inner = result.get("result") or {}
    if result.get("ran"):
        ok = bool(inner.get("ok", True))
        detail = ("dry-run: would merge %s outputs in %s batch(es)" % (
                  inner.get("eligible_utxos"), len(inner.get("batches") or []))
                  if dry_run else
                  "ok" if ok else "failed: " + str(inner.get("error")))
    else:
        ok = result.get("reason") == "disabled or no destination"
        detail = "not run: " + str(result.get("reason"))
    if not dry_run:
        status.update(last_run_ts=time.time(), last_reason=reason,
                      last_result={"ran": result.get("ran"), "detail": detail})
    db.audit(settings.db_path,
             "consolidation_dry_run" if dry_run else "consolidation_run",
             detail=f"reason={reason} {detail}", success=ok)
    logger.info("consolidation %s (%s): %s", "dry-run" if dry_run else "run",
                reason, detail)
    result["detail"] = detail
    return result


async def _pass(settings, rpc, vault, reason, dry_run):
    """One scheduled sweep pass. Unlocks via the vault for a short window,
    runs execute with a freshly derived plan, then relocks. dry_run stops
    after planning (no signing, no broadcast)."""
    dbp = settings.db_path
    cfg = db.get_consolidation_settings(dbp)
    if not cfg["enabled"] or not cfg["destination"]:
        return {"ran": False, "reason": "disabled or no destination"}
    staking = db.get_staking_settings(dbp)
    if not staking["passphrase_enc"]:
        return {"ran": False, "reason": "no stored passphrase (vault)"}
    if vault is None:
        db.audit(dbp, "consolidation", success=False, detail="vault unavailable")
        return {"ran": False, "reason": "vault unavailable"}
    passphrase = vault.decrypt(staking["passphrase_enc"])
    if passphrase is None:
        db.audit(dbp, "consolidation", success=False,
                 detail="vault decrypt failed - passphrase wiped")
        db.alert_add(dbp, "warning",
                     "Scheduled consolidation disabled: the stored wallet "
                     "passphrase could not be decrypted (vault key changed "
                     "or data corrupted). Re-enter it in Staking settings.")
        db.set_consolidation_settings(dbp, enabled=0)
        return {"ran": False, "reason": "vault decrypt failed"}
    try:
        wallets = await rpc.call("listwallets")
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        return {"ran": False, "reason": "node unreachable"}
    if not wallets:
        return {"ran": False, "reason": "no wallet loaded"}
    # The node has ONE global lock, not a per-caller lease: if the wallet
    # is already unlocked (a user mid-Send/Unstake, or another scheduled
    # pass), relocking in `finally` below would cut that other unlock
    # short from under them. Only take the bracket when we are the one
    # closing it. dry_run never signs, so it never needs to unlock at all
    # (matching autostake.reconcile's contract) - plan() below is read-only.
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
                db.audit(dbp, "consolidation_unlock", detail="reason=" + reason)
            # else: already unlocked by someone else - reuse their window
            # as-is; re-unlocking here would shorten (or lengthen) it under them.
        plan_obj = await plan(settings, rpc, settings.session_secret)
        token = hmac.new(settings.session_secret.encode(),
                        f"{CONS_RECIPE_ID}:{plan_obj['_plan_key']}".encode(),
                        hashlib.sha256).hexdigest()
        if dry_run:
            summary = {k: plan_obj[k] for k in (
                "eligible_utxos", "batches", "total_fee", "total_output",
                "skipped", "restake_after")}
            summary["ok"] = True
            return {"ran": True, "dry_run": True, "result": summary}
        result = await execute(settings, rpc, settings.session_secret, token,
                               username="scheduled")
        if not result.get("ok"):
            db.audit(dbp, "consolidation_failed", success=False,
                     detail=result.get("error", "unknown"))
        return {"ran": True, "result": result}
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        db.audit(dbp, "consolidation_error", success=False, detail=str(exc))
        return {"ran": False, "reason": str(exc)}
    finally:
        if not dry_run and took_lock:
            try:
                await rpc.call("walletlock")
            except (RPCError, RPCNotAllowed, RPCUnavailable):
                logger.warning("consolidation: walletlock failed after sweep")


async def _loop(settings, rpc, vault):
    """Startup retry (node takes ~5min to boot) then interval loop."""
    attempt = 0
    while True:
        result = await _run_once(settings, rpc, vault, reason="startup")
        if result.get("ran"):
            break
        if result.get("reason") in ("disabled or no destination",
                                    "no stored passphrase (vault)",
                                    "vault unavailable", "vault decrypt failed",
                                    "no wallet loaded"):
            return
        attempt += 1
        await asyncio.sleep(min(60 * (2 ** min(attempt, 4)), 600))
    while True:
        cfg = db.get_consolidation_settings(settings.db_path)
        minutes = int(cfg["interval_minutes"]) if cfg["enabled"] else 0
        if not minutes:
            return
        await asyncio.sleep(minutes * 60)
        cfg = db.get_consolidation_settings(settings.db_path)
        if not cfg["enabled"]:
            return
        await _run_once(settings, rpc, vault, reason="scheduled")


def start_consolidation(settings, rpc, vault):
    """Fire-and-forget the scheduled sweep task (called from lifespan)."""
    global _task
    cfg = db.get_consolidation_settings(settings.db_path)
    if not cfg["enabled"]:
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.get_event_loop().create_task(_loop(settings, rpc, vault))
    logger.info("consolidation: scheduled sweep task started")


async def stop_consolidation():
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
    _task = None
