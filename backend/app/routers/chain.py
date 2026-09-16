"""Read-only chain-state endpoints for the dashboard."""

from fastapi import APIRouter, HTTPException, Request

from app.deps import AppState
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable

router = APIRouter(prefix="/api/chain", tags=["chain"])


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


@router.get("/summary")
async def chain_summary(request: Request):
    """Everything the dashboard needs in one round trip."""
    state = _state(request)
    state.require_2fa(request)
    try:
        info = await state.rpc.call("getblockchaininfo")
        net = await state.rpc.call("getnetworkinfo")
        mempool = await state.rpc.call("getmempoolinfo")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    return {
        "blockchain": info,
        "network": net,
        "mempool": mempool,
    }


@router.get("/finality")
async def finality(request: Request):
    state = _state(request)
    state.require_2fa(request)
    try:
        return {"finality": await state.rpc.call("getfinalitystatus")}
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)


@router.get("/bridge")
async def bridge(request: Request):
    state = _state(request)
    state.require_2fa(request)
    try:
        return {"bridge": await state.rpc.call("getbridgeinfo")}
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)


@router.get("/supply")
async def supply(request: Request):
    state = _state(request)
    state.require_2fa( request)
    try:
        return {"supply": await state.rpc.call("gettxoutsetinfo")}
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)


@router.get("/staking")
async def staking(request: Request):
    """Staking status via the wallet-scoped getstakinginfo RPC."""
    state = _state(request)
    state.require_2fa(request)
    try:
        info = await state.rpc.call_optional("getstakinginfo")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    return {"staking": info}


@router.get("/peers")
async def peers(request: Request):
    """Connected peers (read-only, session + 2FA)."""
    state = _state(request)
    state.require_2fa(request)
    try:
        plist = await state.rpc.call("getpeerinfo")
    except (RPCError, RPCNotAllowed, RPCUnavailable) as exc:
        raise _translate(exc)
    out = []
    for p in plist or []:
        out.append({
            "addr": p.get("addr"), "subver": p.get("subver", ""),
            "ping": p.get("pingtime"), "bytesrecv": p.get("bytesrecv", 0),
            "bytessent": p.get("bytessent", 0), "inbound": bool(p.get("inbound")),
        })
    return {"peers": out}
