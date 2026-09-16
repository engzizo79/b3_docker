"""FN Coin / FlowMesh asset endpoints.

Read surface (verified against B3-CoinV2 v1.1.5 source):
- getassetstate: chain-level FN/colored activation + issuance state
- getwalletassets: this wallet's FN/bridge/colored balances + UTXOs
  (amounts are exact integer base units; FN has 0 decimals, bUSD has 6)
- listflowmeshmarkets: anchor-final colored-asset/B3 markets
- getflowmeshmarketdata: bounded certified market snapshot (head + history)

Write surface (Advanced mode, unlock-gated, audited):
- startflowmeshvalidator / stopflowmeshvalidator
"""

from fastapi import APIRouter, HTTPException, Request

from app.deps import AppState
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable
from app import db

router = APIRouter(prefix="/api/assets", tags=["assets"])


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
