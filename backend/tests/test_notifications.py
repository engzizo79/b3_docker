"""Push notification tests: prefs CRUD, subscription management, the
wallet-event monitor's no-backfill seed pass, and the smart-coalescing
anti-spam window. All against the mocked RPC layer and a monkeypatched
push/webhook transport - no real network calls, no funded wallet needed."""

import base64
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, notifier
from app.wallet_monitor import WalletMonitor
from tests.conftest import LOCAL, login

RECEIVE_ADDR = "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"


@pytest.fixture()
def fake_push(monkeypatch):
    """Records every app.push.send() call instead of hitting the network.
    calls: list of (subscription, payload). result is what send() returns
    for every call (default "ok"); set it to "expired" to exercise cleanup."""
    from app import push

    state = {"calls": [], "result": "ok"}

    def _fake_send(data_dir, subject, subscription, payload):
        state["calls"].append((subscription, payload))
        return state["result"]

    monkeypatch.setattr(push, "send", _fake_send)
    return state


@pytest.fixture()
def fake_webhook(monkeypatch):
    """Records every webhook POST instead of hitting the network."""
    calls = []

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            calls.append((url, json))
            return _FakeResp()

    class _FakeHttpx:
        AsyncClient = _FakeClient

    monkeypatch.setattr(notifier, "httpx", _FakeHttpx)
    return calls


def _tx(txid, category, amount, vout=0, address=RECEIVE_ADDR):
    return {"txid": txid, "vout": vout, "category": category,
            "amount": amount, "address": address, "confirmations": 1}


def _unpause(settings) -> None:
    """WalletMonitor pauses while the setup wizard holds the daemon down
    (same guard as ChainMonitor); the settings fixture models a fresh
    install where that marker is always present, so real checks unlink it."""
    Path(settings.daemon_deferred_file).unlink(missing_ok=True)


def _subscribe(settings, endpoint: str = "https://push.example/auto") -> None:
    """Register a push subscription directly (bypassing the API) so
    notifier dispatch has somewhere to send to."""
    db.push_subscription_upsert(settings.db_path, endpoint, "p256dh-key", "auth-key")


def _force_flush(settings, event_type: str) -> None:
    """Push a pending coalescing window's flush_ts into the past so the
    next flush_due() call closes it, without waiting out a real cooldown."""
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE notification_pending SET flush_ts=0 WHERE event_type=?",
                (event_type,))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Prefs
# ---------------------------------------------------------------------------

def test_prefs_requires_session(client: TestClient):
    r = client.get("/api/notifications/prefs", headers=LOCAL)
    assert r.status_code == 401


def test_prefs_defaults(client: TestClient):
    out = login(client)
    r = client.get("/api/notifications/prefs", headers=out["headers"])
    assert r.status_code == 200
    types = {t["event_type"]: t for t in r.json()["types"]}
    assert set(types) == {"received", "sent", "stake", "stall", "lag", "recovery"}
    assert types["stake"]["cooldown_minutes"] == 15  # the whole point of the feature
    assert types["received"]["cooldown_minutes"] == 0
    for t in types.values():
        assert t["enabled"] is True
        assert t["push"] is True
        assert t["webhook"] is True


def test_set_prefs_requires_csrf(client: TestClient):
    out = login(client)
    hdr = {k: v for k, v in out["headers"].items() if k != "x-csrf-token"}
    r = client.put("/api/notifications/prefs/stake", json={"cooldown_minutes": 60}, headers=hdr)
    assert r.status_code == 403


def test_set_prefs_unknown_type_404(client: TestClient):
    out = login(client)
    r = client.put("/api/notifications/prefs/bogus", json={}, headers=out["headers"])
    assert r.status_code == 404


def test_set_prefs_updates_and_clamps_cooldown(client: TestClient, settings):
    out = login(client)
    r = client.put("/api/notifications/prefs/received",
                   json={"enabled": False, "cooldown_minutes": 999999, "push": False, "webhook": True},
                   headers=out["headers"])
    assert r.status_code == 200
    prefs = notifier.effective_prefs(settings.db_path, "received")
    assert prefs["enabled"] is False
    assert prefs["cooldown_minutes"] == 1440  # clamped to the 24h ceiling
    assert prefs["push"] is False
    assert prefs["webhook"] is True


# ---------------------------------------------------------------------------
# VAPID key + subscriptions
# ---------------------------------------------------------------------------

def test_vapid_public_key_requires_session(client: TestClient):
    r = client.get("/api/notifications/vapid-public-key", headers=LOCAL)
    assert r.status_code == 401


def test_vapid_public_key_is_a_valid_p256_point(client: TestClient):
    out = login(client)
    r = client.get("/api/notifications/vapid-public-key", headers=out["headers"])
    assert r.status_code == 200
    key = r.json()["key"]
    padded = key + "=" * (-len(key) % 4)
    raw = base64.urlsafe_b64decode(padded)
    assert len(raw) == 65
    assert raw[0] == 0x04  # uncompressed EC point


