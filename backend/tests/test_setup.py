"""Setup wizard endpoint tests: bootstrap command, conf safety rails,
restart-node, wizard completion. No real explorer or daemon needed."""

import json
import secrets as pysecrets
from pathlib import Path

import pytest
TEST_RPC_PASS = "testpass"  # test dummy


@pytest.fixture
def conf_dir(tmp_path):
    """A temp B3 data dir with a generated-style b3coin.conf."""
    d = tmp_path / "data"
    d.mkdir()
    (d / "b3coin.conf").write_text(
        "# generated\n"
        "txindex=1\n"
        "listen=1\n"
        "listenonion=0\n"
        "rpcbind=127.0.0.1\n"
        "rpcallowip=127.0.0.1\n"
        "rpcport=32647\n"
        "rpcuser=test\n"
        f"rpcpassword={TEST_RPC_PASS}\n"
        "disablewallet=0\n"
    )
    return d


@pytest.fixture
def setup_client(client, conf_dir):
    """Logged-in client with setup file paths pointed at the temp dir."""
    s = client.app.state.app_state.settings
    s.b3_data_dir = str(conf_dir)
    s.bootstrap_cmd_file = str(conf_dir / "bootstrap.cmd")
    s.bootstrap_progress_file = str(conf_dir / "bootstrap.progress.json")
    s.wizard_marker_file = str(conf_dir / ".wizard_complete")
    s.bootstrap_manifest_url = "https://explorer.b3hive.io/bootstraps/manifest.json"
    return client


def _login(client):
    r = client.post("/api/auth/login", json={"password": "correct horse battery staple"})
    assert r.status_code == 200
    return r


def _csrf(client):
    return {"x-csrf-token": client.cookies.get("b3_csrf")}


def _bootstrap_json():
    return {
        "height": 820000,
        "sha256": "a" * 64,
        "url": "/bootstraps/bootstrap-820000.tar.zst",
        "size": 915960518,
    }


def test_setup_status_first_run(setup_client):
    _login(setup_client)
    r = setup_client.get("/api/setup/status")
    assert r.status_code == 200
    data = r.json()
    assert data["wizard_done"] is False
    assert data["fresh_chain"] is True  # no chainstate dir
    assert data["bootstrap"]["phase"] == "idle"
    assert "txindex" in data["conf"]["editable"]
    assert "rpcpassword" in data["conf"]["locked"]


def test_setup_status_requires_auth(setup_client):
    r = setup_client.get("/api/setup/status")
    assert r.status_code == 401


def test_bootstraps_list_guarded(setup_client, monkeypatch):
    _login(setup_client)
    # manifest fetch stubbed to return canned entries
    async def fake_get(self, url, **kw):
        class R:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {
                    "bootstraps": [{
                        "height": 820000, "size": 915960518,
                        "sha256": "a" * 64,
        "url": "/bootstraps/bootstrap-820000.tar.zst",
                        "created_utc": "2026-09-16T05:32:50Z",
                    }],
                }
        return R()
    import app.routers.setup as setup_mod
    monkeypatch.setattr(setup_mod.httpx.AsyncClient, "get", fake_get)
    r = setup_client.get("/api/setup/bootstraps")
    assert r.status_code == 200
    data = r.json()
    assert data["available"] is True
    assert data["bootstraps"][0]["height"] == 820000
    assert data["bootstraps"][0]["sha256"] == "a" * 64


def test_bootstrap_start_requires_csrf(setup_client):
    _login(setup_client)
    r = setup_client.post("/api/setup/bootstrap/start", json=_bootstrap_json())
    assert r.status_code == 403


def test_bootstrap_start_writes_cmd(setup_client):
    _login(setup_client)
    r = setup_client.post("/api/setup/bootstrap/start",
                          json=_bootstrap_json(), headers=_csrf(setup_client))
    assert r.status_code == 200
    s = setup_client.app.state.app_state.settings
    cmd = json.loads(Path(s.bootstrap_cmd_file).read_text())
    assert cmd["height"] == 820000
    assert cmd["sha256"] == "a" * 64
    assert cmd["url"].startswith("https://explorer.b3hive.io/bootstraps/")
    assert cmd["size"] == 915960518


