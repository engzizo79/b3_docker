"""Expert console - user-controlled trust model (v0.4.6).

NON-NEGOTIABLE: the browser NEVER talks to the node. The console UI
POSTs a command string to /api/console/run and the BACKEND executes it.
What the backend is willing to execute depends on the client's trust
context - the operator decides where the line is:

- localhost: FULL access. Allow everything, warn but never prohibit.
  The operator owns the machine and the coins; the platform gives an
  honest warning (frontend confirm for danger commands) and stays out
  of the way.
- allowlisted networks (runtime-editable from a full-trust client via
  /api/console/settings): treated exactly like localhost.
- other remotes: 2FA is always required. With 2FA they get full access
  when the operator enabled remote_full_access (default ON, warned
  about), otherwise read-only.

Restricted mode falls back to the read-only list via call(), so the RPC
allowlist remains the hard boundary for everything except full-trust
console runs (call_unrestricted, console-only).

Danger commands are NEVER blocked in full mode - the platform's job is
an honest warning, redacted audit logging, and the operator's
responsibility.
"""

import ipaddress
import json
import shlex
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.deps import AppState
from app.ratelimit import limiter
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable
from app.session import client_is_localhost, effective_client_ip

router = APIRouter(prefix="/api/console", tags=["console"])

# Read-only surface: what restricted remote sessions may run.
CONSOLE_READ: set[str] = {
    "getblockchaininfo", "getnetworkinfo", "getblock", "getblockheader",
    "getblockstats", "getblockhash", "getblockcount", "getbestblockhash",
    "getdifficulty", "gettxout", "gettxoutproof", "getrawtransaction",
    "verifytxoutproof", "getmempoolinfo", "getrawmempool",
    "estimatesmartfee", "gettxoutsetinfo", "getindexinfo",
    "getpeerinfo", "validateaddress", "getfinalitystatus", "getbridgeinfo",
    "getassetstate", "getstakinginfo", "getfinalityinfo", "getwalletassets",
    "listflowmeshmarkets", "getflowmeshmarketdata",
    "getwalletinfo", "getbalances", "getbalance", "listunspent",
    "getaddressesbylabel", "listaddressgroupings", "listreceivedbyaddress",
    "listtransactions", "gettransaction", "listlabels", "getnewaddress",
    "verifymessage", "help",
}

# Danger catalog - warnings, NOT blocks. In full mode every command runs;
# the frontend shows this text in a confirm dialog before sending. The
# backend uses _REDACT_RESULT to keep secrets out of the audit log.
CONSOLE_DANGER: dict[str, str] = {
    "dumpprivkey": "Exports a private key to this screen. Anyone who sees it can steal those coins.",
    "dumpwallet": "Exports ALL private keys to a file. A leak means total wallet compromise.",
    "importprivkey": "Imports a private key - make sure you trust its source.",
    "importmulti": "Imports keys/addresses in bulk - make sure you trust the source.",
    "signmessagewithprivkey": "Signs with a raw private key typed into the console.",
    "walletpassphrase": "Keeps the wallet unlocked on the node for the given time window.",
    "walletpassphrasechange": "Changes the wallet passphrase - the autostake vault must be re-synced (Settings does both).",
    "encryptwallet": "Encrypts the wallet - the autostake vault must be re-synced afterwards.",
    "sendtoaddress": "Sends coins immediately. Irreversible.",
    "sendmany": "Sends coins immediately. Irreversible.",
    "sendall": "Sweeps coins to one address. Irreversible.",
    "sendrawtransaction": "Broadcasts a signed transaction. Irreversible.",
    "createrawtransaction": "Builds an unsigned transaction from hand-written JSON.",
    "fundrawtransaction": "Funds an unsigned transaction from the wallet.",
    "signrawtransactionwithwallet": "Signs a transaction with wallet keys.",
    "createstake": "Locks coins in a stake.",
    "startstaking": "Turns on the staking loop.",
    "stopstaking": "Turns off the staking loop (staked coins stay locked).",
    "bindfinalitykey": "Binds this wallet's finality key (transaction fee applies).",
    "revokefinalitykey": "Submits a finality-key revocation (fee applies; not a recovery step).",
    "stop": "Stops the node - the container supervisor restarts it.",
    "startflowmeshvalidator": "Starts a FlowMesh validator.",
    "stopflowmeshvalidator": "Stops a FlowMesh validator.",
}

_REDACT_RESULT: set[str] = {"dumpprivkey", "dumpwallet", "importprivkey"}
_WALLET_LOCKED_CODE = -13  # bitcoin-core family: wallet locked
_MAX_UNLOCK_S = 3600


