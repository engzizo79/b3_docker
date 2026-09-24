"""FLEET_MODE gate: the multi-node fleet console and dev-build install are
advanced operator features, off by default. Off means the registry API
refuses, /api/system/mode says so (the SPA hides the views), dev-build
upgrades are refused, and request routing / monitors only ever see the
local node — even if remote rows were registered while the flag was on."""

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app import db
from tests.conftest import LOCAL, login

GOOD_SHA = "a" * 64


def _add_remote(settings, name="validator-2", default=False) -> int:
    return db.node_add(settings.db_path, name=name, kind="remote",
                       host="10.0.0.5", port=38647, is_default=default)


def test_settings_default_is_off(monkeypatch):
    from app.config import Settings
    monkeypatch.delenv("FLEET_MODE", raising=False)
    assert Settings().fleet_mode is False
    for val in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("FLEET_MODE", val)
        assert Settings().fleet_mode is True
    monkeypatch.setenv("FLEET_MODE", "0")
    assert Settings().fleet_mode is False


def test_mode_endpoint_reports_flag(client: TestClient, app_state):
    out = login(client, headers=LOCAL)
    body = client.get("/api/system/mode", headers=out["headers"]).json()
    assert body["fleet"] is True
    assert body["dev_build_install"] is True

    app_state.settings.fleet_mode = False
    body = client.get("/api/system/mode", headers=out["headers"]).json()
    assert body["fleet"] is False
    assert body["dev_build_install"] is False


def test_dev_build_install_off_in_external_mode(client: TestClient, app_state):
    app_state.settings.daemon_mode = "external"
    out = login(client, headers=LOCAL)
    body = client.get("/api/system/mode", headers=out["headers"]).json()
    assert body["fleet"] is True
    assert body["dev_build_install"] is False


def test_registry_endpoints_refused_when_off(client: TestClient, app_state):
    app_state.settings.fleet_mode = False
    out = login(client, headers=LOCAL)
    h = out["headers"]
    for method, path, kw in (
        ("get", "/api/nodes", {}),
        ("get", "/api/nodes/fleet", {}),
        ("post", "/api/nodes", {"json": {"name": "v2", "port": 38647}}),
        ("put", "/api/nodes/1", {"json": {"daemon_build": "flowmesh-1"}}),
        ("delete", "/api/nodes/1", {}),
        ("post", "/api/nodes/1/default", {}),
    ):
        r = getattr(client, method)(path, headers=h, **kw)
        assert r.status_code == 403, (method, path, r.text)
        assert "FLEET_MODE" in r.json()["detail"]


def test_dev_build_upgrade_refused_when_off(client: TestClient, app_state, tmp_path):
    app_state.settings.fleet_mode = False
    out = login(client, headers=LOCAL)
    r = client.post("/api/system/upgrade", json={
        "url": "http://10.0.0.5:8765/b.tar.gz", "sha256": GOOD_SHA, "tag": "flowmesh-1",
    }, headers=out["headers"])
    assert r.status_code == 403
    assert "FLEET_MODE" in r.json()["detail"]
    assert not (tmp_path / "upgrade.cmd").exists()


def test_release_upgrade_unaffected_when_off(client: TestClient, app_state, tmp_path):
    """Normal tagged upgrades are not an advanced feature."""
    app_state.settings.fleet_mode = False
    out = login(client, headers=LOCAL)
    r = client.post("/api/system/upgrade", json={"tag": "v1.1.5"}, headers=out["headers"])
    assert r.status_code in (200, 202), r.text
    assert (tmp_path / "upgrade.cmd").exists()


def test_rpc_for_ignores_remote_default_when_off(app_state):
    """A remote row promoted to default while the flag was on must not keep
    receiving every header-less request after it is turned off."""
    node_id = _add_remote(app_state.settings, default=True)
    app_state.settings.fleet_mode = True
    assert app_state.rpc_for(Request({"type": "http", "headers": []})) is not app_state.rpc

    app_state.settings.fleet_mode = False
    assert app_state.rpc_for(Request({"type": "http", "headers": []})) is app_state.rpc

    scope = {"type": "http", "headers": [(b"x-b3-node", str(node_id).encode())]}
    try:
        app_state.rpc_for(Request(scope))
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_rpc_for_local_header_still_works_when_off(app_state):
    app_state.settings.fleet_mode = False
    local = db.node_list(app_state.settings.db_path)[0]
    scope = {"type": "http", "headers": [(b"x-b3-node", str(local["id"]).encode())]}
    assert app_state.rpc_for(Request(scope)) is app_state.rpc


def test_node_list_active_keeps_only_local_when_off(app_state):
    _add_remote(app_state.settings)
    path = app_state.settings.db_path
    assert len(db.node_list_active(path, True)) == 2
    off = db.node_list_active(path, False)
    assert [n["kind"] for n in off] == ["local"]
    # Rows are kept, not deleted: flipping the flag back restores them.
    assert len(db.node_list(path)) == 2


def test_monitors_only_start_for_local_node_when_off(app_state, monkeypatch):
    from app import monitor, wallet_monitor
    monkeypatch.setattr(monitor.ChainMonitor, "start", lambda self: None)
    monkeypatch.setattr(wallet_monitor.WalletMonitor, "start", lambda self: None)
    monkeypatch.setattr(monitor, "monitors", {})
    monkeypatch.setattr(wallet_monitor, "wallet_monitors", {})
    _add_remote(app_state.settings)
    app_state.settings.fleet_mode = False

    monitor.start_monitors(app_state.settings, app_state)
    wallet_monitor.start_wallet_monitors(app_state.settings, app_state)
    local_id = db.node_list_active(app_state.settings.db_path, False)[0]["id"]
    assert list(monitor.monitors) == [local_id]
    assert list(wallet_monitor.wallet_monitors) == [local_id]
