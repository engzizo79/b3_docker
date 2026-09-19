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
