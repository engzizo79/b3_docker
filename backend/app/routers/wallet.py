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

from app import autostake, db
from app.batch_engine import HARD_MAX_INPUTS, P2PKH_SCRIPT_RE
from app.deps import AppState
from app.ratelimit import limiter
from app.crypto_envelope import decrypt_envelope
from app.rpc import (B3_DECIMALS, RPCError, RPCNotAllowed, RPCUnavailable,
                     parse_amount)

router = APIRouter(prefix="/api/wallet", tags=["wallet"])


class Recipient(BaseModel):
    address: str
    amount: str  # string to preserve exact Decimal semantics


class InputRef(BaseModel):
    txid: str
    vout: int


class SendBody(BaseModel):
    recipients: list[Recipient]
    confirm: bool = False  # False = preview/dry-run; True = broadcast
    # Coin control: spend exactly these outputs instead of letting the
    # wallet pick. Optional; omitted/empty = automatic selection (old
    # behavior, unchanged).
    inputs: list[InputRef] | None = None


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
    state.require_persistent_data()  # block unlock if /data not persistent
    ip = request.headers.get("x-forwarded-for", "") or (
        request.client.host if request.client else "")
    if not limiter.allow("unlock", ip, limit=5, window_s=60):
        raise HTTPException(status_code=429, detail="too many attempts, wait a minute")
    passphrase = (body or {}).get("passphrase", "")
    # ECDH envelope: the passphrase arrives encrypted, so the cleartext
    # never crosses the wire (defense even on plain HTTP transports).
    env = (body or {}).get("env")
    if isinstance(env, dict) and state.settings.envelope_encryption and state.ecdh_priv:
        try:
            passphrase = decrypt_envelope(state.ecdh_priv, env)
        except Exception:
            raise HTTPException(status_code=400,
                                detail="could not decrypt passphrase envelope")
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
        # 403 (not 401): the session is valid; the passphrase is simply
        # wrong. 401 means "sign in again" to the frontend, which logged
        # users out over a typo.
        raise HTTPException(status_code=403, detail="wrong passphrase")
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


_TXID_HEX_RE = None  # lazily compiled


def _is_txid(s: str) -> bool:
    global _TXID_HEX_RE
    if _TXID_HEX_RE is None:
        import re
        _TXID_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
    return bool(_TXID_HEX_RE.match(s or ""))


