"""Push notification endpoints: per-type prefs (enable/cooldown/channels),
Web Push subscription management, and a test-send. Reads need a session;
every write needs CSRF and is audit-logged, same as the rest of the API."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import db, notifier, push
from app.deps import AppState

router = APIRouter(prefix="/api/notifications", tags=["notifications"])

MAX_COOLDOWN_MINUTES = 1440  # 24h - a digest waiting longer than a day isn't useful


def _state(request: Request) -> AppState:
    return request.app.state.app_state


class PrefsBody(BaseModel):
    enabled: bool = True
    cooldown_minutes: int = 0
    push: bool = True
    webhook: bool = True


class SubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class SubscribeBody(BaseModel):
    endpoint: str
    keys: SubscriptionKeys
    user_agent: str | None = None


class UnsubscribeBody(BaseModel):
    endpoint: str


class TestBody(BaseModel):
    event_type: str


@router.get("/prefs")
async def get_prefs(request: Request):
    state = _state(request)
    state.require_session(request)
    return {"types": notifier.list_prefs(state.settings.db_path)}


@router.put("/prefs/{event_type}")
async def set_prefs(event_type: str, body: PrefsBody, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    if event_type not in notifier.NOTIFICATION_TYPES:
        raise HTTPException(status_code=404, detail="unknown notification type")
    cooldown = max(0, min(body.cooldown_minutes, MAX_COOLDOWN_MINUTES))
    db.notification_prefs_set(
        state.settings.db_path, event_type,
        enabled=body.enabled, cooldown_minutes=cooldown,
        push=body.push, webhook=body.webhook)
    db.audit(state.settings.db_path, "notification_prefs_set", sess.username,
             detail=f"type={event_type} enabled={body.enabled} cooldown={cooldown}"
                    f" push={body.push} webhook={body.webhook}")
    return {"ok": True}


@router.get("/vapid-public-key")
async def vapid_public_key(request: Request):
    state = _state(request)
    state.require_session(request)
    return {"key": push.public_key_b64url(state.settings.b3_data_dir)}


@router.get("/subscriptions")
async def list_subscriptions(request: Request):
    state = _state(request)
    state.require_session(request)
    subs = db.push_subscription_list(state.settings.db_path)
    # Never return the raw key material back to the browser - it isn't
    # needed to render a device list and there's no reason to re-expose it.
    return {"subscriptions": [
        {"id": s["id"], "user_agent": s["user_agent"],
         "created_ts": s["created_ts"], "last_seen_ts": s["last_seen_ts"]}
        for s in subs]}


@router.post("/subscribe")
async def subscribe(body: SubscribeBody, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    db.push_subscription_upsert(
        state.settings.db_path, body.endpoint, body.keys.p256dh, body.keys.auth,
        body.user_agent)
    db.audit(state.settings.db_path, "push_subscribe", sess.username)
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(body: UnsubscribeBody, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    db.push_subscription_delete(state.settings.db_path, endpoint=body.endpoint)
    db.audit(state.settings.db_path, "push_unsubscribe", sess.username)
    return {"ok": True}


@router.delete("/subscriptions/{sub_id}")
async def revoke_subscription(sub_id: int, request: Request):
    state = _state(request)
    sess = state.require_csrf(request)
    db.push_subscription_delete(state.settings.db_path, sub_id=sub_id)
    db.audit(state.settings.db_path, "push_unsubscribe", sess.username, detail=f"id={sub_id}")
    return {"ok": True}


@router.post("/test")
async def send_test(body: TestBody, request: Request):
    """Bypass prefs/coalescing entirely and dispatch once, so the user can
    verify a channel works. Does not write to the alerts list."""
    state = _state(request)
    sess = state.require_csrf(request)
    if body.event_type not in notifier.NOTIFICATION_TYPES:
        raise HTTPException(status_code=404, detail="unknown notification type")
    await notifier.send_test(state.settings, body.event_type)
    db.audit(state.settings.db_path, "notification_test", sess.username,
             detail=f"type={body.event_type}")
    return {"ok": True}
