"""Full-wallet endpoints: receive, labels, UTXOs, tx details, sign/verify,
passphrase change. Same gating rules as wallet.py:
- read endpoints: session + 2FA; mutations: + CSRF; signing: + unlocked."""

import os
import re
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
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


def _data_dir(state: AppState):
    # v0.6.0: the node datadir (wallets/, wallet.dat) lives under node/;
    # flat layouts were migrated there by the entrypoint at boot.
    from pathlib import Path
    return Path(state.settings.node_datadir)


def _wallets_dir(state: AppState):
    # Bitcoin-31 named wallets live under datadir/wallets/; the legacy
    # datadir/wallet.dat is also recognized as a loadable wallet file.
    return _data_dir(state) / "wallets"


class NewAddressBody(BaseModel):
    label: str = ""


# --- Wallet management (create / load / migrate / backup) --------------------
# These are regular wallet features, NOT setup-wizard-only actions.

_WALLET_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


class CreateWalletBody(BaseModel):
    wallet_name: str
    passphrase: str
    load_on_startup: bool = True


class LoadWalletBody(BaseModel):
    filename: str
    load_on_startup: bool = False


class UnloadWalletBody(BaseModel):
    filename: str


@router.get("/manage")
async def wallet_manage(request: Request):
    """Wallet inventory: loaded wallets, on-disk wallet files (named
    wallets under datadir/wallets plus the legacy wallet.dat), and the
    default wallet directory. Never exposes keys or passphrases."""
    state = _state(request)
    state.require_2fa(request)
    loaded: list[str] = []
    try:
        loaded = await state.rpc.call("listwallets")
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        loaded = []
    # v0.6.6: modern B3 wallets are SUBDIRECTORIES (wallets/<name>/wallet.dat
    # plus -journal). Ask the node first via listwalletdir (node truth,
    # handles both layouts); fall back to a directory-aware disk scan
    # when the node is down.
    on_disk: list[str] = []
    try:
        wd = await state.rpc.call("listwalletdir")
        on_disk = sorted(e.get("name") for e in (wd or {}).get("wallets", [])
                          if e.get("name") is not None)
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        wallets_dir = _wallets_dir(state)
        if wallets_dir.is_dir():
            for p in sorted(wallets_dir.iterdir()):
                if p.is_file() and p.suffix == ".dat":
                    on_disk.append(p.name)
                elif p.is_dir() and (p / "wallet.dat").is_file():
                    on_disk.append(p.name)
        legacy = _data_dir(state) / "wallet.dat"
        if legacy.is_file() and "" not in on_disk:
            on_disk.insert(0, "wallet.dat")
    return {"loaded": loaded or [], "on_disk": on_disk,
            "persistent_data": state.data_persistent()}


@router.post("/manage/create")
async def wallet_manage_create(body: CreateWalletBody, request: Request):
    """Create a new wallet (named, passphrase-encrypted). Regular feature
    mirrored from the setup wizard; requires persistent data so a new
    wallet can never silently land on ephemeral container storage."""
    state = _state(request)
    sess = state.require_csrf(request)
    state.require_persistent_data()
    ip = _client_ip(request)
    name = (body.wallet_name or "").strip()
    if not _WALLET_NAME_RE.fullmatch(name):
        raise HTTPException(422, "invalid wallet name")
    passphrase = (body.passphrase or "").strip()
    if len(passphrase) < 8:
        raise HTTPException(422, "passphrase must be at least 8 characters")
    try:
        await state.rpc.call(
            "createwallet", name, False, False, passphrase,
            False, True, body.load_on_startup, False,
        )
    except RPCError as exc:
        db.audit(state.settings.db_path, "wallet.create", sess.username,
                 detail=f"ip={ip} name={name}", success=False)
        raise HTTPException(409, f"node rejected createwallet: {exc.message}")
    except (RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "wallet.create", sess.username,
             detail=f"ip={ip} name={name}")
    return {"ok": True, "wallet": name}


@router.post("/manage/load")
async def wallet_manage_load(body: LoadWalletBody, request: Request):
    """Load (migrate) an existing wallet file from the wallet directory
    or the legacy datadir location. Regular feature; persistence-guarded."""
    state = _state(request)
    sess = state.require_csrf(request)
    state.require_persistent_data()
    ip = _client_ip(request)
    raw = (body.filename or "").strip()
    # v0.6.6: loadwallet accepts a wallet name OR a path (its own help
    # shows "/path/to/walletname/"). Users moving a wallet from another
    # machine point at it directly instead of editing settings.json by
    # hand with the node down. Paths must resolve INSIDE the data dir.
    target = ""
    if "/" in raw or raw.startswith("."):
        from pathlib import Path, PurePosixPath
        data_root = _data_dir(state).resolve()
        if raw.startswith("/"):
            abs_cand = Path(raw).resolve()
        else:
            abs_cand = (data_root / PurePosixPath(raw)).resolve()
        try:
            abs_cand.relative_to(data_root)
        except ValueError:
            raise HTTPException(422, "path must stay inside the data directory")
        if not (abs_cand / "wallet.dat").is_file() and abs_cand.suffix != ".dat":
            raise HTTPException(422, "no wallet.dat found at that path")
        target = str(abs_cand)
        name = abs_cand.name
    else:
        name = raw
        if not _WALLET_NAME_RE.fullmatch(name):
            raise HTTPException(422, "invalid wallet filename")
        target = name
    try:
        await state.rpc.call("loadwallet", target, body.load_on_startup)
    except RPCError as exc:
        db.audit(state.settings.db_path, "wallet.load", sess.username,
                 detail=f"ip={ip} name={name}", success=False)
        raise HTTPException(409, f"node rejected loadwallet: {exc.message}")
    except (RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "wallet.load", sess.username,
             detail=f"ip={ip} name={name}")
    return {"ok": True, "wallet": name}