async def _resolve_coin_control(state: AppState, refs: list) -> list[dict]:
    """Coin control: the client names exact outpoints to spend. NEVER trust
    that list blindly — re-check every one against a fresh listunspent so a
    stale/forged txid:vout can't be spent, and reject anything that isn't
    plain P2PKH (carrier outputs — stake, asset, metadata — are never
    spendable from a plain send, same rule batch tools and unstake follow).
    Returns the matched listunspent entries, in the order requested."""
    if not refs:
        raise HTTPException(status_code=400, detail="at least one input is required")
    # HARD_MAX_INPUTS is the real ceiling (B3's standard-tx weight policy,
    # same number batch_engine's consolidation sweeps are built around) -
    # not an arbitrary UI limit. A wallet with thousands of small reward
    # UTXOs genuinely needs several transactions to sweep; this is the
    # most any single one can ever carry.
    if len(refs) > HARD_MAX_INPUTS:
        raise HTTPException(status_code=400,
                            detail=f"too many inputs selected (max {HARD_MAX_INPUTS} per transaction)")
    for r in refs:
        if not _is_txid(r.txid):
            raise HTTPException(status_code=400, detail=f"invalid input txid: {r.txid}")
        if r.vout < 0:
            raise HTTPException(status_code=400, detail="invalid input vout")
    try:
        unspent = await state.rpc.call("listunspent", 0)
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    by_key = {(u.get("txid"), u.get("vout")): u for u in (unspent or [])}
    resolved = []
    for r in refs:
        u = by_key.get((r.txid, r.vout))
        if u is None:
            raise HTTPException(status_code=409,
                                detail=f"selected output {r.txid[:12]}…:{r.vout} is no "
                                        "longer in your wallet — refresh and try again")
        if not u.get("spendable", True):
            raise HTTPException(status_code=422,
                                detail=f"selected output {r.txid[:12]}…:{r.vout} is not spendable")
        if not P2PKH_SCRIPT_RE.match(str(u.get("scriptPubKey", "")).lower()):
            raise HTTPException(status_code=422,
                                detail=f"selected output {r.txid[:12]}…:{r.vout} is not a plain "
                                        "P2PKH output (stake/asset/metadata outputs can't be sent "
                                        "this way — use Unstake or the dedicated flow for those)")
        resolved.append(u)
    return resolved


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

    coin_control = None
    vin = []
    if body.inputs is not None:  # [] is a deliberate "nothing selected", not "omitted"
        coin_control = await _resolve_coin_control(state, body.inputs)
        vin = [{"txid": u["txid"], "vout": u["vout"]} for u in coin_control]

    try:
        raw = await state.rpc.call("createrawtransaction", vin, outputs)
        fund_opts = {"add_inputs": False} if coin_control is not None else None
        funded = (await state.rpc.call("fundrawtransaction", raw, fund_opts)
                 if fund_opts else await state.rpc.call("fundrawtransaction", raw))
        signed = await state.rpc.call("signrawtransactionwithwallet", funded["hex"])
        if not signed.get("complete"):
            raise HTTPException(status_code=422, detail="signing incomplete — wallet may be locked")
        tx_hex = signed["hex"]
        txid = signed.get("txid") or await _txid_from_hex(state, tx_hex)

        accepted = await state.rpc.call("testmempoolaccept", [tx_hex])
        ok = bool(accepted and accepted[0].get("allowed"))

        if not body.confirm:
            # The fee the node chose while funding. Shown BEFORE the user
            # commits; the confirm phase rebuilds the identical transaction
            # (txid is verified below), so this is the fee that gets paid.
            fee = _fee_from_funded(funded)
            sent = sum((amt for _, amt in parsed), Decimal(0))
            db.audit(state.settings.db_path, "send_preview", sess.username,
                     detail=f"txid={txid} recipients={len(parsed)}"
                            + (f" coin_control={len(coin_control)}inputs" if coin_control else ""))
            return {
                "preview": True,
                "txid": txid,
                "mempool_ok": ok,
                "fee": None if fee is None else f"{fee:.9f}",
                "total": None if fee is None else f"{sent + fee:.9f}",
                "rejection": None if ok else (accepted[0].get("reject-reason") or "rejected"),
                "recipients": [
                    {"address": a, "amount": f"{amt:.9f}", "amount_b3": str(amt)}
                    for a, amt in parsed
                ],
                "coin_control": None if coin_control is None else {
                    "count": len(coin_control),
                    "total": str(sum((Decimal(str(u["amount"])) for u in coin_control), Decimal(0))),
                },
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


def _fee_from_funded(funded: dict) -> Decimal | None:
    """Fee reported by fundrawtransaction, as an exact Decimal, or None if the
    node did not say (the UI then shows no fee line rather than a guess)."""
    raw = funded.get("fee") if isinstance(funded, dict) else None
    if raw is None:
        return None
    try:
        fee = Decimal(str(raw))
    except ArithmeticError:
        return None
    return fee if fee.is_finite() and fee >= 0 else None


async def _txid_from_hex(state: AppState, tx_hex: str) -> str:
    """Fallback txid extraction if signrawtransactionwithwallet omits it -
    real Bitcoin Core / b3coind never includes one; only this project's
    own test mock ever did, which hid this path from every test until a
    real send hit it. decoderawtransaction takes the raw tx HEX itself
    (any valid transaction, need not be known to the node); it is NOT
    getrawtransaction, which takes a 64-char TXID of an already-indexed
    transaction and rejects anything else with RPC -8."""
    dec = await state.rpc.call("decoderawtransaction", tx_hex)
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
    """Stop staking. Needs NO unlocked wallet: the staker copied its own
    signing material at Start (same reason staking survives re-lock), so
    stopping works while locked. Session + 2FA + CSRF, then audit-log it.
    Moving staked coins back (unstake) is the step that needs unlocking.

    If autostake is on, it gets turned off here too: otherwise its next
    background pass would silently call startstaking again, undoing the
    user's own action without them expecting it."""
    state = _state(request)
    sess = state.require_session(request)
    try:
        await state.rpc.call("stopstaking")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "staking_stop", sess.username)
    disabled = autostake.disable_for_manual_action(
        state.settings.db_path, sess.username, "stop_staking",
        "manually stopped staking")
    return {"ok": True, "staking": False, "autostake_disabled": disabled}