def test_subscribe_requires_csrf(client: TestClient):
    out = login(client)
    hdr = {k: v for k, v in out["headers"].items() if k != "x-csrf-token"}
    r = client.post("/api/notifications/subscribe",
                    json={"endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"}},
                    headers=hdr)
    assert r.status_code == 403


def test_subscribe_list_unsubscribe(client: TestClient):
    out = login(client)
    r = client.post("/api/notifications/subscribe",
                    json={"endpoint": "https://push.example/x",
                          "keys": {"p256dh": "a", "auth": "b"}, "user_agent": "test-ua"},
                    headers=out["headers"])
    assert r.status_code == 200

    r = client.get("/api/notifications/subscriptions", headers=out["headers"])
    subs = r.json()["subscriptions"]
    assert len(subs) == 1
    assert subs[0]["user_agent"] == "test-ua"
    assert "endpoint" not in subs[0] and "p256dh" not in subs[0]  # key material never comes back

    r = client.post("/api/notifications/unsubscribe",
                    json={"endpoint": "https://push.example/x"}, headers=out["headers"])
    assert r.status_code == 200
    r = client.get("/api/notifications/subscriptions", headers=out["headers"])
    assert r.json()["subscriptions"] == []


def test_resubscribe_same_endpoint_upserts(client: TestClient):
    out = login(client)
    body = {"endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"}}
    client.post("/api/notifications/subscribe", json=body, headers=out["headers"])
    client.post("/api/notifications/subscribe", json=body, headers=out["headers"])
    r = client.get("/api/notifications/subscriptions", headers=out["headers"])
    assert len(r.json()["subscriptions"]) == 1


def test_revoke_subscription_by_id(client: TestClient):
    out = login(client)
    client.post("/api/notifications/subscribe",
               json={"endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"}},
               headers=out["headers"])
    sub_id = client.get("/api/notifications/subscriptions", headers=out["headers"]).json()["subscriptions"][0]["id"]
    r = client.delete(f"/api/notifications/subscriptions/{sub_id}", headers=out["headers"])
    assert r.status_code == 200
    assert client.get("/api/notifications/subscriptions", headers=out["headers"]).json()["subscriptions"] == []


def test_test_endpoint_dispatches_without_polluting_alerts(client: TestClient, settings, fake_push):
    out = login(client)
    client.post("/api/notifications/subscribe",
               json={"endpoint": "https://push.example/x", "keys": {"p256dh": "a", "auth": "b"}},
               headers=out["headers"])
    before = db.alert_list(settings.db_path)
    r = client.post("/api/notifications/test", json={"event_type": "stake"}, headers=out["headers"])
    assert r.status_code == 200
    assert len(fake_push["calls"]) == 1
    assert db.alert_list(settings.db_path) == before  # test sends never show up in the alert list


def test_test_endpoint_requires_csrf(client: TestClient):
    out = login(client)
    hdr = {k: v for k, v in out["headers"].items() if k != "x-csrf-token"}
    r = client.post("/api/notifications/test", json={"event_type": "stake"}, headers=hdr)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Wallet monitor: seed pass, dedupe, per-category routing
# ---------------------------------------------------------------------------

