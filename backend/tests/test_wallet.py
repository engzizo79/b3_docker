"""Wallet tests: unlock/lock gating, send preview/confirm, validation.
All against the mocked RPC layer — no funded wallet needed."""

from fastapi.testclient import TestClient

from tests.conftest import LOCAL, MockRPC, login, unlock

GOOD_ADDR = "StkzLY1yGJhZ4NsiGKyHqvn2JYYfC8T2Pj"  # matches ^S[base58]{26,34}$


def test_wallet_info_requires_auth(client: TestClient):
    r = client.get("/api/wallet/info", headers=LOCAL)
    assert r.status_code == 401


def test_unlock_wrong_passphrase(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    mock_rpc.fail_methods.add("walletpassphrase")
    r = client.post("/api/wallet/unlock",
                    json={"passphrase": "wrong"}, headers=out["headers"])
    # 403, NOT 401: a valid session with a wrong passphrase must never
    # read as "session expired" in the frontend (that logged users out).
    assert r.status_code == 403
    assert r.json()["detail"] == "wrong passphrase"


def test_unlock_then_lock(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    assert mock_rpc.called("walletpassphrase")

    r = client.post("/api/wallet/lock", headers=out["headers"])
    assert r.status_code == 200
    assert mock_rpc.called("walletlock")


def test_send_requires_unlocked_wallet(client: TestClient):
    out = login(client, headers=LOCAL)
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "1"}],
                          "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 423  # locked


def test_send_preview_flow(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "1.5"}],
                          "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["preview"] is True
    assert data["mempool_ok"] is True
    assert data["txid"] == "deadbeef"
    # Preview must NOT broadcast.
    assert not mock_rpc.called("sendrawtransaction")
    # But it must have signed and dry-run validated.
    assert mock_rpc.called("signrawtransactionwithwallet")
    assert mock_rpc.called("testmempoolaccept")


def test_send_preview_reports_fee_and_exact_total(client: TestClient, mock_rpc: MockRPC):
    """The fee the node chose is shown before the user commits; the total is
    computed server-side in Decimal (0.1 + 0.2-style float drift must not
    appear)."""
    mock_rpc.responses["fundrawtransaction"] = {"hex": "fundedhex", "fee": 0.00001234}
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "0.1"},
                                         {"address": GOOD_ADDR, "amount": "0.2"}],
                          "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["fee"] == "0.000012340"
    assert data["total"] == "0.300012340"


def test_send_preview_fee_unknown_is_null_not_zero(client: TestClient, mock_rpc: MockRPC):
    mock_rpc.responses["fundrawtransaction"] = {"hex": "fundedhex"}  # no fee field
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "1"}],
                          "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 200
    assert r.json()["fee"] is None and r.json()["total"] is None


def test_send_confirm_broadcasts(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "2"}],
                          "confirm": True},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    assert mock_rpc.called("sendrawtransaction")
    assert r.json()["txid"] == "deadbeef"


def test_send_derives_txid_via_decoderawtransaction_not_getrawtransaction(
        client: TestClient, mock_rpc: MockRPC):
    """Regression: real Bitcoin Core / b3coind never puts 'txid' in
    signrawtransactionwithwallet's result (only this project's own test
    mock once did, by mistake). The fallback used to call getrawtransaction
    with the raw TX HEX as if it were a 64-char txid, which every real
    node rejects (RPC -8: 'parameter 1 must be of length 64') - so every
    real send failed with a 502, while every test passed. Pin the correct
    RPC and prove the wrong one is never called."""
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "1.5"}],
                          "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["txid"] == "deadbeef"
    assert mock_rpc.called("decoderawtransaction")
    decode_call = [c for m, c in mock_rpc.calls if m == "decoderawtransaction"][0]
    assert decode_call[0] == "signedhex"  # the SIGNED tx hex, not a txid
    assert not mock_rpc.called("getrawtransaction")


def test_send_mempool_reject_blocks_broadcast(client: TestClient,
                                               mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    mock_rpc.responses["testmempoolaccept"] = [{"allowed": False,
                                               "reject-reason": "insufficient fee"}]
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "1"}],
                          "confirm": True},
                    headers=out["headers"])
    assert r.status_code == 422
    assert not mock_rpc.called("sendrawtransaction")


