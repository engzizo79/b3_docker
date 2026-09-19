"""Staking automation tests: settings, vault, reconcile, two-phase
unstake. All against the mocked RPC layer - no funded wallet needed."""
import asyncio

from fastapi.testclient import TestClient

from app import db
from app.autostake import reconcile
from app.vault import vault_from_settings
from tests.conftest import LOCAL, MockRPC, login, unlock

STAKE_TXID = "ab" * 32


def test_settings_require_auth(client: TestClient):
    r = client.get("/api/staking/settings", headers=LOCAL)
    assert r.status_code == 401


def test_settings_defaults(client: TestClient):
    out = login(client)
    r = client.get("/api/staking/settings", headers=out["headers"])
    assert r.status_code == 200
    d = r.json()
    assert d["autostake_enabled"] is False
    assert d["vault_stored"] is False
    assert d["vault_fingerprint"] is None


def test_update_target_needs_only_session(client: TestClient):
    out = login(client)
    r = client.post("/api/staking/settings",
                    json={"autostake_target": "1000", "autostake_reserve": "50"},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["autostake_target"] == "1000.000000000"
    assert d["autostake_reserve"] == "50.000000000"
    assert d["autostake_enabled"] is False


def test_enable_without_passphrase_rejected(client: TestClient):
    out = login(client)
    r = client.post("/api/staking/settings",
                    json={"autostake_enabled": True, "acknowledge_risk": True},
                    headers=out["headers"])
    assert r.status_code == 400


def test_enable_without_ack_rejected(client: TestClient):
    out = login(client)
    unlock(client, out["headers"])
    r = client.post("/api/staking/settings",
                    json={"autostake_enabled": True,
                          "passphrase": "test-passphrase"},
                    headers=out["headers"])
    assert r.status_code == 400


def test_vault_store_requires_unlocked_wallet(client: TestClient):
    out = login(client)  # locked wallet
    r = client.post("/api/staking/settings",
                    json={"passphrase": "test-passphrase"},
                    headers=out["headers"])
    assert r.status_code == 423


def test_vault_store_rejects_wrong_passphrase(client, mock_rpc, settings):
    out = login(client)
    unlock(client, out["headers"])
    mock_rpc.fail_methods.add("walletpassphrase")
    r = client.post("/api/staking/settings",
                    json={"passphrase": "typo-passphrase"},
                    headers=out["headers"])
    assert r.status_code == 400
    cfg = db.get_staking_settings(settings.db_path)
    assert cfg["passphrase_enc"] is None


def test_vault_store_and_enable_flow(client, mock_rpc, settings):
    out = login(client)
    unlock(client, out["headers"])
    r = client.post("/api/staking/settings",
                    json={"autostake_enabled": True, "passphrase": "test-passphrase",
                          "acknowledge_risk": True, "autostake_target": "2000",
                          "autostake_reserve": "100"},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["autostake_enabled"] is True
    assert d["vault_stored"] is True
    assert d["vault_fingerprint"]
    # The stored blob decrypts back with the data-dir key file.
    vault = vault_from_settings(settings, settings.b3_data_dir)
    cfg = db.get_staking_settings(settings.db_path)
    assert vault.decrypt(cfg["passphrase_enc"]) == "test-passphrase"


def test_vault_revoke_wipes(client, mock_rpc, settings):
    out = login(client)
    unlock(client, out["headers"])
    client.post("/api/staking/settings",
                json={"autostake_enabled": True, "passphrase": "test-passphrase",
                      "acknowledge_risk": True}, headers=out["headers"])
    r = client.post("/api/staking/vault/revoke", headers=out["headers"])
    assert r.status_code == 200
    cfg = db.get_staking_settings(settings.db_path)
    assert cfg["passphrase_enc"] is None
    assert cfg["autostake_enabled"] is False


# ---------------------------------------------------------------------------
# Reconcile (unit-level, direct calls against the mock)
# ---------------------------------------------------------------------------

def _enable(settings, target="2000", reserve="100", passphrase="test-passphrase"):
    vault = vault_from_settings(settings, settings.b3_data_dir)
    db.set_staking_settings(
        settings.db_path, autostake_enabled=1,
        autostake_target=target, autostake_reserve=reserve,
        passphrase_enc=vault.encrypt(passphrase))
    return vault


def test_reconcile_disabled_is_noop(client, mock_rpc, settings):
    vault = _enable(settings)
    db.set_staking_settings(settings.db_path, autostake_enabled=0)
    result = asyncio.run(reconcile(settings, mock_rpc, vault))
    assert result["ran"] is False
    assert not mock_rpc.calls  # node never touched


def test_reconcile_tops_up_and_relocks(client, mock_rpc, settings):
    vault = _enable(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["getbalances"] = {"mine": {"trusted": 5000}}
    result = asyncio.run(reconcile(settings, mock_rpc, vault))
    assert result["ran"] is True
    assert result["started"] is True
    assert result["topped_up"] == "1000.000000000"  # 2000 target - 1000 staked
    assert mock_rpc.called("createstake")
    assert mock_rpc.called("walletlock")
    # Relock is ALWAYS the last wallet-state RPC in the window.
    wallet_calls = [m for m, _ in mock_rpc.calls
                    if m in ("walletpassphrase", "walletlock")]
    assert wallet_calls[-1] == "walletlock"


def test_reconcile_respects_reserve(client, mock_rpc, settings):
    vault = _enable(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["getbalances"] = {"mine": {"trusted": 500}}
    result = asyncio.run(reconcile(settings, mock_rpc, vault))
    assert result["topped_up"] is None
    assert result["skipped"]
    assert not mock_rpc.called("createstake")
    assert mock_rpc.called("walletlock")


def test_reconcile_decrypt_failure_disables(client, mock_rpc, settings):
    db.set_staking_settings(settings.db_path, autostake_enabled=1,
                            passphrase_enc="garbage-blob")
    vault = vault_from_settings(settings, settings.b3_data_dir)
    result = asyncio.run(reconcile(settings, mock_rpc, vault))
    assert result["reason"] == "vault decrypt failed"
    cfg = db.get_staking_settings(settings.db_path)
    assert cfg["autostake_enabled"] is False
    alerts = db.alert_list(settings.db_path)
    assert any("Unattended staking disabled" in a["message"] for a in alerts)


def test_reconcile_manual_endpoint(client, mock_rpc, settings):
    out = login(client)
    vault = _enable(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["getbalances"] = {"mine": {"trusted": 5000}}
    r = client.post("/api/staking/reconcile", headers=out["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["ran"] is True


# ---------------------------------------------------------------------------
# Two-phase unstake
# ---------------------------------------------------------------------------

def test_unstake_requires_unlocked_wallet(client: TestClient):
    out = login(client)
    r = client.post("/api/staking/unstake",
                    json={"txid": STAKE_TXID, "vout": 0, "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 423


def test_unstake_unknown_stake_404(client: TestClient):
    out = login(client)
    unlock(client, out["headers"])
    r = client.post("/api/staking/unstake",
                    json={"txid": "f" * 64, "vout": 0, "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 404


def test_unstake_preview_never_broadcasts(client, mock_rpc, settings):
    out = login(client)
    unlock(client, out["headers"])
    r = client.post("/api/staking/unstake",
                    json={"txid": STAKE_TXID, "vout": 0, "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["preview"] is True
    assert d["mempool_ok"] is True
    assert d["stake"]["amount"] == "1000.000000000"
    assert d["validator_warning"] is True  # ACTIVE stake
    assert d["confirm_token"]
    assert not mock_rpc.called("sendrawtransaction")
    assert mock_rpc.called("testmempoolaccept")
    # sendall must spend EXACTLY the chosen outpoint, not the wallet.
    sendall_call = [c for m, c in mock_rpc.calls if m == "sendall"][0]
    assert sendall_call[4]["inputs"] == [{"txid": STAKE_TXID, "vout": 0}]
    assert sendall_call[4]["add_to_wallet"] is False


def test_unstake_confirm_broadcasts(client, mock_rpc, settings):
    out = login(client)
    unlock(client, out["headers"])
    prev = client.post("/api/staking/unstake",
                       json={"txid": STAKE_TXID, "vout": 0, "confirm": False},
                       headers=out["headers"]).json()
    r = client.post("/api/staking/unstake",
                    json={"txid": STAKE_TXID, "vout": 0, "confirm": True,
                          "confirm_token": prev["confirm_token"]},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    assert mock_rpc.called("sendrawtransaction")
    assert r.json()["txid"] == "deadbeef"


def test_unstake_confirm_token_mismatch(client, mock_rpc, settings):
    out = login(client)
    unlock(client, out["headers"])
    r = client.post("/api/staking/unstake",
                    json={"txid": STAKE_TXID, "vout": 0, "confirm": True,
                          "confirm_token": "deadbeef" * 8},
                    headers=out["headers"])
    assert r.status_code == 400
    assert not mock_rpc.called("sendrawtransaction")


def test_unstake_mempool_reject_blocks_broadcast(client, mock_rpc, settings):
    out = login(client)
    unlock(client, out["headers"])
    mock_rpc.responses["testmempoolaccept"] = [
        {"allowed": False, "reject-reason": "insufficient fee"}]
    prev = client.post("/api/staking/unstake",
                       json={"txid": STAKE_TXID, "vout": 0, "confirm": False},
                       headers=out["headers"]).json()
    assert prev["mempool_ok"] is False
    assert prev["rejection"] == "insufficient fee"
    r = client.post("/api/staking/unstake",
                    json={"txid": STAKE_TXID, "vout": 0, "confirm": True,
                          "confirm_token": prev["confirm_token"]},
                    headers=out["headers"])
    assert r.status_code == 422
    assert not mock_rpc.called("sendrawtransaction")


# --- validator pipeline (createstake / finality key / readiness) -------------

def test_validator_status_reports_missing_steps(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    mock_rpc.responses["getstakinginfo"] = {
        "staking": {"available": True, "running": False, "state": "idle"},
        "stakes": [], "active": "0.000000000", "pending": "0.000000000",
        "unconfirmed": "0.000000000", "min_stake_amount": "100.000000000",
    }
    mock_rpc.responses["getfinalityinfo"] = {
        "binding": {"bound": False, "revoked": False}, "validator_set": {"member": False},
    }
    r = client.get("/api/staking/validator", headers=out["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ready"] is False
    assert d["missing"] == ["stake", "bind", "start"]
    assert d["min_stake"] == "100.000000000"


def test_validator_status_ready(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    mock_rpc.responses["getstakinginfo"] = {
        "staking": {"available": True, "running": True, "state": "staking", "blocks_produced": 2},
        "stakes": [], "active": "1000.000000000", "pending": "0", "unconfirmed": "0",
    }
    mock_rpc.responses["getfinalityinfo"] = {
        "binding": {"bound": True, "revoked": False, "seq": 0},
        "validator_set": {"member": True, "weight": 1000},
    }
    r = client.get("/api/staking/validator", headers=out["headers"])
    assert r.status_code == 200
    d = r.json()
    assert d["ready"] is True and d["missing"] == []
    assert d["running"] is True and d["member"] is True


def test_create_stake_calls_createstake_and_audits(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    unlock(client, out["headers"])
    # Realistic funded wallet: the trusted balance includes coins already
    # locked in stakes, so it must exceed the base mock's 1000 B3 stake.
    mock_rpc.responses["getbalances"] = {"mine": {"trusted": 5000.0}}
    r = client.post("/api/staking/stake", json={"amount": "250.5"},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    assert mock_rpc.called("createstake")
    assert mock_rpc.calls[-1][1] == ("250.500000000",)
    assert r.json()["stake"]["status"] == "UNCONFIRMED"


def test_create_stake_refuses_amount_over_liquid(client: TestClient, mock_rpc: MockRPC):
    """User scenario: 495 of 500 already staked; asking for 499.9 must be
    refused with plain language, not a cryptic daemon error."""
    out = login(client)
    unlock(client, out["headers"])
    mock_rpc.responses["getbalances"] = {"mine": {"trusted": 500.0}}
    mock_rpc.responses["getstakinginfo"] = {
        "staking": {"available": True, "running": True, "state": "staking"},
        "stakes": [{"txid": "cs" + "b" * 62, "vout": 1,
                    "amount": "495.000000000", "status": "ACTIVE"}],
        "active": "495.000000000", "pending": "0.000000000",
        "unconfirmed": "0.000000000"}
    r = client.post("/api/staking/stake", json={"amount": "499.9"},
                    headers=out["headers"])
    assert r.status_code == 400
    assert "free to lock" in r.json()["detail"]
    assert not mock_rpc.called("createstake")


def test_create_stake_allows_amount_under_liquid(client: TestClient, mock_rpc: MockRPC):
    """4 B3 with 5 liquid (500 total, 495 staked) goes through."""
    out = login(client)
    unlock(client, out["headers"])
    mock_rpc.responses["getbalances"] = {"mine": {"trusted": 500.0}}
    mock_rpc.responses["getstakinginfo"] = {
        "staking": {"available": True, "running": True, "state": "staking"},
        "stakes": [{"txid": "cs" + "b" * 62, "vout": 1,
                    "amount": "495.000000000", "status": "ACTIVE"}],
        "active": "495.000000000", "pending": "0.000000000",
        "unconfirmed": "0.000000000"}
    r = client.post("/api/staking/stake", json={"amount": "4"},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    assert mock_rpc.called("createstake")


def test_create_stake_rejects_bad_amount(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    unlock(client, out["headers"])
    r = client.post("/api/staking/stake", json={"amount": "-1"},
                    headers=out["headers"])
    assert r.status_code == 400
    r = client.post("/api/staking/stake", json={"amount": "0.1234567891"},
                    headers=out["headers"])
    assert r.status_code == 400


def test_create_stake_requires_unlocked_wallet(client: TestClient):
    out = login(client)
    r = client.post("/api/staking/stake", json={"amount": "250.5"},
                    headers=out["headers"])
    assert r.status_code == 423


def test_finality_bind_calls_bindfinalitykey(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    unlock(client, out["headers"])
    mock_rpc.responses["bindfinalitykey"] = {
        "txid": "ff" * 32, "action": "bind", "status": "UNCONFIRMED", "seq": 0}
    r = client.post("/api/staking/finality/bind", headers=out["headers"])
    assert r.status_code == 200, r.text
    assert mock_rpc.called("bindfinalitykey")
    assert r.json()["result"]["action"] == "bind"


def test_finality_revoke_needs_ack(client: TestClient, mock_rpc: MockRPC):
    out = login(client)
    unlock(client, out["headers"])
    r = client.post("/api/staking/finality/revoke", json={}, headers=out["headers"])
    assert r.status_code == 400  # risk acknowledgement required
    mock_rpc.responses["revokefinalitykey"] = {
        "txid": "ee" * 32, "action": "revoke", "status": "UNCONFIRMED"}
    r = client.post("/api/staking/finality/revoke", json={"ack": True},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    assert mock_rpc.called("revokefinalitykey")


def test_finality_revoke_requires_unlocked_wallet(client: TestClient):
    out = login(client)
    r = client.post("/api/staking/finality/revoke", json={"ack": True},
                    headers=out["headers"])
    assert r.status_code == 423


def test_reconcile_binds_when_unbound(client, mock_rpc, settings):
    # No FINALITY_KEY binding -> reconcile must bind before it can earn.
    vault = _enable(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["getfinalityinfo"] = {"binding": {"bound": False, "revoked": False}}
    mock_rpc.responses["bindfinalitykey"] = {"txid": "ab" * 32, "action": "bind"}
    result = asyncio.run(reconcile(settings, mock_rpc, vault))
    assert result["ran"] is True
    assert result.get("bound") is True
    assert mock_rpc.called("bindfinalitykey")
    # Relock still the last wallet-state call in the window.
    wallet_calls = [m for m, _ in mock_rpc.calls
                   if m in ("walletpassphrase", "walletlock")]
    assert wallet_calls[-1] == "walletlock"


def test_reconcile_skips_bind_when_already_bound(client, mock_rpc, settings):
    vault = _enable(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["getfinalityinfo"] = {
        "binding": {"bound": True, "revoked": False, "seq": 0}}
    result = asyncio.run(reconcile(settings, mock_rpc, vault))
    assert result["ran"] is True
    assert "bound" not in result  # nothing new to do
    assert not mock_rpc.called("bindfinalitykey")