class ConsoleBody(BaseModel):
    command: str


class ConsoleSettingsBody(BaseModel):
    networks: str
    remote_full_access: bool


def _state(request: Request) -> AppState:
    return request.app.state.app_state


def _console_settings(state: AppState) -> dict:
    seed = getattr(state.settings, "console_networks", "") or ""
    return db.get_console_settings(state.settings.db_path, seed_networks=seed)


def _parse_networks(raw: str) -> list:
    nets = []
    for tok in (raw or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            nets.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            continue
    return nets


def _trust_mode(state: AppState, request: Request, sess) -> str:
    """'full' or 'restricted'; 403 with remediation for untrusted
    remotes. require_csrf already proved session + 2FA; this decides
    how much of the console applies. 2FA remotes get full access when
    the operator enabled remote_full_access, otherwise read-only."""
    if client_is_localhost(request):
        return "full"
    ip = effective_client_ip(request)
    cfg = _console_settings(state)
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        raise HTTPException(status_code=403,
            detail="console is available from trusted networks only")
    if any(addr in net for net in _parse_networks(cfg["networks"])):
        return "full"
    if cfg["remote_full_access"] and sess.two_fa_verified:
        return "full"
    if sess.two_fa_verified:
        return "restricted"
    raise HTTPException(status_code=403,
        detail="console is available from trusted networks only - "
              "ask the operator to add your network in Console settings")


def _summarize(method: str, result) -> str:
    if method in _REDACT_RESULT:
        return "[result redacted: key material]"
    try:
        text = json.dumps(result, default=str)
    except (TypeError, ValueError):
        text = str(result)
    if len(text) > 80:
        text = text[:77] + "..."
    return text


@router.get("/catalog")
async def console_catalog(request: Request):
    """What this client can run + danger warnings + settings visibility."""
    state = _state(request)
    sess = state.require_2fa(request)
    mode = _trust_mode(state, request, sess)
    cfg = _console_settings(state)
    from app.rpc import ALLOWED_METHODS, FORBIDDEN
    full_list = set(ALLOWED_METHODS) - FORBIDDEN
    full_list |= set(CONSOLE_DANGER)  # QT parity: danger cmds run with a warning
    full_list.discard("getaccountaddress")
    return {
        "mode": mode,
        "runnable": sorted(full_list) if mode == "full" else sorted(CONSOLE_READ),
        "danger": dict(sorted(CONSOLE_DANGER.items())),
        "networks": cfg["networks"],
        "remote_full_access": cfg["remote_full_access"],
        "editable": mode == "full",
    }


@router.get("/settings")
async def console_get_settings(request: Request):
    state = _state(request)
    state.require_2fa(request)
    cfg = _console_settings(state)
    return {"networks": cfg["networks"],
            "remote_full_access": cfg["remote_full_access"],
            "editable": _trust_mode(state, request,
                                     state.require_2fa(request)) == "full"}


def _parses_as_network(tok: str) -> bool:
    try:
        ipaddress.ip_network(tok, strict=False)
        return True
    except ValueError:
        return False


@router.put("/settings")
async def console_put_settings(request: Request, body: ConsoleSettingsBody):
    """Edit the trust config from a full-trust client. The operator
    accepts the tradeoff - the platform warns, the user decides."""
    state = _state(request)
    sess = state.require_csrf(request)
    mode = _trust_mode(state, request, sess)
    if mode != "full":
        raise HTTPException(status_code=403,
            detail="console settings are editable from a trusted network only")
    raw = (body.networks or "").strip()
    if raw == "*":
        raise HTTPException(status_code=400,
            detail="'*' is not accepted - list networks explicitly "
                  "(0.0.0.0/0,::/0 would trust every network)")
    if raw:
        bad = [tok.strip() for tok in raw.split(",")
               if tok.strip() and not _parses_as_network(tok.strip())]
        if bad:
            raise HTTPException(status_code=400,
                detail="not valid networks: " + ", ".join(bad))
    db.set_console_settings(state.settings.db_path, raw,
                             bool(body.remote_full_access))
    db.audit(state.settings.db_path, "console_settings", sess.username,
             detail=f"networks={raw or '(env seed)'} "
                    f"remote_full_access={int(bool(body.remote_full_access))}")
    cfg = _console_settings(state)
    return {"ok": True, "networks": cfg["networks"],
            "remote_full_access": cfg["remote_full_access"],
            "editable": True}


@router.post("/run")
async def console_run(request: Request, body: ConsoleBody):
    """Run one console command: METHOD [json args] (QT-style).

    Security: session + 2FA + CSRF; trust-mode gate; rate-limited;
    audit-logged with secret redaction. Full mode uses call_unrestricted
    (console-only escape from the allowlist); restricted mode keeps the
    read-only allowlist via call()."""
    state = _state(request)
    sess = state.require_csrf(request)
    mode = _trust_mode(state, request, sess)
    ip = effective_client_ip(request)
    if not limiter.allow("console", ip, limit=30, window_s=60):
        raise HTTPException(status_code=429, detail="console is busy - wait a moment")

    raw = (body.command or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="empty command")
    if len(raw) > 500:
        raise HTTPException(status_code=400, detail="command too long")
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"unterminated quote: {exc}")
    method, arg_tokens = parts[0], parts[1:]
    if not method.isidentifier():
        raise HTTPException(status_code=400, detail="not a valid command name")

    params: list = []
    for tok in arg_tokens:
        try:
            params.append(json.loads(tok))
        except json.JSONDecodeError:
            params.append(tok)

    if mode == "restricted":
        if method not in CONSOLE_READ:
            db.audit(state.settings.db_path, "console_restricted", sess.username,
                     detail=f"method={method} ip={ip}")
            raise HTTPException(status_code=403,
                detail="read-only mode from this network - full access needs "
                      "an allowlisted network or the operator's remote policy")
        result = await _run_allowlisted(state, method, params)
    else:
        # Full trust: QT parity. walletpassphrase keeps its scoped handler
        # (redaction, cap, session mirror) for consistency with the UI.
        if method == "walletpassphrase":
            return await _console_unlock(state, sess, ip, params)
        try:
            result = await state.rpc.call_unrestricted(method, *params)
        except RPCError as exc:
            if exc.code == _WALLET_LOCKED_CODE:
                raise HTTPException(status_code=423, detail="wallet is locked")
            db.audit(state.settings.db_path, "console_error", sess.username,
                     detail=f"method={method} ip={ip}")
            raise HTTPException(status_code=502, detail=f"node error: {exc.message}")
        except RPCUnavailable:
            raise HTTPException(status_code=503, detail="node unavailable (sync window?)")
        if method == "walletlock":
            sess.wallet_unlocked_until = 0

    db.audit(state.settings.db_path, "console_run", sess.username,
             detail=f"ip={ip} mode={mode} {method} {_summarize(method, result)}")
    return {"result": result}