def test_send_coin_control_uses_exact_inputs(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "0.1"}],
                          "confirm": False,
                          "inputs": [{"txid": "1111111111111111111111111111111111111111111111111111111111111111", "vout": 0}]},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["coin_control"] == {"count": 1, "total": "0.42"}
    craw_call = [c for m, c in mock_rpc.calls if m == "createrawtransaction"][0]
    assert craw_call[0] == [{"txid": "1111111111111111111111111111111111111111111111111111111111111111", "vout": 0}]  # exact input, not []
    fund_call = [c for m, c in mock_rpc.calls if m == "fundrawtransaction"][0]
    assert fund_call[1] == {"add_inputs": False}  # no auto top-up from other coins


def test_send_without_coin_control_uses_automatic_selection(client: TestClient, mock_rpc: MockRPC):
    """Regression: the default (no 'inputs') path must be byte-for-byte the
    same call shape as before coin control existed."""
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "0.1"}],
                          "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["coin_control"] is None
    craw_call = [c for m, c in mock_rpc.calls if m == "createrawtransaction"][0]
    assert craw_call[0] == []
    fund_call = [c for m, c in mock_rpc.calls if m == "fundrawtransaction"][0]
    assert fund_call == ("rawhex",)  # no options arg at all — auto coin selection


def test_send_coin_control_rejects_unknown_outpoint(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "0.1"}],
                          "confirm": False,
                          "inputs": [{"txid": "ab" * 32, "vout": 0}]},
                    headers=out["headers"])
    assert r.status_code == 409
    assert not mock_rpc.called("createrawtransaction")


def test_send_coin_control_rejects_unspendable_output(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    mock_rpc.responses["listunspent"] = mock_rpc.responses["listunspent"] + [
        {"txid": "3333333333333333333333333333333333333333333333333333333333333333", "vout": 0, "address": "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv",
         "amount": 9.0, "confirmations": 1, "spendable": False,
         "scriptPubKey": "76a914751f0b64ad7c395e05652b72101102cf0da491e888ac"}]
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "0.1"}],
                          "confirm": False,
                          "inputs": [{"txid": "3333333333333333333333333333333333333333333333333333333333333333", "vout": 0}]},
                    headers=out["headers"])
    assert r.status_code == 422
    assert "not spendable" in r.json()["detail"]


def test_send_coin_control_rejects_carrier_output(client: TestClient, mock_rpc: MockRPC):
    """A stake/asset/metadata output must never be spendable via a plain
    send, even if the client explicitly names it — same rule batch tools
    and unstake already follow."""
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    mock_rpc.responses["listunspent"] = mock_rpc.responses["listunspent"] + [
        {"txid": "4444444444444444444444444444444444444444444444444444444444444444", "vout": 1, "address": "SbtSJiDgE7kN4LetizjCLESg6acgubtMj2",
         "amount": 495.0, "confirmations": 500, "spendable": True,
         "scriptPubKey": "4c5c42334d4300070001a77117d0"}]  # B3MC-style, not P2PKH
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "0.1"}],
                          "confirm": False,
                          "inputs": [{"txid": "4444444444444444444444444444444444444444444444444444444444444444", "vout": 1}]},
                    headers=out["headers"])
    assert r.status_code == 422
    assert "P2PKH" in r.json()["detail"]


def test_send_coin_control_rejects_bad_txid_format(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "0.1"}],
                          "confirm": False,
                          "inputs": [{"txid": "not-hex", "vout": 0}]},
                    headers=out["headers"])
    assert r.status_code == 400


def test_send_coin_control_empty_inputs_list_rejected(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "0.1"}],
                          "confirm": False, "inputs": []},
                    headers=out["headers"])
    assert r.status_code == 400


def test_send_coin_control_too_many_inputs_rejected(client: TestClient, mock_rpc: MockRPC):
    """The cap is HARD_MAX_INPUTS (the real per-tx weight-policy ceiling,
    675) - not an arbitrary UI number. A wallet with thousands of small
    reward outputs needs several transactions to sweep them all; this is
    the most any single one can ever hold."""
    from app.batch_engine import HARD_MAX_INPUTS
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    inputs = [{"txid": format(i, "064x"), "vout": 0} for i in range(HARD_MAX_INPUTS + 1)]
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR, "amount": "0.1"}],
                          "confirm": False, "inputs": inputs},
                    headers=out["headers"])
    assert r.status_code == 400
    assert str(HARD_MAX_INPUTS) in r.json()["detail"]
    assert not mock_rpc.called("listunspent")  # rejected before touching the node


