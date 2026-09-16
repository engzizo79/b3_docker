"""Tests for alerts endpoints and monitor integration."""

from fastapi.testclient import TestClient

from tests.conftest import LOCAL, login


def test_alert_list_requires_session(client: TestClient):
    r = client.get("/api/alerts", headers=LOCAL)
    assert r.status_code == 401


def test_alert_list_empty_initially(client: TestClient):
    out = login(client, headers=LOCAL)
    r = client.get("/api/alerts", headers=out["headers"])
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_alert_ack_requires_csrf(client: TestClient):
    out = login(client, headers=LOCAL)
    hdr = {k: v for k, v in out["headers"].items() if k != "x-csrf-token"}
    r = client.post("/api/alerts/1/ack", headers=hdr)
    assert r.status_code == 403


def test_alert_ack_nonexistent_is_ok(client: TestClient):
    out = login(client, headers=LOCAL)
    r = client.post("/api/alerts/999/ack", headers=out["headers"])
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_alert_ack_all(client: TestClient):
    from app import db
    out = login(client, headers=LOCAL)
    state = client.app.state.app_state
    db.alert_add(state.settings.db_path, "stall", "test stall")
    db.alert_add(state.settings.db_path, "lag", "test lag")
    r = client.get("/api/alerts", headers=out["headers"])
    assert r.json()["count"] == 2
    r = client.post("/api/alerts/ack-all", headers=out["headers"])
    assert r.status_code == 200
    r = client.get("/api/alerts?unacked=true", headers=out["headers"])
    assert r.json()["count"] == 0


def test_monitor_status_running(client: TestClient):
    out = login(client, headers=LOCAL)
    r = client.get("/api/alerts/status", headers=out["headers"])
    assert r.status_code == 200
    data = r.json()
    assert data["running"] is True
    assert data["level"] == "alert"
