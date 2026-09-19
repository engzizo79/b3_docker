"""Expert console: QT-style command line, allowlist-gated.

Three tiers:
- READ: full inspection surface, works while the wallet is locked.
- UNLOCK: needs an unlocked wallet (e.g. signmessage) — returns 423 so
  the point-of-action passphrase prompt opens and the command retries
  after unlock, exactly like the dedicated flows.
- BLOCKED: money-affecting / key-lifecycle / node-control commands —
  rejected with guidance pointing to the dedicated UI flow, because a
  web console must keep confirmation flows, scoping and the audit log.

walletpassphrase/walletlock are BLOCKED here on purpose: unlock windows
are changed ONLY through the backend's scoped, audited unlock flow.
"""

import ipaddress
import json
import shlex

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import db
from app.deps import AppState
from app.ratelimit import limiter
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable

router = APIRouter(prefix="/api/console", tags=["console"])

# Tier 1 — inspection only; safe while locked.
CONSOLE_READ: set[str] = {
    # chain / network
    "getblockchaininfo", "getnetworkinfo", "getblock", "getblockheader",
    "getblockstats", "getblockhash", "getblockcount", "getbestblockhash",
    "getdifficulty", "gettxout", "gettxoutproof", "getrawtransaction",
    "verifytxoutproof", "getmempoolinfo", "getrawmempool",
    "estimatesmartfee", "gettxoutsetinfo", "getindexinfo",
    "getpeerinfo", "validateaddress", "getfinalitystatus", "getbridgeinfo",
    "getassetstate",
    # staking / finality / assets (read)
    "getstakinginfo", "getfinalityinfo", "getwalletassets",
    "listflowmeshmarkets", "getflowmeshmarketdata",
    # wallet (read)
    "getwalletinfo", "getbalances", "getbalance", "listunspent",
    "getaddressesbylabel", "listaddressgroupings", "listreceivedbyaddress",
    "listtransactions", "gettransaction", "listlabels", "getnewaddress",
    "verifymessage",
    "help",
}

# Tier 2 — safe to run but the NODE requires an unlocked wallet; the
# backend mirrors that with 423 so the passphrase prompt opens.
CONSOLE_UNLOCK: set[str] = {
    "signmessage", "sendall",
}

# Tier 3 — never console-callable; guidance points to the UI flow.
CONSOLE_BLOCKED: dict[str, str] = {
    "walletpassphrase": "Unlock from the action you are performing — the prompt opens automatically where you need it.",
    "walletlock": "Lock via the wallet header lock button (or wait for the unlock window to expire).",
    "walletpassphrasechange": "Use Settings → Change passphrase.",
    "createstake": "Use Staking → Add stake (guided flow with confirmation).",
    "startstaking": "Use Staking → Start staking (guided flow).",
    "stopstaking": "Use Staking → Stop staking (guidance on revoke included).",
    "bindfinalitykey": "Use Staking → the start-staking flow binds the key for you.",
    "revokefinalitykey": "Use Staking → Leaving the validator set? (guided, with the operator checklist).",
    "createrawtransaction": "Use Send — the backend builds and signs safely.",
    "fundrawtransaction": "Use Send — the backend builds and signs safely.",
    "signrawtransactionwithwallet": "Use Send — the backend builds and signs safely.",
    "sendrawtransaction": "Use Send — the backend builds and signs safely.",
    "testmempoolaccept": "The consolidation preview runs this dry-run for you.",
    "setlabel": "Use the Address book (Addresses view).",
    "createwallet": "Use Wallet → Manage wallets → Create.",
    "loadwallet": "Use Wallet → Manage wallets.",
    "unloadwallet": "Use Wallet → Manage wallets.",
    "listwallets": "Use Wallet → Manage wallets.",
    "backupwallet": "Use Wallet → Backup (with browser download).",
    "startflowmeshvalidator": "Use Assets → validator controls.",
    "stopflowmeshvalidator": "Use Assets → validator controls.",
    "getaccountaddress": "Deprecated on this node; use getnewaddress.",
}


class ConsoleBody(BaseModel):
    command: str


def _state(request: Request) -> AppState:
    return request.app.state.app_state


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _trusted_networks(state: AppState) -> list | None:
    """Parse CONSOLE_NETWORKS; None means the operator disabled the gate."""
    raw = (getattr(state.settings, "console_networks", "") or "").strip()
    if raw == "*":
        return None
    nets = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            nets.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            continue  # unparseable entry skipped: fails closed
    return nets


