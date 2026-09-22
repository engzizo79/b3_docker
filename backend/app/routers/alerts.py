"""Alert endpoints: list, acknowledge, and monitor status."""

from fastapi import APIRouter, Request

from app import db
from app.deps import AppState

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _state(request: Request) -> AppState:
    return request.app.state.app_state


@router.get("")
async def list_alerts(request: Request, unacked: bool = False, node_id: int | None = None):
    """List alerts, optionally filtered to unacked only and/or to one node.
    Each row carries node_id/node_name (db.alert_list's LEFT JOIN) so the
    bell can show which node an alert came from."""
    state = _state(request)
    state.require_session(request)
    alerts = db.alert_list(state.settings.db_path, unacked_only=unacked, node_id=node_id)
    return {"alerts": alerts, "count": len(alerts)}


@router.post("/{alert_id}/ack")
async def ack_alert(request: Request, alert_id: int):
    """Acknowledge a single alert."""
    state = _state(request)
    sess = state.require_csrf(request)
    db.alert_ack(state.settings.db_path, alert_id)
    db.audit(state.settings.db_path, "alert_ack", sess.username,
             detail=f"alert_id={alert_id}")
    return {"ok": True}


@router.post("/ack-all")
async def ack_all_alerts(request: Request):
    """Acknowledge all unacked alerts."""
    state = _state(request)
    sess = state.require_csrf(request)
    db.alert_ack_all(state.settings.db_path)
    db.audit(state.settings.db_path, "alert_ack_all", sess.username)
    return {"ok": True}


@router.get("/status")
async def monitor_status(request: Request):
    """Per-node monitor status (docs/MULTINODE_PLAN.md Phase 3): last
    block, stall count, recovery state, keyed by node id. Node id 0 (or
    any id with no registry row) means the pre-registry/legacy monitor,
    which shouldn't happen once _seed_local_node has run, but is handled
    rather than crashing."""
    state = _state(request)
    state.require_session(request)
    from app.monitor import monitors
    names = {n["id"]: n["name"] for n in db.node_list(state.settings.db_path)}
    out = []
    for node_id, mon in monitors.items():
        out.append({
            "node_id": node_id,
            "node_name": names.get(node_id, mon.node_name),
            "running": True,
            "last_blocks": mon._last_blocks,
            "last_ts": mon._last_ts,
            "stall_count": mon._stall_count,
            "recovery_triggered": mon._recovery_triggered,
            "level": mon.settings.stall_level,
            "interval": mon.settings.monitor_interval,
        })
    return {"monitors": out}
