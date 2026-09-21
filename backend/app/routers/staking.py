"""Staking automation API: autostake settings, passphrase vault,
manual reconcile, and one-click two-phase unstake (UX_DESIGN S1/S3/S5).

The vault opt-in requires the interactive unlocked-wallet state and an
explicit risk acknowledgement; the passphrase is stored Fernet-encrypted
and used ONLY by app/autostake.py reconcile. Unstake preview spends
exactly the chosen stake outpoint back to a fresh wallet address via
sendall (signed, add_to_wallet=false, never broadcast), dry-runs
testmempoolaccept, and returns an HMAC token bound to the exact
construction; confirm re-derives the identical transaction, verifies the
token, and only then broadcasts.
"""

import hashlib
import hmac as hmac_mod
import secrets as secrets_mod
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Request

from app import db
from app.autostake import reconcile as reconcile_pass
from app.deps import AppState
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable, parse_amount
from app.vault import vault_from_settings

router = APIRouter(prefix="/api/staking", tags=["staking"])


def _state(request: Request) -> AppState:
    return request.app.state.app_state


def _vault(state: AppState):
    return vault_from_settings(state.settings, state.settings.b3_data_dir)


def _amount_or_400(value, field, allow_zero=False):
    # Parse exact-9dp amounts; empty string means 0. parse_amount enforces
    # positive (send amounts), so thresholds that legitimately allow zero
    # (e.g. min_utxo_value) pass allow_zero=True to accept an exact 0.
    s = str(value).strip()
    if not s:
        return Decimal(0)
    try:
        return parse_amount(s)
    except (ValueError, ArithmeticError, InvalidOperation):
        if allow_zero:
            try:
                z = Decimal(s)
                if z == 0:
                    return z
            except InvalidOperation:
                pass
        raise HTTPException(status_code=400, detail=f"invalid {field}")


def _txid_from_hex(tx_hex):
    # Bitcoin-style txid: double-SHA256 of the raw tx, reversed.
    raw = bytes.fromhex(tx_hex)
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()


def _unstake_token(secret, txid, vout, dest, preview_id):
    msg = f"unstake:{txid}:{vout}:{dest}:{preview_id}"
    return hmac_mod.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


@router.get("/settings")
async def get_settings(request: Request):
    # Never returns the passphrase; only a fingerprint of the stored blob.
    state = _state(request)
    state.require_session(request)
    cfg = db.get_staking_settings(state.settings.db_path)
    vault = _vault(state)
    stored = bool(cfg["passphrase_enc"])
    return {
        "autostake_enabled": cfg["autostake_enabled"],
        "autostake_target": cfg["autostake_target"],
        "autostake_reserve": cfg["autostake_reserve"],
        "vault_stored": stored,
        "vault_fingerprint": (vault.fingerprint(cfg["passphrase_enc"])
                              if vault and stored else None),
        "vault_key_source": "env" if getattr(state.settings, "wallet_vault_key", "") else "file",
        "vault_available": vault is not None,
    }


@router.post("/settings")
async def update_settings(body: dict, request: Request):
    # Enabling unattended mode needs: stored passphrase (or one provided
    # now, which requires the interactively unlocked wallet) + explicit
    # risk acknowledgement. Changing target/reserve just needs a session.
    state = _state(request)
    sess = state.require_session(request)
    body = body or {}
    dbp = state.settings.db_path
    target = _amount_or_400(body.get("autostake_target", ""), "target",
                            allow_zero=True)
    reserve = _amount_or_400(body.get("autostake_reserve", ""), "reserve",
                             allow_zero=True)
    enable = bool(body.get("autostake_enabled"))
    passphrase = str(body.get("passphrase") or "")
    acknowledged = bool(body.get("acknowledge_risk"))

    cfg = db.get_staking_settings(dbp)
    vault = _vault(state)

    if passphrase:
        state.require_wallet_unlocked(request)
        if vault is None:
            raise HTTPException(status_code=503, detail="vault unavailable")
        # Verify against the node BEFORE storing: a typo'd passphrase would
        # otherwise silently break every unattended reconcile (audited
        # failure each boot). Wrong passphrase -> uniform 400.
        try:
            await state.rpc.call("walletpassphrase", passphrase, 60)
        except RPCError:
            db.audit(dbp, "vault_store", sess.username, success=False,
                     detail="passphrase rejected by node")
            raise HTTPException(status_code=400, detail="wrong passphrase")
        except (RPCNotAllowed, RPCUnavailable):
            raise HTTPException(status_code=503, detail="node unreachable")
        db.set_staking_settings(dbp, passphrase_enc=vault.encrypt(passphrase))
        db.audit(dbp, "vault_store", sess.username)
        cfg["passphrase_enc"] = "stored"

    if enable and not cfg["passphrase_enc"]:
        raise HTTPException(status_code=400,
                            detail="store a passphrase before enabling unattended staking")
    if enable and not acknowledged:
        raise HTTPException(status_code=400,
                            detail="risk acknowledgement required")
    if enable and vault is None:
        raise HTTPException(status_code=503, detail="vault unavailable")

    db.set_staking_settings(
        dbp, autostake_enabled=1 if enable else 0,
        autostake_target=format(target, ".9f"),
        autostake_reserve=format(reserve, ".9f"))
    db.audit(dbp, "autostake_settings", sess.username,
             detail=f"enabled={enable} target={target:.9f} reserve={reserve:.9f}")
    return await get_settings(request)


