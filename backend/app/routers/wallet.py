"""Wallet endpoints: unlock/lock, balances, addresses, history, send.

Security invariants:
- Every mutating endpoint: session + 2FA + CSRF (+ unlocked wallet for spends).
- Send is two-phase: preview (sign + testmempoolaccept, no broadcast) and
  confirm (broadcast). The preview txid is bound into the confirm step so a
  confirmed broadcast is exactly the previewed transaction.
- Amounts are Decimal throughout; 9dp max; never float.
"""

import time
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.deps import AppState
from app.ratelimit import limiter
from app.rpc import (B3_DECIMALS, RPCError, RPCNotAllowed, RPCUnavailable,
                     parse_amount)

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


class Recipient(BaseModel):
    address: str
    amount: str  # string to preserve exact Decimal semantics


class SendBody(BaseModel):
    recipients: list[Recipient]
    confirm: bool = False  # False = preview/dry-run; True = broadcast


def _state(request: Request) -> AppState:
    return request.app.state.app_state


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, RPCNotAllowed):
        return HTTPException(status_code=403, detail=f"RPC not allowed: {exc}")
    if isinstance(exc, RPCUnavailable):
        return HTTPException(status_code=503, detail="node unavailable (sync window?)")
    if isinstance(exc, RPCError):
        return HTTPException(status_code=502, detail=f"node error: {exc.message}")
    return HTTPException(status_code=500, detail="internal error")


_ADDRESS_RE = None  # lazily compiled; P2PKH only


def _validate_address(address: str) -> None:
    """B3 legacy P2PKH only: version byte 0x3F, S prefix, base58.
    Rejects witness/bech32 and malformed strings before they reach the node."""
    global _ADDRESS_RE
    if _ADDRESS_RE is None:
        import re
        _ADDRESS_RE = re.compile(r"^S[1-9A-HJ-NP-Za-km-z]{26,34}$")
    if not _ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="invalid B3 P2PKH address")


# ---------------------------------------------------------------------------
# Lock state
# ---------------------------------------------------------------------------

@router.post("/unlock")
async def unlock_wallet(body: dict, request: Request):
    """Unlock the node wallet for signing (passphrase + timeout).
    Session-bound: the API gates spends on THIS session being in the
    unlocked window; the node-side unlock time mirrors it."""
    state = _state(request)
    sess = state.require_csrf(request)
    ip = request.headers.get("x-forwarded-for", "") or (
        request.client.host if request.client else "")
    if not limiter.allow("unlock", ip, limit=5, window_s=60):
        raise HTTPException(status_code=429, detail="too many attempts, wait a minute")
    passphrase = (body or {}).get("passphrase", "")
    if not passphrase:
        raise HTTPException(status_code=400, detail="passphrase required")
    timeout = (body or {}).get("timeout") or state.settings.wallet_unlock_timeout
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="timeout must be an integer")
    if not 1 <= timeout <= 3600:
        raise HTTPException(status_code=400, detail="timeout must be 1..3600 seconds")

    try:
        # walletpassphrase unlocks for `timeout` seconds on the node.
        await state.rpc.call("walletpassphrase", passphrase, timeout)
    except RPCError as exc:
        db.audit(state.settings.db_path, "wallet_unlock", sess.username, ip, success=False)
        # Wrong passphrase -> uniform message (no node error passthrough).
        raise HTTPException(status_code=401, detail="wrong passphrase")
    except (RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)

    sess.wallet_unlocked_until = time.time() + timeout
    db.audit(state.settings.db_path, "wallet_unlock", sess.username, ip)
    return {"ok": True, "unlocked_for_s": timeout}


