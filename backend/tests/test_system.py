"""System endpoint tests: About (versions) and daemon log tail.

- /api/system/info: app version from settings; daemon version from
  getnetworkinfo; graceful (node_up=false, daemon=null) when the node is
  down or deferred.
- /api/system/logs: bounded tail of the daemon log file; friendly note when
  the log does not exist yet; auth-gated.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import LOCAL, login


def _mock(client: TestClient):
    return client.app.state.app_state.rpc


def test_system_info_requires_auth(client: TestClient):
    r = client.get("/api/system/info")
    assert r.status_code in (401, 403)


def test_system_info_node_up(client: TestClient):
    _mock(client).responses["getnetworkinfo"] = {
        "version": 1010500,
        "subversion": "/B3Hive:1.1.5/",
        "protocolversion": 70016,
    }
    login(client, headers=LOCAL)
    r = client.get("/api/system/info")
    assert r.status_code == 200
    body = r.json()
    assert body["node_up"] is True
    assert body["daemon"]["subversion"] == "/B3Hive:1.1.5/"
    assert body["daemon"]["version"] == 1010500
    assert body["daemon"]["protocolversion"] == 70016
    # app_version always present ("dev" outside a docker build)
    assert isinstance(body["app_version"], str) and body["app_version"]


def test_system_info_node_down(client: TestClient):
    # Simulate a down node: getnetworkinfo fails -> graceful, not 500.
    _mock(client).fail_methods.add("getnetworkinfo")
    login(client, headers=LOCAL)
    r = client.get("/api/system/info")
    assert r.status_code == 200
    body = r.json()
    assert body["node_up"] is False
    assert body["daemon"] is None
    assert body["app_version"]


def test_system_logs_tail_bounded(client: TestClient, tmp_path: Path):
    NL = chr(10)
    log = tmp_path / "daemon.log"
    log.write_text(NL.join("line-%d" % i for i in range(500)))
    client.app.state.app_state.settings.daemon_log_file = str(log)
    login(client, headers=LOCAL)
    r = client.get("/api/system/logs")
    assert r.status_code == 200
    body = r.json()
    assert body["lines"][0] == "line-300"
    assert body["lines"][-1] == "line-499"
    assert len(body["lines"]) == 200
    r = client.get("/api/system/logs?lines=10")
    assert r.json()["lines"][0] == "line-490"
    r = client.get("/api/system/logs?lines=5")
    assert r.status_code == 422


def test_system_logs_missing_file(client: TestClient, tmp_path: Path):
    client.app.state.app_state.settings.daemon_log_file = str(
        tmp_path / "does-not-exist.log")
    login(client, headers=LOCAL)
    r = client.get("/api/system/logs")
    assert r.status_code == 200
    body = r.json()
    assert body["lines"] == []
    assert "no daemon log" in body.get("note", "")


def test_system_logs_requires_auth(client: TestClient):
    r = client.get("/api/system/logs")
    assert r.status_code in (401, 403)
