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



# ---------------------------------------------------------------------------
# v0.6.0 deployment mode + daemon management
# ---------------------------------------------------------------------------
from app import daemon_release
from app.config import settings as global_settings


@router.get("/mode")
async def system_mode(request: Request):
    """Current deployment mode + managed-mode capabilities.

    External (UI-only) mode gates process-control features; the frontend
    uses this to hide/disable restart, bootstrap, log viewer, backup
    download, and daemon upgrade with honest explanations.
    """
    state = _state(request)
    state.require_2fa(request)
    s = state.settings
    installed = daemon_release.read_installed_version(s.daemon_version_file)
    managed = s.daemon_mode == "managed"
    return {
        "mode": s.daemon_mode,
        "min_daemon_version": s.min_daemon_version,
        "installed_version": installed,
        "managed": managed,
        # Capability flags are TOP-LEVEL: the frontend Object.assigns this
        # whole body into sysMode and gates UI with sysMode.node_restart etc.
        "node_restart": managed,
        "bootstrap": managed,
        "daemon_logs": managed,
        "daemon_upgrade": managed,
        "backup_download": managed,
        "wallet_operations": True,  # both modes
        "console": True,
        "staking": True,
    }


@router.get("/releases")
async def system_releases(request: Request):
    """Cached release check; falls back to a live check if no cache.

    Returns the release-check dict: latest_stable, current_installed,
    update_available, min_version, releases list, checked_at, error.
    """
    state = _state(request)
    state.require_2fa(request)
    s = state.settings
    # Try cache first
    cache = None
    try:
        import json as _json
        with open(s.release_check_file) as fh:
            cache = _json.load(fh)
    except (FileNotFoundError, OSError, ValueError):
        cache = None
    # Live check if no cache or cache is stale (> 1 hour)
    import time as _time
    now = _time.time()
    stale = True
    if cache and cache.get("checked_at"):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(cache["checked_at"])
            stale = (now - dt.timestamp()) > 3600
        except Exception:
            stale = True
    if cache and not stale:
        return cache
    # Live check (failure-tolerant)
    result = daemon_release.check_latest(
        s.min_daemon_version, s.release_check_file, timeout=15)
    return result


@router.post("/upgrade")
async def system_upgrade(request: Request, body: dict | None = None):
    """Trigger a daemon upgrade (managed mode only).

    Writes an upgrade command file that the entrypoint polls and executes:
    download -> SHA256 verify -> stop -> swap -> start. The entrypoint owns
    process control; the backend never touches the daemon process.
    Returns 202 Accepted with the command id; UI polls /mode for status.
    """
    state = _state(request)
    sess = state.require_csrf(request)
    s = state.settings
    if s.daemon_mode != "managed":
        raise HTTPException(
            status_code=403,
            detail="daemon upgrade is only available in managed mode")
    body = body or {}
    tag = str(body.get("tag") or "")
    if not tag:
        raise HTTPException(status_code=400, detail="tag required")
    # Validate against min version
    version = tag.lstrip("vV")
    if not daemon_release.meets_minimum(version, s.min_daemon_version):
        raise HTTPException(
            status_code=400,
            detail=f"version {tag} is below minimum {s.min_daemon_version}")
    import json as _json
    import secrets as _secrets
    cmd_id = _secrets.token_hex(8)
    cmd = {"id": cmd_id, "tag": tag, "action": "upgrade"}
    cmd_file = str(__import__("pathlib").Path(s.b3_data_dir) / "upgrade.cmd")
    try:
        with open(cmd_file + ".tmp", "w") as fh:
            _json.dump(cmd, fh)
        import os as _os
        _os.replace(cmd_file + ".tmp", cmd_file)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"write failed: {exc}")
    from app import db
    db.audit(s.db_path, "daemon_upgrade_start", sess.username,
             detail=f"tag={tag} cmd_id={cmd_id}")
    return {"accepted": True, "cmd_id": cmd_id, "tag": tag}