@router.post("/manage/unload")
async def wallet_manage_unload(body: UnloadWalletBody, request: Request):
    """Unload a loaded wallet. Refuses to unload the last loaded wallet
    (the UI relies on at least one active wallet context)."""
    state = _state(request)
    sess = state.require_csrf(request)
    ip = _client_ip(request)
    try:
        loaded = await state.rpc.call("listwallets")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    if len(loaded or []) <= 1:
        raise HTTPException(409, "cannot unload the last loaded wallet")
    name = (body.filename or "").strip()
    if name not in (loaded or []):
        raise HTTPException(404, "wallet not loaded")
    try:
        await state.rpc.call("unloadwallet", name)
    except RPCError as exc:
        raise HTTPException(409, f"node rejected unloadwallet: {exc.message}")
    except (RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "wallet.unload", sess.username,
             detail=f"ip={ip} name={name}")
    return {"ok": True}


@router.post("/manage/backup")
async def wallet_manage_backup(request: Request):
    """Backup the active wallet via backupwallet. The destination path is
    server-generated (timestamped, under datadir/backups) — the browser
    never controls it. Contains private keys: persistence-guarded and
    audited. Requires an unlocked wallet only if the node says so; the
    backend never touches passphrases here."""
    state = _state(request)
    sess = state.require_csrf(request)
    state.require_persistent_data()
    ip = _client_ip(request)
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backups_dir = _data_dir(state) / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    dest = backups_dir / f"backup-{stamp}.dat"
    try:
        await state.rpc.call("backupwallet", str(dest))
    except RPCError as exc:
        db.audit(state.settings.db_path, "wallet.backup", sess.username,
                 detail=f"ip={ip}", success=False)
        raise HTTPException(409, f"node rejected backupwallet: {exc.message}")
    except (RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "wallet.backup", sess.username,
             detail=f"ip={ip} dest={dest.name}")
    return {"ok": True, "path": str(dest), "file": dest.name}


@router.get("/manage/backup/download")
async def wallet_backup_download(request: Request, file: str = ""):
    """Stream a previously-created backup file to the browser as a download.
    The filename is validated to stay inside the backups directory — no
    path traversal. Contains private keys: persistence-guarded, audited.
    CSRF-protected (double-submit) so only the SPA can fetch it."""
    state = _state(request)
    sess = state.require_csrf(request)
    state.require_persistent_data()
    ip = _client_ip(request)
    backups_dir = _data_dir(state) / "backups"
    safe_name = os.path.basename(file)
    if not safe_name or safe_name != file:
        raise HTTPException(status_code=400, detail="invalid filename")
    dest = backups_dir / safe_name
    if not dest.is_file():
        raise HTTPException(status_code=404, detail="backup file not found")
    db.audit(state.settings.db_path, "wallet.backup_download", sess.username,
             detail=f"ip={ip} file={safe_name}")
    return FileResponse(str(dest), filename=safe_name,
                        media_type="application/octet-stream")


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

    Descriptor wallets (which the setup wizard creates with
    descriptors=true) do not support listaddressgroupings, so
    listreceivedbyaddress is the primary path; listaddressgroupings
    is the fallback for legacy wallets. Stake owner addresses from
    getstakinginfo are merged in so the user sees their staking
    address even though it never appears in receive listings."""
    state = _state(request)
    state.require_2fa(request)
    out = []
    seen = set()

    # Primary: listreceivedbyaddress (works on descriptor wallets).
    try:
        received = await state.rpc.call("listreceivedbyaddress", 0, True, True)
        for e in received or []:
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
                "label": e.get("label", "") or "",
                "amount": format(amt, ".9f"),
            })
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        # Fallback: listaddressgroupings (legacy wallets only).
        try:
            groupings = await state.rpc.call("listaddressgroupings")
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
        except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
            raise _translate(exc)

    # Merge stake owner addresses so the staking address is visible.
    try:
        info = await state.rpc.call_optional("getstakinginfo") or {}
        for s in info.get("stakes") or []:
            addr = s.get("owner_address")
            if not addr:
                continue
            if addr in seen:
                for entry in out:
                    if entry["address"] == addr:
                        entry["stake"] = True
                        break
            else:
                seen.add(addr)
                try:
                    amt = parse_amount(str(s.get("amount", "0")))
                except (ValueError, ArithmeticError):
                    amt = Decimal(0)
                out.append({
                    "address": addr,
                    "label": "staking",
                    "amount": format(amt, ".9f"),
                    "stake": True,
                })
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        pass  # stakes are optional enrichment

    # True balance = sum of the wallet's own unspent outputs per address.
    # "amount" above is lifetime RECEIVED, which does not drop when coins
    # are spent. Display-only; every spend path re-derives its own inputs.
    # None (not 0) when the node cannot say, so the UI shows "unknown".
    balances: dict | None
    try:
        unspent = await state.rpc.call("listunspent", 0)
        balances = {}
        for u in unspent or []:
            addr = u.get("address")
            if not addr or not u.get("spendable", True):
                continue
            try:
                val = Decimal(str(u.get("amount", "0")))
            except ArithmeticError:
                continue
            if val.is_finite() and val > 0:
                balances[addr] = balances.get(addr, Decimal(0)) + val
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        balances = None
    for entry in out:
        entry["balance"] = (None if balances is None
                            else format(balances.get(entry["address"], Decimal(0)), ".9f"))

    return {"addresses": out}
