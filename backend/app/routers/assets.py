"""FN Coin / FlowMesh asset endpoints.

Read surface (verified against B3-CoinV2 v1.1.5 source):
- getassetstate: chain-level FN/colored activation + issuance state
- getwalletassets: this wallet's FN/bridge/colored balances + UTXOs
  (amounts are exact integer base units; FN has 0 decimals, bUSD has 6)
- listflowmeshmarkets: anchor-final colored-asset/B3 markets
- getflowmeshmarketdata: bounded certified market snapshot (head + history)

Write surface (Advanced mode, unlock-gated, audited):
- startflowmeshvalidator / stopflowmeshvalidator
- createfncoin (destroys native B3 to mint exactly one FN Coin)
"""

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.deps import AppState
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable
from app import db

router = APIRouter(prefix="/api/assets", tags=["assets"])

_ADDRESS_RE = re.compile(r"^S[1-9A-HJ-NP-Za-km-z]{26,34}$")


class FnCreateBody(BaseModel):
    address: str | None = None


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


@router.get("")
async def assets_overview(request: Request):
    """Wallet asset balances + FN chain state, one round trip."""
    state = _state(request)
    state.require_2fa(request)
    try:
        wallet_assets = await state.rpc.call("getwalletassets")
        asset_state = await state.rpc.call("getassetstate")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    # getwalletassets returns null when no wallet is loaded — surface as empty.
    if wallet_assets is None:
        wallet_assets = {"assets": []}
    assets = []
    for a in wallet_assets.get("assets") or []:
        assets.append({
            "asset_id": a.get("asset_id"),
            "kind": a.get("kind"),
            "ticker": a.get("ticker"),
            "name": a.get("name"),
            "decimals": a.get("decimals", 0),
            "confirmed": a.get("confirmed", 0),
            "unconfirmed": a.get("unconfirmed", 0),
            "spendable": a.get("spendable", 0),
        })
    fn_state = asset_state.get("fn", {}) if asset_state else {}
    return {
        "assets": assets,
        "fn": {
            "configured": fn_state.get("configured", False),
            "active": fn_state.get("active", False),
            "asset_id": fn_state.get("asset_id"),
            "modern_issued": fn_state.get("modern_issued"),
            "modern_capacity": fn_state.get("modern_capacity"),
            "pod_active": fn_state.get("pod_active", False),
# counter_known gates createfncoin on the node (getassetstate);
# dropping it made the UI claim an eternal FN-counter sync.
"counter_known": fn_state.get("counter_known", True),
# Legacy-era FN coins predate the modern pod counter.
"historical_issued": fn_state.get("historical_issued", 0),
        },
        "wallet_loaded": state.rpc.wallet_loaded() if hasattr(state.rpc, "wallet_loaded") else None,
    }


@router.get("/markets")
async def assets_markets(request: Request):
    """FlowMesh market list (read-only)."""
    state = _state(request)
    state.require_2fa(request)
    try:
        markets = await state.rpc.call("listflowmeshmarkets")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    return {"markets": markets or []}


@router.post("/validator/start")
async def validator_start(request: Request):
    """Start the FlowMesh validator (Advanced, unlock-gated, audited)."""
    state = _state(request)
    sess = state.require_wallet_unlocked(request)
    try:
        result = await state.rpc.call("startflowmeshvalidator")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "flowmesh_validator_start", sess.username)
    return result or {"started": True}


@router.post("/validator/stop")
async def validator_stop(request: Request):
    """Stop the FlowMesh validator (Advanced, unlock-gated, audited)."""
    state = _state(request)
    sess = state.require_wallet_unlocked(request)
    try:
        result = await state.rpc.call("stopflowmeshvalidator")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    db.audit(state.settings.db_path, "flowmesh_validator_stop", sess.username)
    return result or {"stopped": True}


@router.post("/fn/create")
async def fn_create(body: FnCreateBody, request: Request):
    """Create one FN Coin. IRREVERSIBLE: the node destroys a consensus-pinned
    amount of native B3 (tiered by slot). Unlock-gated, audited; the UI shows
    the cost and requires explicit acknowledgement before calling this."""
    state = _state(request)
    sess = state.require_wallet_unlocked(request)
    params: list = []
    address = (body.address or "").strip()
    if address:
        if not _ADDRESS_RE.match(address):
            raise HTTPException(status_code=400, detail="invalid B3 P2PKH address")
        params.append(address)
    try:
        result = await state.rpc.call("createfncoin", *params)
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        db.audit(state.settings.db_path, "fn_create", sess.username,
                 detail=f"failed: {type(exc).__name__}", success=False)
        raise _translate(exc)
    db.audit(state.settings.db_path, "fn_create", sess.username,
             detail=f"txid={(result or {}).get('txid')} tier={(result or {}).get('tier')}")
    return result or {}
