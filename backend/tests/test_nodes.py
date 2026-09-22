"""Node registry (multi-node fleet console, docs/MULTINODE_PLAN.md Phase 1):
CRUD auth/CSRF, credentials never returned, X-B3-Node header resolution,
and the delete guards."""

from fastapi.testclient import TestClient

from tests.conftest import LOCAL, login


def _fake_rpc_post(monkeypatch, result=None, error=None):
    """Stub the httpx call B3RPCClient.call() makes so node-add connectivity
    checks succeed without a real second daemon."""
    import app.rpc as rpc_mod

    async def fake_post(self, url, **kw):
        class R:
            status_code = 200

            def json(self):
                if error is not None:
                    return {"error": error}
                return {"result": result if result is not None else {"blocks": 1, "chain": "test"}}
        return R()

    monkeypatch.setattr(rpc_mod.httpx.AsyncClient, "post", fake_post)


def test_local_node_auto_seeded(app_state):
    from app import db
    rows = db.node_list(app_state.settings.db_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "local"
    assert rows[0]["is_default"] == 1


def test_list_nodes_requires_session(client: TestClient):
    r = client.get("/api/nodes")
    assert r.status_code == 401


def test_add_node_requires_csrf(client: TestClient):
    out = login(client, headers=LOCAL)
    # POST without the csrf header (only session cookie).
    headers = {k: v for k, v in out["headers"].items() if k != "x-csrf-token"}
    r = client.post("/api/nodes", json={"name": "v2", "port": 38647}, headers=headers)
    assert r.status_code == 403


def test_add_node_and_list_never_returns_credentials(client: TestClient, monkeypatch):
    _fake_rpc_post(monkeypatch)
    out = login(client, headers=LOCAL)
    r = client.post("/api/nodes", json={
        "name": "validator-2", "host": "10.0.0.5", "port": 38647,
        "rpc_user": "u", "rpc_password": "secret-password",
    }, headers=out["headers"])
    assert r.status_code == 200, r.text
    node = r.json()["node"]
    assert node["name"] == "validator-2"
    assert node["has_credentials"] is True
    assert "rpc_user" not in node and "rpc_password" not in node
    assert "rpc_user_enc" not in node and "rpc_password_enc" not in node

    r = client.get("/api/nodes", headers=out["headers"])
    assert r.status_code == 200
    body = r.json()
    assert len(body["nodes"]) == 2
    for n in body["nodes"]:
        assert "rpc_user_enc" not in n and "rpc_password_enc" not in n
        assert "rpc_password" not in n


def test_add_node_refused_when_unreachable(client: TestClient, monkeypatch):
    import app.rpc as rpc_mod

    async def fail_post(self, url, **kw):
        raise rpc_mod.httpx.ConnectError("refused")

    monkeypatch.setattr(rpc_mod.httpx.AsyncClient, "post", fail_post)
    out = login(client, headers=LOCAL)
    r = client.post("/api/nodes", json={
        "name": "dead-node", "host": "10.0.0.9", "port": 38647,
    }, headers=out["headers"])
    assert r.status_code == 422
    r = client.get("/api/nodes", headers=out["headers"])
    assert len(r.json()["nodes"]) == 1  # never saved


def test_add_node_duplicate_name_rejected(client: TestClient, monkeypatch):
    _fake_rpc_post(monkeypatch)
    out = login(client, headers=LOCAL)
    body = {"name": "dup", "host": "10.0.0.5", "port": 38647}
    r1 = client.post("/api/nodes", json=body, headers=out["headers"])
    assert r1.status_code == 200
    r2 = client.post("/api/nodes", json=body, headers=out["headers"])
    assert r2.status_code == 409


def test_no_header_resolves_to_default_node(client: TestClient, mock_rpc):
    """Non-breaking guarantee: an existing (not-yet-migrated) endpoint
    behaves identically with and without the X-B3-Node header — chain.py
    still talks to state.rpc directly, exactly as before Phase 1."""
    out = login(client, headers=LOCAL)
    r_default = client.get("/api/chain/summary", headers=out["headers"])
    assert r_default.status_code == 200

    from app import db
    local = db.node_list(client.app.state.app_state.settings.db_path)[0]
    headers = dict(out["headers"])
    headers["X-B3-Node"] = str(local["id"])
    r_explicit = client.get("/api/chain/summary", headers=headers)
    assert r_explicit.status_code == 200
    assert r_explicit.json() == r_default.json()


def test_rpc_for_unknown_id_is_404(app_state, mock_rpc):
    """Unit-level: AppState.rpc_for() is the resolver every migrated router
    calls (console.py in Phase 1) — a bad id must 404, never silently fall
    back to a client."""
    from fastapi import HTTPException, Request

    scope = {"type": "http", "headers": [(b"x-b3-node", b"999999")]}
    request = Request(scope)
    try:
        app_state.rpc_for(request)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 404


def test_rpc_for_no_header_resolves_local(app_state, mock_rpc):
    from fastapi import Request

    scope = {"type": "http", "headers": []}
    request = Request(scope)
    assert app_state.rpc_for(request) is app_state.rpc


def test_set_default_node(client: TestClient, monkeypatch):
    _fake_rpc_post(monkeypatch)
    out = login(client, headers=LOCAL)
    r = client.post("/api/nodes", json={
        "name": "v2", "host": "10.0.0.5", "port": 38647,
    }, headers=out["headers"])
    node_id = r.json()["node"]["id"]

    r = client.post(f"/api/nodes/{node_id}/default", headers=out["headers"])
    assert r.status_code == 200
    assert r.json()["node"]["is_default"] is True

    nodes = client.get("/api/nodes", headers=out["headers"]).json()["nodes"]
    defaults = [n for n in nodes if n["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == node_id


def test_delete_last_node_refused(client: TestClient):
    out = login(client, headers=LOCAL)
    from app import db
    local = db.node_list(client.app.state.app_state.settings.db_path)[0]
    r = client.delete(f"/api/nodes/{local['id']}", headers=out["headers"])
    assert r.status_code == 400
    assert "last node" in r.json()["detail"]


def test_delete_default_node_refused_without_promotion(client: TestClient, monkeypatch):
    _fake_rpc_post(monkeypatch)
    out = login(client, headers=LOCAL)
    r = client.post("/api/nodes", json={
        "name": "v2", "host": "10.0.0.5", "port": 38647,
    }, headers=out["headers"])
    assert r.status_code == 200

    from app import db
    local = db.node_list(client.app.state.app_state.settings.db_path)
    default_row = next(n for n in local if n["is_default"])
    r = client.delete(f"/api/nodes/{default_row['id']}", headers=out["headers"])
    assert r.status_code == 400
    assert "default node" in r.json()["detail"]


def test_delete_non_default_node_ok(client: TestClient, monkeypatch):
    _fake_rpc_post(monkeypatch)
    out = login(client, headers=LOCAL)
    r = client.post("/api/nodes", json={
        "name": "v2", "host": "10.0.0.5", "port": 38647,
    }, headers=out["headers"])
    node_id = r.json()["node"]["id"]

    r = client.delete(f"/api/nodes/{node_id}", headers=out["headers"])
    assert r.status_code == 200

    nodes = client.get("/api/nodes", headers=out["headers"]).json()["nodes"]
    assert len(nodes) == 1


def test_update_local_node_connection_refused(client: TestClient):
    out = login(client, headers=LOCAL)
    from app import db
    local = db.node_list(client.app.state.app_state.settings.db_path)[0]
    r = client.put(f"/api/nodes/{local['id']}", json={"host": "9.9.9.9"},
                    headers=out["headers"])
    assert r.status_code == 400


def test_console_run_uses_selected_node(client: TestClient, mock_rpc, monkeypatch):
    """The console's method call is routed through rpc_for(), so selecting
    an unknown node id via the header is rejected before reaching the node."""
    out = login(client, headers=LOCAL)
    headers = dict(out["headers"])
    headers["X-B3-Node"] = "42424242"
    r = client.post("/api/console/run", json={"command": "getblockchaininfo"},
                     headers=headers)
    assert r.status_code == 404