def test_seed_pass_records_history_without_notifying(client, mock_rpc, settings, fake_push):
    _unpause(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["listtransactions"] = [
        _tx("aa" * 32, "receive", 5.0),
        _tx("bb" * 32, "stake", 100.0),
    ]
    wm = WalletMonitor(settings, mock_rpc)
    import asyncio
    asyncio.run(wm._check())
    assert db.notification_seeded(settings.db_path) is True
    assert db.alert_list(settings.db_path) == []
    assert fake_push["calls"] == []


def test_new_tx_after_seed_notifies_once(client, mock_rpc, settings, fake_push):
    import asyncio
    _unpause(settings)
    _subscribe(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["listtransactions"] = []
    wm = WalletMonitor(settings, mock_rpc)
    asyncio.run(wm._check())  # seed pass, nothing to seed

    mock_rpc.responses["listtransactions"] = [_tx("cc" * 32, "receive", "12.500000000")]
    asyncio.run(wm._check())

    alerts = db.alert_list(settings.db_path)
    assert len(alerts) == 1
    assert alerts[0]["level"] == "received"
    assert "12.5" in alerts[0]["message"]
    assert len(fake_push["calls"]) == 1

    # Polling again without a new tx must not re-notify.
    asyncio.run(wm._check())
    assert len(db.alert_list(settings.db_path)) == 1
    assert len(fake_push["calls"]) == 1


def test_send_category_maps_to_sent(client, mock_rpc, settings, fake_push):
    import asyncio
    _unpause(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["listtransactions"] = []
    wm = WalletMonitor(settings, mock_rpc)
    asyncio.run(wm._check())

    mock_rpc.responses["listtransactions"] = [_tx("dd" * 32, "send", "-3.000000000")]
    asyncio.run(wm._check())
    alerts = db.alert_list(settings.db_path)
    assert alerts[0]["level"] == "sent"
    assert "3" in alerts[0]["message"]  # sign stripped, not "-3"


def test_immature_to_stake_transition_does_not_double_notify(client, mock_rpc, settings, fake_push):
    """The same reward shows up first as 'immature' then later as 'stake'
    once mature. Both bucket to 'stake' and share a txid:vout dedupe key,
    so the transition must not fire a second notification."""
    import asyncio
    _unpause(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["listtransactions"] = []
    wm = WalletMonitor(settings, mock_rpc)
    asyncio.run(wm._check())

    txid = "ee" * 32
    mock_rpc.responses["listtransactions"] = [_tx(txid, "immature", "50.000000000")]
    asyncio.run(wm._check())
    assert len(db.alert_list(settings.db_path)) == 1

    mock_rpc.responses["listtransactions"] = [_tx(txid, "stake", "50.000000000")]
    asyncio.run(wm._check())
    assert len(db.alert_list(settings.db_path)) == 1  # still just the one


def test_orphan_category_ignored(client, mock_rpc, settings, fake_push):
    import asyncio
    _unpause(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["listtransactions"] = []
    wm = WalletMonitor(settings, mock_rpc)
    asyncio.run(wm._check())

    mock_rpc.responses["listtransactions"] = [_tx("ff" * 32, "orphan", "1.0")]
    asyncio.run(wm._check())
    assert db.alert_list(settings.db_path) == []


def test_no_wallet_loaded_is_a_noop(client, mock_rpc, settings, fake_push):
    import asyncio
    _unpause(settings)
    mock_rpc.responses["listwallets"] = []  # default
    wm = WalletMonitor(settings, mock_rpc)
    asyncio.run(wm._check())
    assert mock_rpc.called("listtransactions") is False


def test_paused_while_daemon_deferred(client, mock_rpc, settings, fake_push):
    """settings fixture models a fresh install: .daemon_deferred exists by
    default. Without _unpause(), the monitor must not touch the node."""
    import asyncio
    mock_rpc.responses["listwallets"] = ["wallet"]
    wm = WalletMonitor(settings, mock_rpc)
    asyncio.run(wm._check())
    assert mock_rpc.calls == []


# ---------------------------------------------------------------------------
# Smart coalescing: notifier.notify()/flush_due() unit-level
# ---------------------------------------------------------------------------

def test_burst_within_cooldown_coalesces_to_one_digest(client, settings, fake_push):
    import asyncio
    _subscribe(settings)
    # 15 stake events land in the same tick - the exact scenario the
    # feature exists for: 1 instant push, everything else folded into one
    # digest, never 15 separate pings.
    for i in range(15):
        asyncio.run(notifier.notify(settings, "stake", f"Staking reward: +1 B3 (#{i})",
                                    amount=Decimal("1")))
    assert len(fake_push["calls"]) == 1  # only the first one dispatched instantly
    assert len(db.alert_list(settings.db_path)) == 15  # every one still lands in the alert list

    _force_flush(settings, "stake")
    asyncio.run(notifier.flush_due(settings))
    assert len(fake_push["calls"]) == 2
    digest_payload = fake_push["calls"][1][1]
    assert "14" in digest_payload["title"] or "14" in digest_payload["body"]
    assert "+14" in digest_payload["body"]
    assert "1" in digest_payload["body"]  # total_amount (14 * 1 B3)

    # The window is closed - flushing again must not send anything else.
    asyncio.run(notifier.flush_due(settings))
    assert len(fake_push["calls"]) == 2


def test_lone_event_in_window_flushes_silently(client, settings, fake_push):
    """If nothing else arrives during the cooldown, the window just closes
    - no pointless "+0 more" digest."""
    import asyncio
    _subscribe(settings)
    asyncio.run(notifier.notify(settings, "stake", "Staking reward: +1 B3", amount=Decimal("1")))
    assert len(fake_push["calls"]) == 1
    _force_flush(settings, "stake")
    asyncio.run(notifier.flush_due(settings))
    assert len(fake_push["calls"]) == 1  # no second, empty digest


def test_disabled_type_suppresses_dispatch_but_still_alerts(client, settings, fake_push):
    import asyncio
    _subscribe(settings)
    db.notification_prefs_set(settings.db_path, "received", enabled=False,
                              cooldown_minutes=0, push=True, webhook=True)
    asyncio.run(notifier.notify(settings, "received", "Received 1 B3"))
    assert fake_push["calls"] == []
    assert len(db.alert_list(settings.db_path)) == 1  # in-app list is unconditional


def test_webhook_only_type_skips_push(client, settings, fake_push, fake_webhook):
    import asyncio
    _subscribe(settings)
    settings.webhook_url = "https://hooks.example/relay"
    db.notification_prefs_set(settings.db_path, "received", enabled=True,
                              cooldown_minutes=0, push=False, webhook=True)
    asyncio.run(notifier.notify(settings, "received", "Received 1 B3"))
    assert fake_push["calls"] == []
    assert len(fake_webhook) == 1
    assert fake_webhook[0][0] == "https://hooks.example/relay"


def test_expired_push_subscription_is_removed(client, settings, fake_push):
    import asyncio
    db.push_subscription_upsert(settings.db_path, "https://push.example/dead", "a", "b")
    fake_push["result"] = "expired"
    asyncio.run(notifier.notify(settings, "received", "Received 1 B3"))
    assert db.push_subscription_list(settings.db_path) == []