def test_send_coin_control_confirm_broadcasts(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    body = {"recipients": [{"address": GOOD_ADDR, "amount": "0.1"}],
           "inputs": [{"txid": "1111111111111111111111111111111111111111111111111111111111111111", "vout": 0}]}
    r = client.post("/api/wallet/send", json={**body, "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    r = client.post("/api/wallet/send", json={**body, "confirm": True},
                    headers=out["headers"])
    assert r.status_code == 200, r.text
    assert mock_rpc.called("sendrawtransaction")


def test_send_invalid_address_rejected(client: TestClient):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    for bad in ["bc1q bad", "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNV", "", "Sx"]:
        r = client.post("/api/wallet/send",
                        json={"recipients": [{"address": bad, "amount": "1"}],
                              "confirm": False},
                        headers=out["headers"])
        assert r.status_code == 400, f"address {bad!r} should be rejected"


def test_send_invalid_amount_rejected(client: TestClient):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    for bad in ["-1", "0", "0.0000000001", "1e999", "abc"]:
        r = client.post("/api/wallet/send",
                        json={"recipients": [{"address": GOOD_ADDR, "amount": bad}],
                              "confirm": False},
                        headers=out["headers"])
        assert r.status_code == 400, f"amount {bad!r} should be rejected"


def test_send_amount_precision_preserved(client: TestClient,
                                          mock_rpc: MockRPC):
    """9dp amounts must reach the node as exact strings, never float-rounded."""
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/send",
                    json={"recipients": [{"address": GOOD_ADDR,
                                          "amount": "0.123456789"}],
                          "confirm": False},
                    headers=out["headers"])
    assert r.status_code == 200
    call = next(c for c in mock_rpc.calls if c[0] == "createrawtransaction")
    outputs = call[1][1]
    assert outputs[GOOD_ADDR] == "0.123456789"


def test_history_and_addresses(client: TestClient):
    out = login(client, headers=LOCAL)
    r = client.get("/api/wallet/history", headers=out["headers"])
    assert r.status_code == 200
    r = client.get("/api/wallet/labels", headers=out["headers"])
    assert r.status_code == 200
    assert r.json()["labels"] == ["*"]


def test_staking_start_requires_unlocked_wallet(client: TestClient):
    out = login(client, headers=LOCAL)
    r = client.post("/api/wallet/staking/start", headers=out["headers"])
    assert r.status_code == 423  # locked


def test_staking_start_calls_startstaking(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/staking/start", headers=out["headers"])
    assert r.status_code == 200, r.text
    assert mock_rpc.called("startstaking")
    assert r.json()["staking"] is True


def test_staking_stop_calls_stopstaking(client: TestClient, mock_rpc: MockRPC):
    # stopstaking needs NO unlocked wallet: the staker holds its own
    # signing material (staking survives re-lock). Stop while LOCKED.
    out = login(client, headers=LOCAL)
    r = client.post("/api/wallet/staking/stop", headers=out["headers"])
    assert r.status_code == 200, r.text
    assert mock_rpc.called("stopstaking")
    assert r.json()["staking"] is False
    assert r.json()["autostake_disabled"] is False  # wasn't on


def test_staking_stop_disables_autostake_when_it_was_on(client: TestClient, mock_rpc: MockRPC, settings):
    """Regression: stopping staking manually must not leave autostake free
    to call startstaking again on its next background pass, silently
    reverting the user's own action."""
    from app import db
    from app.vault import vault_from_settings
    vault = vault_from_settings(settings, settings.b3_data_dir)
    db.set_staking_settings(settings.db_path, autostake_enabled=1,
                            passphrase_enc=vault.encrypt("test-passphrase"))
    out = login(client, headers=LOCAL)
    r = client.post("/api/wallet/staking/stop", headers=out["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["autostake_disabled"] is True
    assert db.get_staking_settings(settings.db_path)["autostake_enabled"] is False
    actions = [a["action"] for a in db.audit_list(settings.db_path, 50)]
    assert "autostake_disabled_by_manual_action" in actions
    alerts = db.alert_list(settings.db_path)
    assert any("Autostake was turned off" in a["message"] for a in alerts)
