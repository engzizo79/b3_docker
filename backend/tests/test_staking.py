"""Staking automation tests: settings, vault, reconcile, two-phase
unstake. All against the mocked RPC layer - no funded wallet needed."""
import asyncio
import time

from fastapi.testclient import TestClient

from app import db
from app.autostake import reconcile
from app.rpc import RPCError
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


def test_reconcile_does_not_relock_an_already_unlocked_wallet(client, mock_rpc, settings):
    """Regression: the node has ONE global wallet lock, not a per-caller
    lease. If a user is mid-Send/Unstake (or another pass is running) and
    the wallet is already unlocked, a background reconcile pass must NOT
    call walletlock when it finishes - that cuts the other unlock short
    from under them, and their next signing call fails as if the node
    itself were broken ('confirm token mismatch' / 'node returned an
    error') seconds after they entered their passphrase."""
    vault = _enable(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["getbalances"] = {"mine": {"trusted": 5000}}
    mock_rpc.responses["getwalletinfo"] = {"unlocked_until": time.time() + 55}
    result = asyncio.run(reconcile(settings, mock_rpc, vault))
    assert result["ran"] is True
    assert result["topped_up"] == "1000.000000000"  # still does its job
    assert not mock_rpc.called("walletlock")
    assert not mock_rpc.called("walletpassphrase")  # doesn't shorten their window either


def test_reconcile_still_relocks_when_it_took_the_lock_itself(client, mock_rpc, settings):
    """The normal case (nobody else has it unlocked) is unaffected."""
    vault = _enable(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["getbalances"] = {"mine": {"trusted": 5000}}
    mock_rpc.responses["getwalletinfo"] = {"unlocked_until": 0}
    result = asyncio.run(reconcile(settings, mock_rpc, vault))
    assert result["ran"] is True
    assert mock_rpc.called("walletpassphrase")
    assert mock_rpc.called("walletlock")


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
    assert r.json()["autostake_disabled"] is False  # wasn't on


def test_unstake_confirm_disables_autostake_when_it_was_on(client, mock_rpc, settings):
    """Regression: unstaking manually must not leave autostake free to top
    the stake back up toward the old target on its next background pass,
    silently reverting the user's own unstake."""
    vault = _enable(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["getbalances"] = {"mine": {"trusted": 5000}}
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
    assert r.json()["autostake_disabled"] is True
    assert db.get_staking_settings(settings.db_path)["autostake_enabled"] is False
    actions = [a["action"] for a in db.audit_list(settings.db_path, 50)]
    assert "autostake_disabled_by_manual_action" in actions


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


def test_unstake_default_destination_is_owner_address(client, mock_rpc, settings):
 out = login(client)
 unlock(client, out["headers"])
 r = client.post("/api/staking/unstake",
 json={"txid": STAKE_TXID, "vout": 0, "confirm": False},
 headers=out["headers"])
 assert r.status_code == 200, r.text
 d = r.json()
 # No client destination, and no resolvable funding source (the mock's
 # fabricated txid isn't a real transaction) -> falls back to owner_address
 assert d["destination"] == "SbtSJiDgE7kN4LetizjCLESg6acgubtMj2"
 assert d["destination_source"] == "owner"
 sendall_call = [c for m, c in mock_rpc.calls if m == "sendall"][0]
 assert sendall_call[0] == ["SbtSJiDgE7kN4LetizjCLESg6acgubtMj2"]
 assert mock_rpc.called("validateaddress")


def test_unstake_defaults_to_funding_source_when_resolvable(client, mock_rpc, settings):
 """Where the coins actually came from beats the stake's own dedicated
 owner_address, which the user often does not recognize."""
 out = login(client)
 unlock(client, out["headers"])
 funding_addr = "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"
 prev_txid = "cc" * 32
 orig = mock_rpc.call
 async def call(method, *params):
     if method == "getrawtransaction" and params and params[0] == STAKE_TXID:
         return {"vin": [{"txid": prev_txid, "vout": 0}]}
     if method == "getrawtransaction" and params and params[0] == prev_txid:
         return {"vout": [{"scriptPubKey": {"address": funding_addr}}]}
     return await orig(method, *params)
 mock_rpc.call = call
 r = client.post("/api/staking/unstake",
 json={"txid": STAKE_TXID, "vout": 0, "confirm": False},
 headers=out["headers"])
 assert r.status_code == 200, r.text
 d = r.json()
 assert d["destination"] == funding_addr
 assert d["destination_source"] == "funding"
 sendall_call = [c for m, c in mock_rpc.calls if m == "sendall"][0]
 assert sendall_call[0] == [funding_addr]


def test_unstake_falls_back_to_owner_when_funding_source_ambiguous(client, mock_rpc, settings):
 """Multiple distinct source addresses -> don't guess, use owner_address."""
 out = login(client)
 unlock(client, out["headers"])
 prev_a, prev_b = "cc" * 32, "dd" * 32
 orig = mock_rpc.call
 async def call(method, *params):
     if method == "getrawtransaction" and params and params[0] == STAKE_TXID:
         return {"vin": [{"txid": prev_a, "vout": 0}, {"txid": prev_b, "vout": 0}]}
     if method == "getrawtransaction" and params and params[0] == prev_a:
         return {"vout": [{"scriptPubKey": {"address": "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"}}]}
     if method == "getrawtransaction" and params and params[0] == prev_b:
         return {"vout": [{"scriptPubKey": {"address": "SeLxbTthMTY1iMFbbBU4Du52BMR9BNSfu6"}}]}
     return await orig(method, *params)
 mock_rpc.call = call
 r = client.post("/api/staking/unstake",
 json={"txid": STAKE_TXID, "vout": 0, "confirm": False},
 headers=out["headers"])
 assert r.status_code == 200, r.text
 d = r.json()
 assert d["destination"] == "SbtSJiDgE7kN4LetizjCLESg6acgubtMj2"
 assert d["destination_source"] == "owner"


def test_unstake_falls_back_to_wallet_first_address_before_owner(client, mock_rpc, settings):
 """No resolvable funding source, but the wallet's first-ever address
 (descriptor index 0) IS resolvable -> prefer that over owner_address,
 since it's a stand-in for "my main address" and the user recognizes
 it, unlike the stake's own dedicated owner_address."""
 out = login(client)
 unlock(client, out["headers"])
 first_addr = "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"
 desc = "pkh([aa/44h/0h/0h]xpub000/0/*)#chk0000"
 mock_rpc.responses["listdescriptors"] = {"descriptors": [
     {"desc": "pkh([aa/44h/0h/0h]xpub000/1/*)#chk1111", "active": True, "internal": True},
     {"desc": desc, "active": True, "internal": False},
 ]}
 orig = mock_rpc.call
 async def call(method, *params):
     if method == "deriveaddresses" and params and params[0] == desc:
         return [first_addr]
     return await orig(method, *params)
 mock_rpc.call = call
 r = client.post("/api/staking/unstake",
 json={"txid": STAKE_TXID, "vout": 0, "confirm": False},
 headers=out["headers"])
 assert r.status_code == 200, r.text
 d = r.json()
 assert d["destination"] == first_addr
 assert d["destination_source"] == "wallet"


def test_unstake_client_destination_honored_across_phases(client, mock_rpc, settings):
 out = login(client)
 unlock(client, out["headers"])
 custom = "SxyExampleCustomDestinationAddress111111111"
 prev = client.post("/api/staking/unstake",
 json={"txid": STAKE_TXID, "vout": 0, "confirm": False,
 "destination": custom},
 headers=out["headers"]).json()
 assert prev["destination"] == custom
 assert prev["destination_source"] == "custom"
 r = client.post("/api/staking/unstake",
 json={"txid": STAKE_TXID, "vout": 0, "confirm": True,
 "destination": custom,
 "confirm_token": prev["confirm_token"]},
 headers=out["headers"])
 assert r.status_code == 200, r.text
 # Both sendall builds used the SAME client-chosen destination
 dests = [c[0][0] for m, c in mock_rpc.calls if m == "sendall"]
 assert dests == [custom, custom]
 assert mock_rpc.called("sendrawtransaction")


def test_unstake_destination_swap_rejected(client, mock_rpc, settings):
 out = login(client)
 unlock(client, out["headers"])
 custom = "SxyExampleCustomDestinationAddress111111111"
 prev = client.post("/api/staking/unstake",
 json={"txid": STAKE_TXID, "vout": 0, "confirm": False,
 "destination": custom},
 headers=out["headers"]).json()
 # Swap the destination between phases: the HMAC token binds the
 # destination, so this must fail as a token mismatch, never broadcast.
 r = client.post("/api/staking/unstake",
 json={"txid": STAKE_TXID, "vout": 0, "confirm": True,
 "destination": "SbtSJiDgE7kN4LetizjCLESg6acgubtMj2",
 "confirm_token": prev["confirm_token"]},
 headers=out["headers"])
 assert r.status_code == 400
 assert not mock_rpc.called("sendrawtransaction")


def test_unstake_invalid_destination_400(client, mock_rpc, settings):
 out = login(client)
 unlock(client, out["headers"])
 mock_rpc.responses["validateaddress"] = {"isvalid": False}
 r = client.post("/api/staking/unstake",
 json={"txid": STAKE_TXID, "vout": 0, "confirm": False,
 "destination": "NotARealAddress"},
 headers=out["headers"])
 assert r.status_code == 400
 assert "invalid" in r.json()["detail"].lower()
 assert not mock_rpc.called("sendall")


# ---------------------------------------------------------------------------
# Observability: dry run, per-pass audit trail, activity log endpoint
# ---------------------------------------------------------------------------

def test_reconcile_dry_run_touches_no_wallet_state(client, mock_rpc, settings):
    vault = _enable(settings)
    mock_rpc.responses["listwallets"] = ["wallet"]
    mock_rpc.responses["getbalances"] = {"mine": {"trusted": 5000}}
    result = asyncio.run(reconcile(settings, mock_rpc, vault, reason="manual-dry-run",
                                   dry_run=True))
    assert result["ran"] and result["dry_run"]
    assert result["would_top_up"] == "1000.000000000"
    assert any(s["step"] == "Top-up" for s in result["steps"])
    for m in ("walletpassphrase", "startstaking", "createstake", "bindfinalitykey",
              "walletlock"):
        assert not mock_rpc.called(m), m
    actions = [a["action"] for a in db.audit_list(settings.db_path)]
    assert "autostake_dry_run" in actions


def test_every_reconcile_pass_is_audited_even_when_skipped(client, mock_rpc, settings):
    vault = _enable(settings)
    mock_rpc.responses["listwallets"] = []
    asyncio.run(reconcile(settings, mock_rpc, vault))
    row = next(a for a in db.audit_list(settings.db_path) if a["action"] == "autostake_run")
    assert "no wallet loaded" in row["detail"] and row["success"] == 0


def test_logs_endpoint_filters(client, settings):
    out = login(client)
    dbp = settings.db_path
    db.audit(dbp, "autostake_run", detail="reason=startup started", success=True)
    db.audit(dbp, "consolidation_run", detail="not run: node unreachable", success=False)
    db.audit(dbp, "send_broadcast", detail="txid=abc")
    def get(**p):
        r = client.get("/api/logs", params=p, headers=out["headers"])
        assert r.status_code == 200, r.text
        return [e["action"] for e in r.json()["entries"]]
    assert set(get(group="automation")) >= {"autostake_run", "consolidation_run"}
    assert "send_broadcast" not in get(group="automation")
    assert get(group="automation", status="failed") == ["consolidation_run"]
    assert get(q="txid=abc") == ["send_broadcast"]
    assert get(q="%") == []  # LIKE wildcards are escaped, not matched
    r = client.get("/api/logs/status", headers=out["headers"])
    assert r.status_code == 200 and "autostake" in r.json()


def test_logs_require_auth(client):
    assert client.get("/api/logs").status_code in (401, 403)


def test_unstake_build_failure_shows_node_reason(client, mock_rpc, settings):
    out = login(client)
    unlock(client, out["headers"])
    orig = mock_rpc.call
    async def call(method, *params):
        if method == "sendall":
            raise RPCError(-4, "stake output is in use by the staker")
        return await orig(method, *params)
    mock_rpc.call = call
    r = client.post("/api/staking/unstake",
                    json={"txid": STAKE_TXID, "vout": 0, "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 422
    assert "stake output is in use by the staker" in r.json()["detail"]


def test_unstake_confirm_succeeds_when_fee_reestimate_changes_the_tx(client, mock_rpc, settings):
    """Regression: sendall's fee comes from estimatesmartfee, which can
    return a different fee (and so different signed bytes) between the
    preview call and the confirm call, even for the identical stake and
    destination. The confirm token must survive that - it must NOT be
    bound to the literal tx bytes, or confirm fails near-permanently
    with 'confirm token mismatch' ('run the preview again' in the UI)."""
    out = login(client)
    unlock(client, out["headers"])
    orig = mock_rpc.call
    hexes = iter(["aa" * 40, "bb" * 40])  # a different fee -> different bytes
    async def call(method, *params):
        if method == "sendall":
            return {"complete": True, "hex": next(hexes)}
        return await orig(method, *params)
    mock_rpc.call = call
    prev = client.post("/api/staking/unstake",
                       json={"txid": STAKE_TXID, "vout": 0, "confirm": False},
                       headers=out["headers"]).json()
    r = client.post("/api/staking/unstake",
                    json={"txid": STAKE_TXID, "vout": 0, "confirm": True,
                          "confirm_token": prev["confirm_token"]},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["txid"] == "deadbeef"