def test_bootstrap_start_rejects_existing_chain(setup_client):
    _login(setup_client)
    s = setup_client.app.state.app_state.settings
    cs = Path(s.b3_data_dir) / "chainstate"
    cs.mkdir()
    (cs / "somefile.ldb").write_text("x")
    r = setup_client.post("/api/setup/bootstrap/start",
                          json=_bootstrap_json(), headers=_csrf(setup_client))
    assert r.status_code == 409


def test_bootstrap_start_rejects_bad_sha(setup_client):
    _login(setup_client)
    body = _bootstrap_json()
    body["sha256"] = "zz"
    r = setup_client.post("/api/setup/bootstrap/start",
                          json=body, headers=_csrf(setup_client))
    assert r.status_code == 422


def test_bootstrap_start_rejects_traversal(setup_client):
    _login(setup_client)
    body = _bootstrap_json()
    body["url"] = "/bootstraps/../../etc/passwd"
    r = setup_client.post("/api/setup/bootstrap/start",
                          json=body, headers=_csrf(setup_client))
    assert r.status_code == 422


def test_bootstrap_progress_idle(setup_client):
    _login(setup_client)
    r = setup_client.get("/api/setup/bootstrap/progress")
    assert r.status_code == 200
    assert r.json()["phase"] == "idle"


def test_bootstrap_progress_running_blocks_new_start(setup_client):
    _login(setup_client)
    s = setup_client.app.state.app_state.settings
    Path(s.bootstrap_progress_file).write_text(
        '{"phase":"downloading","height":820000,"bytes":100,"size":1000}')
    r = setup_client.post("/api/setup/bootstrap/start",
                          json=_bootstrap_json(), headers=_csrf(setup_client))
    assert r.status_code == 409


def test_conf_get(setup_client):
    _login(setup_client)
    r = setup_client.get("/api/setup/conf")
    assert r.status_code == 200
    data = r.json()
    assert data["editable"]["txindex"] == "1"
    assert "rpcuser" in data["locked"]
    assert data["unrecognized"] == [] or "listenonion" in data["unrecognized"]


def test_conf_apply_roundtrip(setup_client):
    _login(setup_client)
    r = setup_client.post("/api/setup/conf/apply",
                          json={"conf": {"maxconnections": "64"}},
                          headers=_csrf(setup_client))
    assert r.status_code == 200
    assert "maxconnections" in r.json()["applied"]
    s = setup_client.app.state.app_state.settings
    content = (Path(s.b3_data_dir) / "b3coin.conf").read_text()
    assert "maxconnections=64" in content
    # preserved lines survive verbatim
    assert f"rpcpassword={TEST_RPC_PASS}" in content
    assert "txindex=1" in content


def test_conf_apply_rejects_locked(setup_client):
    _login(setup_client)
    r = setup_client.post("/api/setup/conf/apply",
                          json={"conf": {"rpcpassword": "hacked"}},
                          headers=_csrf(setup_client))
    assert r.status_code == 422
    s = setup_client.app.state.app_state.settings
    assert "hacked" not in (Path(s.b3_data_dir) / "b3coin.conf").read_text()


def test_conf_apply_rejects_unknown(setup_client):
    _login(setup_client)
    r = setup_client.post("/api/setup/conf/apply",
                          json={"conf": {"reindex": "1"}},
                          headers=_csrf(setup_client))
    assert r.status_code == 422


def test_conf_apply_validates_values(setup_client):
    _login(setup_client)
    for k, v in [("txindex", "2"), ("listen", "yes"),
                 ("maxconnections", "5000"), ("bantime", "-1")]:
        r = setup_client.post("/api/setup/conf/apply",
                              json={"conf": {k: v}}, headers=_csrf(setup_client))
        assert r.status_code == 422, (k, v)


def test_conf_apply_requires_csrf(setup_client):
    _login(setup_client)
    r = setup_client.post("/api/setup/conf/apply",
                          json={"conf": {"txindex": "1"}})
    assert r.status_code == 403


def test_restart_node_writes_recovery_cmd(setup_client):
    _login(setup_client)
    r = setup_client.post("/api/setup/restart-node", headers=_csrf(setup_client))
    assert r.status_code == 200
    s = setup_client.app.state.app_state.settings
    assert Path(s.recovery_cmd_file).read_text().strip() == "restart"
    Path(s.recovery_cmd_file).unlink()


