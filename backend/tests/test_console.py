"""Expert console: user-controlled trust model.

- localhost / allowlisted / 2FA remote (operator opt-in): FULL access,
  warn but never prohibit - even key material (redacted in audit).
- operator can restrict remote sessions to read-only (their call).
- trust settings runtime-editable from full-trust clients.
"""

from fastapi.testclient import TestClient

from app.rpc import RPCError
from tests.conftest import LOCAL, REMOTE, MockRPC, login, unlock


def _run(client: TestClient, headers: dict, command: str):
    return client.post("/api/console/run", json={"command": command},
                       headers=headers)


def _settings(client: TestClient, headers: dict, networks: str,
              remote_full_access: bool):
    return client.put("/api/console/settings",
        json={"networks": networks,
              "remote_full_access": remote_full_access}, headers=headers)


# -- catalog ---------------------------------------------------------------


def test_catalog_full_mode_for_local(client: TestClient):
    out = login(client)
    r = client.get("/api/console/catalog", headers=out["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "full"
    assert body["editable"] is True
    # Everything runs now: reads, spends, unlock, key material.
    for m in ("getblockchaininfo", "createstake", "sendtoaddress",
              "walletpassphrase", "dumpprivkey", "stop"):
        assert m in body["runnable"], m
    # Danger catalog carries honest warnings, not blocks.
    assert "danger" in body
    assert "Irreversible" in body["danger"]["sendtoaddress"]


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


# -- full mode: warn, never prohibit ----------------------------------------


def test_spend_command_runs_in_full_mode(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _run(client, out["headers"], "createstake 100")
    assert r.status_code == 200, r.text
    assert mock_rpc.calls[-1] == ("createstake", (100,))


def test_key_material_runs_but_is_redacted_in_audit(
        client: TestClient, mock_rpc: MockRPC):
    """The platform warns (frontend confirm) but never prohibits; the
    audit log never records the exported material itself."""
    from app import db
    out = login(client)
    mock_rpc.responses["dumpprivkey"] = "SUPERSECRETHEX"
    r = _run(client, out["headers"],
             "dumpprivkey SbtSJiDgE7kN4LetizjCLESg6acgubtMj2")
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "SUPERSECRETHEX"
    rows = db.audit_list(client.app.state.app_state.settings.db_path)
    row = next(a for a in rows if a["action"] == "console_run"
               and "dumpprivkey" in (a["detail"] or ""))
    assert "SUPERSECRETHEX" not in row["detail"]
    assert "redacted" in row["detail"]


def test_unknown_method_is_node_error_in_full_mode(
        client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _run(client, out["headers"], "notacommand")
    # The daemon answers (method not found) - honest 502, not a 403 wall.
    assert r.status_code == 502, r.text
    assert "node error" in r.json()["detail"]


def test_stop_allowed_like_qt(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _run(client, out["headers"], "stop")
    assert r.status_code == 200, r.text
    assert mock_rpc.called("stop")


# -- console unlock (2FA-proven, capped, session-mirrored) ------------------


def test_console_unlock_works(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _run(client, out["headers"], 'walletpassphrase "test-passphrase" 120')
    assert r.status_code == 200, r.text
    body = r.json()["result"]
    assert body["ok"] is True
    assert body["unlocked_for_s"] == 120
    assert mock_rpc.calls[-1] == ("walletpassphrase", ("test-passphrase", 120))
    r = client.get("/api/auth/status", headers=out["headers"])
    assert r.json()["wallet_unlocked"] is True


def test_console_unlock_caps_timeout(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _run(client, out["headers"], 'walletpassphrase "test-passphrase" 999999')
    assert r.status_code == 200, r.text
    assert r.json()["result"]["unlocked_for_s"] == 3600
    assert mock_rpc.calls[-1] == ("walletpassphrase", ("test-passphrase", 3600))


def test_console_unlock_wrong_passphrase(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    mock_rpc.fail_methods.add("walletpassphrase")
    r = _run(client, out["headers"], 'walletpassphrase "wrong" 60')
    assert r.status_code == 403, r.text
    assert "passphrase" in r.json()["detail"].lower()


def test_console_walletlock_resets_session(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    unlock(client, out["headers"])
    r = client.get("/api/auth/status", headers=out["headers"])
    assert r.json()["wallet_unlocked"] is True
    r = _run(client, out["headers"], "walletlock")
    assert r.status_code == 200, r.text
    r = client.get("/api/auth/status", headers=out["headers"])
    assert r.json()["wallet_unlocked"] is False


# -- remote policy: the operator decides ------------------------------------


def test_remote_defaults_to_full_when_operator_allows(
        client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=REMOTE)
    r = client.get("/api/console/catalog", headers=out["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "full"  # default policy: 2FA remotes trusted


def test_operator_can_restrict_remotes_to_read_only(
        client: TestClient, mock_rpc: MockRPC):
    local = login(client)  # localhost edits the policy
    r = _settings(client, local["headers"], "127.0.0.1/8", False)
    assert r.status_code == 200, r.text

    remote = login(client, headers=REMOTE)
    r = client.get("/api/console/catalog", headers=remote["headers"])
    assert r.json()["mode"] == "restricted"
    # Reads still work...
    r = _run(client, remote["headers"], "getblockchaininfo")
    assert r.status_code == 200, r.text
    # ...spends do not.
    r = _run(client, remote["headers"], "createstake 100")
    assert r.status_code == 403, r.text
    assert "read-only" in r.json()["detail"]


def test_allowlisted_network_gets_full_access(
        client: TestClient, mock_rpc: MockRPC):
    local = login(client)
    r = _settings(client, local["headers"], "203.0.113.0/24", True)
    assert r.status_code == 200, r.text
    remote = login(client, headers=REMOTE)  # 203.0.113.7
    r = client.get("/api/console/catalog", headers=remote["headers"])
    assert r.json()["mode"] == "full"
    r = _run(client, remote["headers"], "createstake 100")
    assert r.status_code == 200, r.text


def test_settings_not_editable_from_restricted_client(
        client: TestClient, mock_rpc: MockRPC):
    local = login(client)
    _settings(client, local["headers"], "127.0.0.1/8", False)
    remote = login(client, headers=REMOTE)
    r = _settings(client, remote["headers"], "0.0.0.0/0", True)
    assert r.status_code == 403, r.text


def test_settings_reject_wildcard(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _settings(client, out["headers"], "*", True)
    assert r.status_code == 400, r.text


def test_settings_reject_garbage_networks(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    r = _settings(client, out["headers"], "not-a-network, 10.0.0.0/8", True)
    assert r.status_code == 400, r.text
    assert "not-a-network" in r.json()["detail"]


# -- hygiene -----------------------------------------------------------------


def test_console_rate_limited(client: TestClient, monkeypatch):
    out = login(client)
    for _ in range(30):
        r = _run(client, out["headers"], "getblockchaininfo")
        assert r.status_code == 200
    r = _run(client, out["headers"], "getblockchaininfo")
    assert r.status_code == 429


def test_console_requires_csrf(client: TestClient):
    login(client)
    r = client.post("/api/console/run", json={"command": "getblockchaininfo"},
                    headers={"x-forwarded-for": "127.0.0.1"})
    assert r.status_code == 403


def test_spoofed_xff_from_untrusted_peer_is_ignored(
        client: TestClient, mock_rpc: MockRPC, monkeypatch):
    """XFF is honored only from trusted proxies: a spoofed
    X-Forwarded-For: 127.0.0.1 must NOT grant localhost trust.
    With the proxy untrusted, the effective IP is the unparseable
    peer name and the gate fails closed (403) - never full access."""
    monkeypatch.delenv("B3_TRUSTED_PROXIES", raising=False)
    out = login(client, headers=LOCAL)
    r = client.get("/api/console/catalog", headers=out["headers"])
    assert r.status_code == 403, r.text
    assert "trusted networks" in r.json()["detail"]