def _require_console_network(state: AppState, request: Request) -> None:
    """Full console access only from trusted networks (default: local
    machine). Fails closed: no match -> 403 regardless of session."""
    nets = _trusted_networks(state)
    if nets is None:
        return
    forwarded = request.headers.get("x-forwarded-for", "")
    ip_text = forwarded.split(",")[0].strip() if forwarded else (
        request.client.host if request.client else "")
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        raise HTTPException(status_code=403,
                            detail="console is available from trusted networks only")
    if not any(ip in net for net in nets):
        raise HTTPException(status_code=403,
                            detail="console is available from trusted networks only")


def _guidance(method: str) -> str:
    return CONSOLE_BLOCKED.get(
        method, "This command is not available in the web console.")


def _extra_methods(state: AppState) -> set[str]:
    """EXTRA_CONSOLE_METHODS: operator-declared additions. These still
    pass through the RPC allowlist choke point, so only methods the
    daemon knows AND the operator explicitly named can run."""
    raw = getattr(state.settings, "extra_console_methods", "") or ""
    return {m.strip() for m in raw.split(",") if m.strip()}


def _tier(state: AppState, method: str) -> str:
    if method in CONSOLE_BLOCKED:
        return "blocked"
    if method in CONSOLE_READ or method in _extra_methods(state):
        return "read"
    if method in CONSOLE_UNLOCK:
        return "unlock"
    return "unknown"


def _summarize(result) -> str:
    """One-line result summary for the audit log (never the full payload)."""
    try:
        text = json.dumps(result, default=str)
    except (TypeError, ValueError):
        text = str(result)
    if len(text) > 80:
        text = text[:77] + "..."
    return text


@router.get("/catalog")
async def console_catalog(request: Request):
    """Method lists for autocomplete + the tier split."""
    state = _state(request)
    state.require_2fa(request)
    _require_console_network(state, request)
    return {
        "read": sorted(CONSOLE_READ),
        "unlock": sorted(CONSOLE_UNLOCK),
        "blocked": {m: _guidance(m) for m in sorted(CONSOLE_BLOCKED)},
        "extra": sorted(_extra_methods(state)),
    }


@router.post("/run")
async def console_run(request: Request, body: ConsoleBody):
    """Run one console command: METHOD [json args...] (QT-style).

    Security: session + 2FA + CSRF; rate-limited; audit-logged with a
    result summary; the RPC allowlist still applies underneath
    (defense in depth — the console tiers are a subset of it).
    """
    state = _state(request)
    sess = state.require_csrf(request)
    _require_console_network(state, request)
    ip = _client_ip(request)
    if not limiter.allow("console", ip, limit=30, window_s=60):
        raise HTTPException(status_code=429, detail="console is busy — wait a moment")

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

    tier = _tier(state, method)
    if tier == "blocked":
        db.audit(state.settings.db_path, "console_blocked", sess.username,
                detail=f"method={method} ip={ip}")
        raise HTTPException(status_code=403, detail=_guidance(method))

    if tier == "unknown":
        db.audit(state.settings.db_path, "console_blocked", sess.username,
                detail=f"method={method} ip={ip}")
        raise HTTPException(status_code=403,
                            detail="not on the console allowlist: " + method)

    # Tier-2 mirrors the node: locked wallet -> 423 -> passphrase prompt
    if tier == "unlock":
        if not sess.wallet_unlocked():
            raise HTTPException(status_code=423, detail="wallet is locked")

    # Parse each token as JSON (QT-style literals; bare words -> string).
    params: list = []
    for tok in arg_tokens:
        try:
            params.append(json.loads(tok))
        except json.JSONDecodeError:
            params.append(tok)

    try:
        result = await state.rpc.call(method, *params)
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        db.audit(state.settings.db_path, "console_error", sess.username,
detail=f"method={method} ip={ip}")
        if isinstance(exc, RPCNotAllowed):
            raise HTTPException(status_code=403,
                                detail="not on the console allowlist: " + method)
        if isinstance(exc, RPCUnavailable):
            raise HTTPException(status_code=503, detail="node unavailable (sync window?)")
        raise HTTPException(status_code=502, detail=f"node error: {exc.message}")

    db.audit(state.settings.db_path, "console_run", sess.username,
detail=f"ip={ip} {method} {_summarize(result)}")
    return {"result": result}