@router.post("/vault/revoke")
async def revoke_vault(request: Request):
    # Wipe the stored passphrase and disable unattended mode.
    state = _state(request)
    sess = state.require_wallet_unlocked(request)
    db.set_staking_settings(state.settings.db_path,
                            autostake_enabled=0, passphrase_enc=None)
    db.audit(state.settings.db_path, "vault_revoke", sess.username)
    return {"ok": True, "vault_stored": False, "autostake_enabled": False}


@router.post("/reconcile")
async def manual_reconcile(request: Request):
    # Run one autostake reconcile pass now (uses the vault under the same
    # scoped rules as the startup loop: unlock 30s, act, relock).
    state = _state(request)
    sess = state.require_2fa(request)
    vault = _vault(state)
    result = await reconcile_pass(state.settings, state.rpc, vault,
                                  reason="manual")
    db.audit(state.settings.db_path, "autostake_manual", sess.username,
             detail=str(result.get("reason") or result.get("topped_up") or "ran"))
    return result


async def _build_unstake(state, txid, vout, destination=None):
    # Verify the stake exists in getstakinginfo (never trust client input
    # for what to spend), then build the signed-not-broadcast sendall spend
    # of exactly this outpoint to the chosen destination.
    #
    # CRITICAL: the destination MUST be deterministic across the two phases
    # (preview + confirm). The CLIENT sends the destination in both phases,
    # so they match. If the client omits it, we default to the stake's
    # owner_address (the wallet's own address that owns the stake) — NOT
    # getnewaddress(), which returns a different address every call on a
    # real node and breaks the HMAC token binding.
    try:
        info = await state.rpc.call("getstakinginfo")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise HTTPException(status_code=503, detail="node unreachable")
    stake = None
    for entry in (info.get("stakes") or []):
        if entry.get("txid") == txid and entry.get("vout") == vout:
            stake = entry
            break
    if stake is None:
        raise HTTPException(status_code=404, detail="stake not found")
    # Use the client's chosen destination, or default to the stake's own
    # owner_address — deterministic, and the wallet's own address.
    dest = (destination or "").strip() or stake.get("owner_address")
    if not dest:
        raise HTTPException(status_code=422,
                            detail="no destination address available")
    # Validate the address via the node (rejects non-P2PKH, wrong network).
    try:
        vinfo = await state.rpc.call("validateaddress", [dest])
        if not vinfo.get("isvalid"):
            raise HTTPException(status_code=400, detail="invalid destination address")
    except RPCError as exc:
        raise HTTPException(status_code=400, detail=f"invalid destination: {exc}")
    except (RPCNotAllowed, RPCUnavailable):
        raise HTTPException(status_code=503, detail="node unreachable")
    try:
        built = await state.rpc.call(
            "sendall", [dest], None, "unset", None,
            {"inputs": [{"txid": txid, "vout": vout}],
             "add_to_wallet": False})
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        raise HTTPException(status_code=422,
                            detail="unstake build failed (is the wallet unlocked?)")
    tx_hex = built.get("hex")
    if not tx_hex:
        raise HTTPException(status_code=422, detail="node returned no signed tx")
    return stake, dest, tx_hex


