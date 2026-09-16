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


def _amount_or_400(value, field):
    # Parse exact-9dp amounts; empty string means 0.
    try:
        amt = parse_amount(str(value)) if str(value).strip() else Decimal(0)
    except (ValueError, ArithmeticError, InvalidOperation):
        raise HTTPException(status_code=400, detail=f"invalid {field}")
    if amt < 0:
        raise HTTPException(status_code=400, detail=f"{field} must be >= 0")
    return amt


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
        "vault_key_source": "env" if state.settings.wallet_vault_key else "file",
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
    target = _amount_or_400(body.get("autostake_target", ""), "target")
    reserve = _amount_or_400(body.get("autostake_reserve", ""), "reserve")
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


async def _build_unstake(state, txid, vout):
    # Verify the stake exists in getstakinginfo (never trust client input
    # for what to spend), derive a fresh receiving address, and build the
    # signed-not-broadcast sendall spend of exactly this outpoint.
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
    try:
        dest = await state.rpc.call("getnewaddress")
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

    stake, dest, tx_hex = await _build_unstake(state, txid, vout)
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