def test_complete_writes_marker(setup_client):
    _login(setup_client)
    r = setup_client.post("/api/setup/complete", headers=_csrf(setup_client))
    assert r.status_code == 200
    s = setup_client.app.state.app_state.settings
    assert Path(s.wizard_marker_file).is_file()
    r = setup_client.get("/api/setup/status")
    assert r.json()["wizard_done"] is True


def test_wizard_marker_blocks_conf_apply(setup_client):
    """The wizard can be re-run from Settings; conf editing stays available."""
    _login(setup_client)
    s = setup_client.app.state.app_state.settings
    Path(s.wizard_marker_file).write_text("done")
    r = setup_client.post("/api/setup/conf/apply",
                          json={"conf": {"txindex": "1"}},
                          headers=_csrf(setup_client))
    assert r.status_code == 200


# --- wallet create / migrate -------------------------------------------------


def test_setup_status_includes_wallet_and_persistence(setup_client):
    _login(setup_client)
    r = setup_client.get("/api/setup/status")
    assert r.status_code == 200
    data = r.json()
    assert "data_persistent" in data
    assert data["data_persistent"] is True  # allow_ephemeral_data=True in tests
    assert "wallet" in data
    assert data["wallet"]["reachable"] is True
    assert data["wallet"]["loaded"] == []


def test_wallet_create_requires_csrf(setup_client):
    _login(setup_client)
    r = setup_client.post("/api/setup/wallet/create",
        json={"wallet_name": "mywallet", "passphrase": "longpassphrase"})
    assert r.status_code == 403


def test_wallet_create_calls_createwallet(setup_client):
    _login(setup_client)
    mock = setup_client.app.state.app_state.rpc
    r = setup_client.post("/api/setup/wallet/create",
        json={"wallet_name": "mywallet", "passphrase": "longpassphrase123"},
        headers=_csrf(setup_client))
    assert r.status_code == 200, r.text
    assert r.json()["wallet"] == "mywallet"
    method, params = mock.calls[-1]
    assert method == "createwallet"
    assert params[0] == "mywallet"
    assert params[3] == "longpassphrase123"


def test_wallet_create_rejects_short_passphrase(setup_client):
    _login(setup_client)
    r = setup_client.post("/api/setup/wallet/create",
        json={"wallet_name": "mywallet", "passphrase": "short"},
        headers=_csrf(setup_client))
    assert r.status_code == 422


def test_wallet_create_rejects_bad_name(setup_client):
    _login(setup_client)
    for name in ["../etc", "", "a" * 100, "has space"]:
        r = setup_client.post("/api/setup/wallet/create",
            json={"wallet_name": name, "passphrase": "longpassphrase123"},
            headers=_csrf(setup_client))
        assert r.status_code == 422, (name, r.text)


def test_wallet_load_requires_csrf(setup_client):
    _login(setup_client)
    r = setup_client.post("/api/setup/wallet/load",
        json={"filename": "existing_wallet"})
    assert r.status_code == 403


def test_wallet_load_calls_loadwallet(setup_client):
    _login(setup_client)
    mock = setup_client.app.state.app_state.rpc
    r = setup_client.post("/api/setup/wallet/load",
        json={"filename": "existing_wallet"},
        headers=_csrf(setup_client))
    assert r.status_code == 200, r.text
    assert r.json()["wallet"] == "existing_wallet"
    method, params = mock.calls[-1]
    assert method == "loadwallet"
    assert params[0] == "existing_wallet"


def test_wallet_load_rejects_bad_name(setup_client):
    _login(setup_client)
    for name in ["../etc", "", "has space"]:
        r = setup_client.post("/api/setup/wallet/load",
            json={"filename": name},
            headers=_csrf(setup_client))
        assert r.status_code == 422, (name, r.text)


def test_wallet_unlock_blocked_on_ephemeral_data(setup_client):
    """If /data is not persistent and allow_ephemeral_data is false,
    wallet unlock must be blocked to prevent fund loss."""
    _login(setup_client)
    s = setup_client.app.state.app_state.settings
    s.allow_ephemeral_data = False
    r = setup_client.post("/api/wallet/unlock",
        json={"passphrase": "test-passphrase"},
        headers=_csrf(setup_client))
    # The storage-blocked middleware intercepts ALL /api/* routes with 503
    # before the per-endpoint require_persistent_data guard (403) runs.
    assert r.status_code == 503
    assert r.json().get("storage_blocked") is True