@router.post("/unstake")
async def unstake(body: dict, request: Request):
    # Two-phase unstake. Phase 1 (confirm=False): build + sign the spend of
    # exactly the chosen stake outpoint to a server-derived fresh address,
    # dry-run testmempoolaccept, return the HMAC token bound to the exact
    # construction. Phase 2 (confirm=True): re-derive, verify the token,
    # re-check mempool, broadcast. Never broadcasts from a preview.
    state = _state(request)
    sess = state.require_wallet_unlocked(request)
    body = body or {}
    txid = str(body.get("txid") or "")
    try:
        vout = int(body.get("vout"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="txid and vout required")
    if len(txid) != 64 or any(c not in "0123456789abcdef" for c in txid):
        raise HTTPException(status_code=400, detail="invalid txid")
    confirm = bool(body.get("confirm"))

    stake, dest, tx_hex = await _build_unstake(state, txid, vout, body.get("destination"))
    preview_id = _txid_from_hex(tx_hex)
    token = _unstake_token(state.settings.session_secret, txid, vout,
                           dest, preview_id)

    try:
        accepted = await state.rpc.call("testmempoolaccept", [tx_hex])
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        accepted = None
    ok = bool(accepted and accepted[0].get("allowed"))

    if not confirm:
        db.audit(state.settings.db_path, "unstake_preview", sess.username,
                 detail=f"txid={txid} vout={vout} preview_id={preview_id}")
        return {
            "preview": True,
            "stake": {"txid": txid, "vout": vout,
                      "amount": stake.get("amount"),
                      "status": stake.get("status")},
            "destination": dest,
            "preview_txid": preview_id,
            "confirm_token": token,
            "mempool_ok": ok,
            "rejection": None if ok else (
                (accepted[0].get("reject-reason") if accepted else None)
                or "rejected"),
            "validator_warning": stake.get("status") == "ACTIVE",
        }

    supplied = str(body.get("confirm_token") or "")
    if not secrets_mod.compare_digest(supplied, token):
        db.audit(state.settings.db_path, "unstake_denied", sess.username,
                 detail="confirm token mismatch", success=False)
        raise HTTPException(status_code=400, detail="confirm token mismatch")
    if not ok:
        db.audit(state.settings.db_path, "unstake_denied", sess.username,
                 detail="mempool reject", success=False)
        raise HTTPException(status_code=422,
                            detail="transaction rejected by mempool policy")

    try:
        sent = await state.rpc.call("sendrawtransaction", tx_hex)
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise HTTPException(status_code=422, detail=f"broadcast failed: {exc}")
    db.audit(state.settings.db_path, "unstake_broadcast", sess.username,
             detail=f"stake={txid}:{vout} dest={dest} txid={sent}")
    return {"preview": False, "txid": sent, "destination": dest,
            "mempool_ok": True}


# ---------------------------------------------------------------------------
# Consolidation sweep (S2) — scheduled plain-P2PKH UTXO consolidation.
# Advanced-only feature: settings, preview (wallet unlock), execute (confirm
# token). The scheduled path lives in app/consolidation.py and reuses the
# same vault + BatchEngine rails.
# ---------------------------------------------------------------------------

from app import consolidation as cons_mod
from app.batch_engine import validate_b3_address


@router.get("/consolidation/settings")
async def get_consolidation_settings(request: Request):
    state = _state(request)
    state.require_session(request)
    return db.get_consolidation_settings(state.settings.db_path)


@router.post("/consolidation/settings")
async def update_consolidation_settings(body: dict, request: Request):
    state = _state(request)
    sess = state.require_session(request)
    body = body or {}
    dbp = state.settings.db_path

    destination = str(body.get("destination") or "").strip()
    if destination and not validate_b3_address(destination):
        raise HTTPException(status_code=400,
                            detail="destination must be a valid B3 P2PKH address")

    interval = int(body.get("interval_minutes", 1440))
    if interval < 0 or interval > 1_000_000:
        raise HTTPException(status_code=400, detail="interval_minutes out of range")

    inputs_per_tx = int(body.get("inputs_per_tx", 50))
    if inputs_per_tx < 1 or inputs_per_tx > 675:
        raise HTTPException(status_code=400, detail="inputs_per_tx out of range")

    max_batches = int(body.get("max_batches", 5))
    if max_batches < 1 or max_batches > 100:
        raise HTTPException(status_code=400, detail="max_batches out of range")

    fee_mode = str(body.get("fee_mode") or "estimate")
    if fee_mode not in ("estimate", "fixed"):
        raise HTTPException(status_code=400, detail="fee_mode must be estimate or fixed")

    min_utxo = _amount_or_400(body.get("min_utxo_value", ""), "min_utxo_value",
                                  allow_zero=True)
    min_output = _amount_or_400(body.get("min_output", "0.0001"), "min_output",
                                   allow_zero=True)
    fee_rate = _amount_or_400(body.get("fee_rate", "0.0001"), "fee_rate")
    fallback_rate = _amount_or_400(body.get("fallback_fee_rate", "0.0001"),
                                  "fallback_fee_rate")

    db.set_consolidation_settings(
        dbp,
        enabled=1 if bool(body.get("enabled")) else 0,
        interval_minutes=interval,
        destination=destination,
        min_utxo_value=format(min_utxo, ".9f"),
        inputs_per_tx=inputs_per_tx,
        max_batches=max_batches,
        min_output=format(min_output, ".9f"),
        fee_mode=fee_mode,
        fee_rate=format(fee_rate, ".9f"),
        fee_target=int(body.get("fee_target", 6)),
        fallback_fee_rate=format(fallback_rate, ".9f"),
        restake_after=1 if bool(body.get("restake_after")) else 0,
    )
    db.audit(dbp, "consolidation_settings", sess.username,
             detail=f"enabled={bool(body.get('enabled'))} dest={destination} "
                    f"interval={interval} restake={bool(body.get('restake_after'))}")
    return db.get_consolidation_settings(dbp)


@router.post("/consolidation/preview")
async def consolidation_preview(request: Request):
    state = _state(request)
    state.require_wallet_unlocked(request)
    try:
        plan = await cons_mod.plan(state.settings, state.rpc,
                                   state.settings.session_secret)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise HTTPException(status_code=503, detail=f"node unavailable: {exc}")
    # Strip internal fields before returning to the client.
    plan.pop("_chunks", None)
    plan.pop("_rate", None)
    plan.pop("_plan_key", None)
    return plan


@router.post("/consolidation/execute")
async def consolidation_execute(body: dict, request: Request):
    state = _state(request)
    sess = state.require_wallet_unlocked(request)
    body = body or {}
    token = str(body.get("confirm_token") or "")
    if not token:
        raise HTTPException(status_code=400, detail="confirm_token required")
    result = await cons_mod.execute(state.settings, state.rpc,
                                    state.settings.session_secret, token,
                                    username=sess.username)
    if not result.get("ok"):
        raise HTTPException(status_code=422, detail=result.get("error", "execute failed"))
    return result


# ---------------------------------------------------------------------------
# Validator pipeline (Modern PoS V1). Block production needs THREE things:
#   1. STAKE weight   - createstake locks coins into a STAKE output
#   2. eligibility    - bindfinalitykey registers the BLS FINALITY_KEY
#                      binding (revokefinalitykey removes block eligibility)
#   3. the staking loop - startstaking (this was ALL the old start button did)
# GET /validator exposes the honest per-step state so the UI can guide the
# user through whatever is missing next instead of silently doing nothing.
# ---------------------------------------------------------------------------

def _dec0(value) -> Decimal:
 try:
  return Decimal(str(value))
 except Exception:
  return Decimal(0)


@router.get("/validator")
async def validator_status(request: Request):
 """Readiness for block production: stake weight, finality-key binding
 and the staking loop - each reported so the UI can name the exact
 next step. Read-only (session + 2FA)."""
 state = _state(request)
 state.require_2fa(request)
 info = await state.rpc.call_optional("getstakinginfo") or {}
 fin = await state.rpc.call_optional("getfinalityinfo") or {}
 loop = info.get("staking") or {}
 binding = fin.get("binding") or {}
 staked = (_dec0(info.get("active")) + _dec0(info.get("pending"))
           + _dec0(info.get("unconfirmed")))
 bound = bool(binding.get("bound")) and not bool(binding.get("revoked"))
 running = bool(loop.get("running"))
 missing = []
 if staked <= 0:
  missing.append("stake")
 if not bound:
  missing.append("bind")
 if not running:
  missing.append("start")
 return {
  "staked": format(staked, ".9f"),
  "min_stake": info.get("min_stake_amount"),
  "bound": bound,
  "revoked": bool(binding.get("revoked")),
  "member": bool((fin.get("validator_set") or {}).get("member")),
  "running": running,
  "blocks_produced": loop.get("blocks_produced"),
  "ready": not missing,
  "missing": missing,
 }


@router.post("/stake")
async def create_stake(body: dict, request: Request):
 """Lock coins into a STAKE output (step 1 of staking). Wallet-affecting:
 session + 2FA + CSRF + unlocked wallet, amount validated as exact 9dp,
 then audit-logged."""
 state = _state(request)
 sess = state.require_wallet_unlocked(request)
 state.require_persistent_data("staking")
 body = body or {}
 amount = _amount_or_400(body.get("amount"), "amount")
 if amount <= 0:
  raise HTTPException(status_code=400, detail="amount must be positive")
 # Defense-in-depth: coins already locked in stakes cannot fund a new
 # one. Compare against the LIQUID balance (trusted minus every stake
 # output in getstakinginfo) so a stale client cannot over-lock. The
 # 0.001 floor keeps a fee reserve, mirroring the frontend check.
 try:
  balances = await state.rpc.call("getbalances")
  staking_info = await state.rpc.call("getstakinginfo")
 except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
  raise _plain_stake_error(exc)
 liquid = None
 try:
  trusted = parse_amount(str((balances.get("mine") or {}).get("trusted") or "0"))
  staked = Decimal("0")
  for st in (staking_info.get("stakes") or []):
   staked += parse_amount(str(st.get("amount") or "0"))
  liquid = trusted - staked
 except (InvalidOperation, ValueError, TypeError):
  liquid = None
 if liquid is not None and amount >= liquid:
  free = liquid - Decimal("0.001")
  raise HTTPException(
   status_code=400,
   detail=(
    "Only " + format(free if free > 0 else Decimal("0"), ".9f")
    + " B3 is free to lock — coins already staked cannot fund another stake"
   ),
  )
 try:
  result = await state.rpc.call("createstake", format(amount, ".9f"))
 except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
  raise _plain_stake_error(exc)
 db.audit(state.settings.db_path, "staking_createstake", sess.username,
          detail="amount=" + format(amount, ".9f"))
 return {"ok": True, "stake": result}


@router.post("/finality/bind")
async def finality_bind(request: Request):
 """Bind this wallet's BLS finality key (step 2 of staking). The key is
 derived by the wallet itself; there is nothing to type. Takes effect at
 the next epoch snapshot boundary."""
 state = _state(request)
 sess = state.require_wallet_unlocked(request)
 state.require_persistent_data("staking")
 try:
  result = await state.rpc.call("bindfinalitykey")
 except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
  raise _plain_stake_error(exc)
 db.audit(state.settings.db_path, "staking_bind_finality", sess.username)
 return {"ok": True, "result": result}


@router.post("/finality/revoke")
async def finality_revoke(body: dict, request: Request):
 """Revoke the finality key binding - the developer-documented procedure
 for operators LEAVING the validator set (going offline long-term). Not a
 recovery step: revocation removes block eligibility once the committee
 handover completes. It does NOT unstake coins and does not erase earlier
 signatures. Requires an explicit risk acknowledgement."""
 state = _state(request)
 sess = state.require_wallet_unlocked(request)
 state.require_persistent_data("staking")
 body = body or {}
 if not body.get("ack"):
  raise HTTPException(status_code=400, detail="risk acknowledgement required")
 try:
  result = await state.rpc.call("revokefinalitykey")
 except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
  raise _plain_stake_error(exc)
 db.audit(state.settings.db_path, "staking_revoke_finality", sess.username)
 return {"ok": True, "result": result}


def _plain_stake_error(exc) -> HTTPException:
 """No RPC method names or stack traces in browser-facing errors."""
 if isinstance(exc, RPCError):
  msg = str(exc.message or "").strip()
  # Node messages are already operator-facing sentences; cap length and
  # strip any method-name fragment just in case.
  if "rpc" in msg.lower() and ":" in msg:
   msg = msg.split(":", 1)[1].strip()
  return HTTPException(status_code=422, detail=(msg[:200] or "the node rejected this action"))
 return HTTPException(status_code=503, detail="node unavailable")
