"""Full-wallet endpoints: receive, labels, UTXOs, tx details, sign/verify,
passphrase change. Same gating rules as wallet.py:
- read endpoints: session + 2FA; mutations: + CSRF; signing: + unlocked."""

import re
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.deps import AppState
from app.ratelimit import limiter
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable, parse_amount

router = APIRouter(prefix="/api/wallet", tags=["wallet"])

_ADDRESS_RE = re.compile(r"^S[1-9A-HJ-NP-Za-km-z]{26,34}$")
_TXID_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _validate_address(address: str) -> None:
    if not _ADDRESS_RE.match(address):
        raise HTTPException(status_code=400, detail="invalid B3 P2PKH address")


def _client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", "") or (
        request.client.host if request.client else "")


class NewAddressBody(BaseModel):
    label: str = ""


@router.post("/receive")
async def receive_address(body: NewAddressBody, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    label = (body.label or "").strip()[:64]
    try:
        if label:
            addr = await state.rpc.call("getnewaddress", label)
        else:
            addr = await state.rpc.call("getnewaddress")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "receive_address", sess.username,
             detail="label=" + label)
    return {"address": addr, "label": label}


class LabelBody(BaseModel):
    address: str
    label: str


@router.post("/label")
async def set_label(body: LabelBody, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    _validate_address(body.address)
    label = (body.label or "").strip()[:64]
    try:
        await state.rpc.call("setlabel", body.address, label)
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "set_label", sess.username,
             detail="address=" + body.address + " label=" + label)
    return {"ok": True}


@router.get("/utxos")
async def utxos(request: Request, minconf: int = 1):
    state = _state(request)
    state.require_2fa(request)
    minconf = max(0, min(minconf, 999999))
    try:
        unspent = await state.rpc.call("listunspent", minconf)
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    out = []
    for u in unspent or []:
        try:
            amt = parse_amount(str(u.get("amount", "0")))
        except (ValueError, ArithmeticError):
            amt = Decimal(0)
        out.append({
            "txid": u.get("txid"), "vout": u.get("vout"),
            "address": u.get("address"), "label": u.get("label", ""),
            "amount": format(amt, ".9f"),
            "confirmations": u.get("confirmations", 0),
            "spendable": bool(u.get("spendable", True)),
            "safe": bool(u.get("safe", True)),
        })
    return {"utxos": out}


@router.get("/tx/{txid}")
async def tx_detail(txid: str, request: Request):
    if not _TXID_RE.match(txid):
        raise HTTPException(status_code=400, detail="invalid txid")
    state = _state(request)
    state.require_2fa(request)
    try:
        tx = await state.rpc.call("gettransaction", txid)
    except RPCError as exc:
        if "Invalid or non-wallet" in exc.message:
            raise HTTPException(status_code=404, detail="not a wallet transaction")
        raise _translate(exc)
    except (RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    return {"transaction": tx}


class SignMessageBody(BaseModel):
    address: str
    message: str


@router.post("/signmessage")
async def sign_message(body: SignMessageBody, request: Request):
    state = _state(request)
    sess = state.require_wallet_unlocked(request)
    ip = _client_ip(request)
    if not limiter.allow("signmessage", ip, limit=10, window_s=60):
        raise HTTPException(status_code=429, detail="too many sign requests, wait a minute")
    _validate_address(body.address)
    message = body.message[:8192]
    try:
        sig = await state.rpc.call("signmessage", body.address, message)
    except RPCError as exc:
        if "Private key" in exc.message or "not found" in exc.message:
            raise HTTPException(status_code=404, detail="address not in wallet")
        raise _translate(exc)
    except (RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "sign_message", sess.username,
             detail="ip=" + ip + " address=" + body.address)
    return {"signature": sig}


class VerifyMessageBody(BaseModel):
    address: str
    signature: str
    message: str


@router.post("/verifymessage")
async def verify_message(body: VerifyMessageBody, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    _validate_address(body.address)
    try:
        ok = await state.rpc.call("verifymessage", body.address,
                               body.signature.strip()[:512], body.message[:8192])
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "verify_message", sess.username,
             detail="address=" + body.address)
    return {"valid": bool(ok)}


class PassphraseChangeBody(BaseModel):
    old_passphrase: str
    new_passphrase: str


@router.post("/passphrase-change")
async def passphrase_change(body: PassphraseChangeBody, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    ip = _client_ip(request)
    if not limiter.allow("passphrase_change", ip, limit=3, window_s=3600):
        raise HTTPException(status_code=429, detail="too many attempts, try later")
    if len(body.new_passphrase) < 8:
        raise HTTPException(status_code=400,
                            detail="new passphrase must be at least 8 characters")
    if body.new_passphrase == body.old_passphrase:
        raise HTTPException(status_code=400, detail="new passphrase must differ")
    try:
        await state.rpc.call("walletpassphrasechange",
                           body.old_passphrase, body.new_passphrase)
    except RPCError as exc:
        db.audit(state.settings.db_path, "passphrase_change", sess.username,
                 detail="ip=" + ip, success=False)
        raise HTTPException(status_code=401, detail="wrong passphrase")
    except (RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    sess.wallet_unlocked_until = 0.0
    db.audit(state.settings.db_path, "passphrase_change", sess.username,
             detail="ip=" + ip)
    return {"ok": True}


@router.get("/book")
async def address_book(request: Request):
    """Address book: all wallet addresses with labels and amounts.
    One listaddressgroupings call; label key tolerated for both
    Bitcoin-31 (label) and legacy (account) shapes."""
    state = _state(request)
    state.require_2fa(request)
    try:
        groupings = await state.rpc.call("listaddressgroupings")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    out = []
    seen = set()
    for grp in groupings or []:
        for e in grp:
            addr = e.get("address")
            if not addr or addr in seen:
                continue
            seen.add(addr)
            try:
                amt = parse_amount(str(e.get("amount", "0")))
            except (ValueError, ArithmeticError):
                amt = Decimal(0)
            out.append({
                "address": addr,
                "label": e.get("label", "") or e.get("account", "") or "",
                "amount": format(amt, ".9f"),
            })
    return {"addresses": out}