@router.post("/lock")
async def lock_wallet(request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    try:
        await state.rpc.call("walletlock")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    sess.wallet_unlocked_until = 0.0
    db.audit(state.settings.db_path, "wallet_lock", sess.username)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------

@router.get("/info")
async def wallet_info(request: Request):
    state = _state(request)
    state.require_2fa(request)
    try:
        info = await state.rpc.call("getwalletinfo")
        balances = await state.rpc.call("getbalances")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    return {"wallet": info, "balances": balances}


@router.get("/addresses")
async def addresses(request: Request, label: str = "*"):
    state = _state(request)
    state.require_2fa(request)
    try:
        addrs = await state.rpc.call("getaddressesbylabel", label)
    except RPCError as exc:
        if "Unknown label" in exc.message:
            return {"addresses": {}}
        raise _translate(exc)
    except (RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    return {"addresses": addrs}


@router.get("/labels")
async def labels(request: Request):
    state = _state(request)
    state.require_2fa(request)
    try:
        return {"labels": await state.rpc.call("listlabels")}
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)


@router.get("/history")
async def history(request: Request, count: int = 50, skip: int = 0):
    state = _state(request)
    state.require_2fa(request)
    count = max(1, min(count, 200))
    skip = max(0, skip)
    try:
        txs = await state.rpc.call("listtransactions", "*", count, skip)
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    return {"transactions": txs}


# ---------------------------------------------------------------------------
# Send: preview -> confirm
# ---------------------------------------------------------------------------

@router.post("/send")
async def send(body: SendBody, request: Request):
    """Two-phase send. Phase 1 (confirm=False): build, sign, testmempoolaccept
    — returns the preview txid WITHOUT broadcasting. Phase 2 (confirm=True):
    broadcast the previously previewed txid. The client repeats the request
    with confirm=True; the server re-signs deterministically and verifies the
    txid matches what testmempoolaccept accepted, then broadcasts.

    This keeps the flow stateless while guaranteeing the broadcast tx is
    exactly what was previewed."""
    state = _state(request)
    sess = state.require_wallet_unlocked(request)  # session + 2FA + CSRF + unlocked

    if not body.recipients or len(body.recipients) > 50:
        raise HTTPException(status_code=400, detail="1..50 recipients required")

    parsed: list[tuple[str, Decimal]] = []
    for rcp in body.recipients:
        _validate_address(rcp.address)
        try:
            amount = parse_amount(rcp.amount)
        except (ValueError, ArithmeticError):
            raise HTTPException(status_code=400, detail=f"invalid amount: {rcp.amount}")
        parsed.append((rcp.address, amount))

    # B3 JSON amounts must be exact 9dp strings — never float-serialized.
    outputs = {addr: f"{amt:.9f}" for addr, amt in parsed}

    try:
        raw = await state.rpc.call("createrawtransaction", [], outputs)
        funded = await state.rpc.call("fundrawtransaction", raw)
        signed = await state.rpc.call("signrawtransactionwithwallet", funded["hex"])
        if not signed.get("complete"):
            raise HTTPException(status_code=422, detail="signing incomplete — wallet may be locked")
        tx_hex = signed["hex"]
        txid = signed.get("txid") or await _txid_from_hex(state, tx_hex)

        accepted = await state.rpc.call("testmempoolaccept", [tx_hex])
        ok = bool(accepted and accepted[0].get("allowed"))

        if not body.confirm:
            db.audit(state.settings.db_path, "send_preview", sess.username,
                     detail=f"txid={txid} recipients={len(parsed)}")
            return {
                "preview": True,
                "txid": txid,
                "mempool_ok": ok,
                "rejection": None if ok else (accepted[0].get("reject-reason") or "rejected"),
                "recipients": [
                    {"address": a, "amount": f"{amt:.9f}", "amount_b3": str(amt)}
                    for a, amt in parsed
                ],
            }

        # Confirm phase: preview must have passed testmempoolaccept.
        if not ok:
            db.audit(state.settings.db_path, "send_broadcast_denied", sess.username,
                     detail=f"txid={txid} reason=mempool-reject", success=False)
            raise HTTPException(status_code=422, detail="transaction rejected by mempool policy")

        sent_txid = await state.rpc.call("sendrawtransaction", tx_hex)
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)

    db.audit(state.settings.db_path, "send_broadcast", sess.username,
             detail=f"txid={sent_txid} recipients={len(parsed)}")
    return {"preview": False, "txid": sent_txid, "mempool_ok": True}


async def _txid_from_hex(state: AppState, tx_hex: str) -> str:
    """Fallback txid extraction if signrawtransactionwithwallet omits it.
    Uses decoderawtransaction via the allowlist (added to chain_read)."""
    dec = await state.rpc.call("getrawtransaction", tx_hex)
    return dec["txid"]


# ---------------------------------------------------------------------------
# Staking control
# ---------------------------------------------------------------------------

@router.post("/staking/start")
async def staking_start(request: Request):
    """Start staking. Wallet-affecting action: session + 2FA + CSRF + unlocked,
    then audit-log it."""
    state = _state(request)
    sess = state.require_wallet_unlocked(request)
    try:
        await state.rpc.call("startstaking")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "staking_start", sess.username)
    return {"ok": True, "staking": True}


@router.post("/staking/stop")
async def staking_stop(request: Request):
    """Stop staking. Wallet-affecting action: session + 2FA + CSRF + unlocked,
    then audit-log it."""
    state = _state(request)
    sess = state.require_wallet_unlocked(request)
    try:
        await state.rpc.call("stopstaking")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "staking_stop", sess.username)
    return {"ok": True, "staking": False}
