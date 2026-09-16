"""Alert endpoints: list, acknowledge, and monitor status."""

from fastapi import APIRouter, Request

from app import db
from app.deps import AppState

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _state(request: Request) -> AppState:
    return request.app.state.app_state


@router.get("")
async def list_alerts(request: Request, unacked: bool = False):
    """List alerts, optionally filtered to unacked only."""
    state = _state(request)
    state.require_session(request)
    alerts = db.alert_list(state.settings.db_path, unacked_only=unacked)
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
    """Current monitor status: last block, stall count, recovery state."""
    state = _state(request)
    state.require_session(request)
    from app.monitor import monitor
    if monitor is None:
        return {"running": False}
    return {
        "running": True,
        "last_blocks": monitor._last_blocks,
        "last_ts": monitor._last_ts,
        "stall_count": monitor._stall_count,
        "recovery_triggered": monitor._recovery_triggered,
        "level": monitor.settings.stall_level,
        "interval": monitor.settings.monitor_interval,
    }
