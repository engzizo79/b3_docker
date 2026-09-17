"""System endpoints: versions and daemon log tail.

- GET /api/system/info : UI/backend version (build-injected from git describe)
  + daemon version/subversion from getnetworkinfo (graceful when the node
  is down, deferred, or still in its sync window).
- GET /api/system/logs : bounded tail of the daemon stdout log (the
  entrypoint redirects -printtoconsole to <data>/daemon.log). Read-only;
  auth-gated because log lines can contain addresses and IP hints.
"""

from fastapi import APIRouter, HTTPException, Query, Request

from app.config import settings
from app.deps import AppState
from app.rpc import RPCError, RPCNotAllowed, RPCUnavailable

router = APIRouter(prefix="/api/system", tags=["system"])


def _state(request: Request) -> AppState:
    return request.app.state.app_state


@router.get("/info")
async def system_info(request: Request):
    """UI/backend version + daemon version (node may be down: fields null)."""
    state = _state(request)
    state.require_2fa(request)
    info: dict = {
        "app_version": settings.app_version,
        "daemon": None,
        "node_up": False,
    }
    try:
        net = await state.rpc.call("getnetworkinfo")
    except (RPCError, RPCNotAllowed, RPCUnavailable):
        return info
    info["node_up"] = True
    info["daemon"] = {
        "version": net.get("version"),
        "subversion": net.get("subversion"),
        "protocolversion": net.get("protocolversion"),
    }
    return info


@router.get("/logs")
async def system_logs(
    request: Request,
    lines: int = Query(200, ge=10, le=1000),
):
    """Tail of the daemon log. Bounded; read-only; never serves other files."""
    state = _state(request)
    state.require_2fa(request)
    path = state.settings.daemon_log_file
    try:
        with open(path, "r", errors="replace") as fh:
            # Read only the tail: seek back at most ~256 KB to stay cheap.
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 262144))
            tail = fh.read().splitlines()
    except FileNotFoundError:
        return {"lines": [], "note": "no daemon log yet (node not started?)"}
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"log read failed: {exc}")
    return {"lines": tail[-lines:]}