"""Application activity log (audit_log) viewer + automation status.

Distinct from the daemon log: this is what THIS backend did and why
(automation passes, wallet-affecting actions, failures). Read-only.
"""

import time

from fastapi import APIRouter, Query, Request

from app import autostake, consolidation, db
from app.deps import AppState

router = APIRouter(prefix="/api/logs", tags=["logs"])

# UI filter groups -> audit action prefixes.
GROUPS = {
    "automation": ["autostake", "consolidation", "vault"],
    "staking": ["autostake", "staking", "unstake", "consolidation"],
    "wallet": ["wallet", "send", "unstake", "fn_create", "console"],
    "auth": ["login", "logout", "auth", "totp", "2fa", "setup", "password"],
}


def _state(request: Request) -> AppState:
    return request.app.state.app_state


@router.get("")
async def activity(
    request: Request,
    group: str = Query("all", max_length=20),
    status: str = Query("all", pattern="^(all|failed|ok)$"),
    q: str = Query("", max_length=100),
    hours: float = Query(0, ge=0, le=24 * 365),
    limit: int = Query(200, ge=1, le=500),
    before_id: int = Query(0, ge=0),
):
    state = _state(request)
    state.require_2fa(request)
    since = time.time() - hours * 3600 if hours else 0.0
    rows = db.audit_query(
        state.settings.db_path, limit=limit, prefixes=GROUPS.get(group),
        failed_only=(status == "failed"), q=q.strip(), since=since,
        before_id=before_id)
    if status == "ok":
        rows = [r for r in rows if r["success"]]
    return {"entries": rows, "groups": sorted(GROUPS)}


@router.get("/status")
async def automation_status(request: Request):
    state = _state(request)
    state.require_2fa(request)
    dbp = state.settings.db_path
    stk = db.get_staking_settings(dbp)
    cons = db.get_consolidation_settings(dbp)
    return {
        "autostake": {
            "enabled": bool(stk["autostake_enabled"] and stk["passphrase_enc"]),
            "task_running": bool(autostake._task and not autostake._task.done()),
            **autostake.status,
        },
        "consolidation": {
            "enabled": bool(cons["enabled"]),
            "interval_minutes": cons["interval_minutes"],
            "task_running": bool(consolidation._task and not consolidation._task.done()),
            **consolidation.status,
        },
        "now": time.time(),
    }
