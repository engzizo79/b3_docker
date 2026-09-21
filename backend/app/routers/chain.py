"""Read-only chain-state endpoints for the dashboard."""

from pathlib import Path

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
    # Honest sync progress: local blocks vs last known explorer tip
    # (verificationprogress alone reports ~1.0 early on this chain).
    from pathlib import Path
    blocks = info.get("blocks", 0)
    headers = info.get("headers", blocks)
    tip = None
    try:
        tip_path = Path(state.settings.explorer_tip_file)
        if tip_path.is_file():
            tip = int(tip_path.read_text().strip())
    except Exception:
        tip = None
    sync = {"blocks": blocks, "headers": headers, "tip": tip}
    if tip and tip > 0:
        sync["percent"] = round(min(100.0, blocks / tip * 100), 1)
        sync["behind"] = max(0, tip - blocks)
    return {
        "blockchain": info,
        "network": net,
        "mempool": mempool,
        "sync": sync,
    }


# Bitcoin-derived nodes answer RPC while warming up (block index load, verify,
# rescan) with this error code; the message says which phase they are in.
RPC_IN_WARMUP = -28


@router.get("/node-state")
async def node_state(request: Request):
    """Why the node is (not) answering, so the UI can tell them apart.

    running        node answers RPC
    warming_up     node is up and says it is still loading (detail = its words)
    not_started    managed daemon was never told to start (first-run deferral)
    starting       managed daemon launched but RPC is not listening yet
    unreachable    external node did not answer at host:port
    binary_missing managed mode but the daemon binary is not installed
    auth_error     node rejected the RPC credentials
    error          node answered with some other error (detail = its message)

    "The backend itself is down" cannot be reported here by definition; the
    browser detects that from the failed request (see api.js)."""
    state = _state(request)
    state.require_2fa(request)
    cfg = state.settings
    try:
        await state.rpc.call("getblockchaininfo")
        return {"state": "running", "detail": ""}
    except RPCError as exc:
        if exc.code == RPC_IN_WARMUP:
            return {"state": "warming_up", "detail": exc.message}
        if "rejected credentials" in exc.message:
            return {"state": "auth_error", "detail": exc.message}
        return {"state": "error", "detail": exc.message}
    except RPCNotAllowed as exc:
        raise _translate(exc)
    except RPCUnavailable:
        pass
    if cfg.daemon_mode == "external":
        return {"state": "unreachable",
                "detail": f"{cfg.ext_rpc_host}:{cfg.ext_rpc_port}"}
    if not (Path(cfg.daemon_dir) / "b3coind").is_file():
        return {"state": "binary_missing", "detail": ""}
    if Path(cfg.daemon_deferred_file).is_file():
        return {"state": "not_started", "detail": ""}
    return {"state": "starting", "detail": ""}


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