async def _run_allowlisted(state: AppState, method: str, params: list):
    try:
        return await state.rpc.call(method, *params)
    except RPCError as exc:
        if exc.code == _WALLET_LOCKED_CODE:
            raise HTTPException(status_code=423, detail="wallet is locked")
        raise HTTPException(status_code=502, detail=f"node error: {exc.message}")
    except RPCNotAllowed:
        raise HTTPException(status_code=403,
            detail="not on the console allowlist: " + method)
    except RPCUnavailable:
        raise HTTPException(status_code=503, detail="node unavailable (sync window?)")


async def _console_unlock(state: AppState, sess, ip: str, params: list):
    """walletpassphrase from the console: like POST /wallet/unlock but
    QT-style positional args. 2FA is already proven; the passphrase never
    reaches the audit log; the timeout is capped; the session unlock window
    mirrors the node so every gated API stays consistent."""
    state.require_persistent_data()
    if not limiter.allow("unlock", ip, limit=5, window_s=60):
        raise HTTPException(status_code=429, detail="too many attempts, wait a minute")
    if not params:
        raise HTTPException(status_code=400,
            detail='usage: walletpassphrase "passphrase" timeout')
    passphrase = params[0]
    if not isinstance(passphrase, str) or not passphrase:
        raise HTTPException(status_code=400, detail="passphrase required")
    timeout = state.settings.wallet_unlock_timeout
    if len(params) > 1:
        try:
            timeout = int(params[1])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="timeout must be an integer")
    timeout = max(1, min(timeout, _MAX_UNLOCK_S))

    try:
        await state.rpc.call("walletpassphrase", passphrase, timeout)
    except RPCError as exc:
        db.audit(state.settings.db_path, "wallet_unlock", sess.username,
                 detail=f"ip={ip} via console", success=False)
        raise HTTPException(status_code=403, detail="wrong passphrase")
    except (RPCNotAllowed, RPCUnavailable):
        raise HTTPException(status_code=503, detail="node unavailable (sync window?)")

    sess.wallet_unlocked_until = time.time() + timeout
    db.audit(state.settings.db_path, "wallet_unlock", sess.username,
             detail=f"ip={ip} via console")
    return {"result": {"ok": True, "unlocked_for_s": timeout,
        "note": f"wallet unlocked for {timeout}s (capped at {_MAX_UNLOCK_S}s)"}}
