"""Expert console: tiering, IP gate, rate limit, audit."""

from fastapi.testclient import TestClient

from tests.conftest import LOCAL, REMOTE, MockRPC, login, unlock


def _run(client: TestClient, headers: dict, command: str):
    return client.post("/api/console/run", json={"command": command},
                       headers=headers)


def test_catalog_lists_tiers(client: TestClient):
    out = login(client)
    r = client.get("/api/console/catalog", headers=out["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert "getblockchaininfo" in body["read"]
    assert "signmessage" in body["unlock"]
    assert "dumpprivkey" not in body["read"] + body["unlock"]
    assert "walletpassphrase" in body["blocked"]
    assert body["blocked"]["walletpassphrase"]


def test_read_command_works_while_locked(client: TestClient, mock_rpc: MockRPC):
    out = login(client)  # wallet stays locked
    r = _run(client, out["headers"], "getblockchaininfo")
    assert r.status_code == 200, r.text
    assert r.json()["result"]["chain"] == "main"
    assert mock_rpc.called("getblockchaininfo")


def test_read_command_with_json_args(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _run(client, out["headers"], "getblockhash 822000")
    assert r.status_code == 200, r.text
    assert mock_rpc.calls[-1] == ("getblockhash", (822000,))


def test_bare_word_args_become_strings(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _run(client, out["headers"], "help Chain")
    assert r.status_code == 200, r.text
    assert mock_rpc.calls[-1] == ("help", ("Chain",))


def test_unknown_method_rejected(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _run(client, out["headers"], "notacommand")
    assert r.status_code == 403, r.text
    assert "not on the console allowlist" in r.json()["detail"]


def test_blocked_method_returns_guidance(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _run(client, out["headers"], "createstake 100")
    assert r.status_code == 403, r.text
    assert "Staking" in r.json()["detail"]
    assert not mock_rpc.called("createstake")


def test_walletpassphrase_blocked_with_guidance(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _run(client, out["headers"], 'walletpassphrase "hunter2" 60')
    assert r.status_code == 403, r.text
    assert "Unlock from the action" in r.json()["detail"]
    assert not mock_rpc.called("walletpassphrase")


def test_unlock_tier_returns_423_while_locked(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _run(client, out["headers"], "signmessage")
    assert r.status_code == 423, r.text
    assert not mock_rpc.called("signmessage")


def test_unlock_tier_works_after_unlock(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    unlock(client, out["headers"])
    r = _run(client, out["headers"], "signmessage")
    assert r.status_code == 200, r.text
    assert mock_rpc.called("signmessage")


def test_console_forbidden_from_remote_ip(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=REMOTE)
    r = _run(client, out["headers"], "getblockchaininfo")
    assert r.status_code == 403, r.text
    assert "trusted networks" in r.json()["detail"]
    assert not mock_rpc.called("getblockchaininfo")
    r = client.get("/api/console/catalog", headers=out["headers"])
    assert r.status_code == 403, r.text


def test_console_gate_optout_wildcard(client: TestClient, mock_rpc: MockRPC, monkeypatch):
    out = login(client, headers=REMOTE)
    monkeypatch.setattr(
        client.app.state.app_state.settings, "console_networks", "*")
    r = _run(client, out["headers"], "getblockchaininfo")
    assert r.status_code == 200, r.text


def test_console_gate_custom_cidr(client: TestClient, mock_rpc: MockRPC, monkeypatch):
    out = login(client, headers=REMOTE)
    monkeypatch.setattr(
        client.app.state.app_state.settings, "console_networks", "203.0.113.0/24")
    r = _run(client, out["headers"], "getblockchaininfo")
    assert r.status_code == 200, r.text


def test_console_rate_limited(client: TestClient, monkeypatch):
    out = login(client)
    for _ in range(30):
        r = _run(client, out["headers"], "getblockchaininfo")
        assert r.status_code == 200
    r = _run(client, out["headers"], "getblockchaininfo")
    assert r.status_code == 429, r.text


def test_console_requires_csrf(client: TestClient):
    login(client)
    r = client.post("/api/console/run",
                    json={"command": "getblockchaininfo"}, headers=LOCAL)
    assert r.status_code == 403
