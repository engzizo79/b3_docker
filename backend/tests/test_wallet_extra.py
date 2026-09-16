"""Tests for wallet_extra endpoints: receive, label, utxos, tx detail,
sign/verify message, passphrase change, peers."""

from fastapi.testclient import TestClient

from tests.conftest import LOCAL, MockRPC, login, unlock

GOOD_ADDR = "StkzLY1yGJhZ4NsiGKyHqvn2JYYfC8T2Pj"


def test_receive_address_requires_csrf(client: TestClient):
    out = login(client, headers=LOCAL)
    # drop the CSRF header: still session-authenticated but no token
    hdr = {k: v for k, v in out["headers"].items() if k != "x-csrf-token"}
    r = client.post("/api/wallet/receive", json={"label": ""}, headers=hdr)
    assert r.status_code == 403


def test_receive_address_creates_with_label(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    r = client.post("/api/wallet/receive", json={"label": "savings"},
                     headers=out["headers"])
    assert r.status_code == 200
    assert r.json()["address"].startswith("S")
    calls = [c for c in mock_rpc.calls if c[0] == "getnewaddress"]
    assert calls and calls[0][1] == ("savings",)


def test_label_rejects_bad_address(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    r = client.post("/api/wallet/label",
                     json={"address": "bc1qbadaddress", "label": "x"},
                     headers=out["headers"])
    assert r.status_code == 400
    assert not mock_rpc.called("setlabel")


def test_utxos_amounts_exact_9dp(client: TestClient):
    out = login(client, headers=LOCAL)
    r = client.get("/api/wallet/utxos", headers=out["headers"])
    assert r.status_code == 200
    utxos = r.json()["utxos"]
    assert len(utxos) == 2
    for u in utxos:
        assert len(u["amount"].split(".")[1]) == 9
    assert utxos[0]["amount"] == "0.420000000"
    assert utxos[1]["amount"] == "1.500000000"


def test_utxos_requires_session(client: TestClient):
    r = client.get("/api/wallet/utxos", headers=LOCAL)
    assert r.status_code == 401


def test_tx_detail_valid_and_invalid(client: TestClient):
    out = login(client, headers=LOCAL)
    good = "a" * 64
    r = client.get("/api/wallet/tx/" + good, headers=out["headers"])
    assert r.status_code == 200
    assert r.json()["transaction"]["txid"] == good
    r = client.get("/api/wallet/tx/notahexid", headers=out["headers"])
    assert r.status_code == 400


def test_signmessage_requires_unlocked_wallet(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    r = client.post("/api/wallet/signmessage",
                     json={"address": GOOD_ADDR, "message": "hello"},
                     headers=out["headers"])
    assert r.status_code == 423
    assert not any(m == "signmessage" for m, _ in mock_rpc.calls)


def test_signmessage_after_unlock(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/signmessage",
                     json={"address": GOOD_ADDR, "message": "hello"},
                     headers=out["headers"])
    assert r.status_code == 200
    assert r.json()["signature"] == "SIGBASE64=="
    assert mock_rpc.called("signmessage")


def test_verifymessage_returns_validity(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    r = client.post("/api/wallet/verifymessage",
                     json={"address": GOOD_ADDR,
                           "signature": "SIGBASE64==", "message": "hello"},
                     headers=out["headers"])
    assert r.status_code == 200
    assert r.json()["valid"] is True
    assert mock_rpc.called("verifymessage")


def test_passphrase_change_validates(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    r = client.post("/api/wallet/passphrase-change",
                     json={"old_passphrase": "old", "new_passphrase": "short"},
                     headers=out["headers"])
    assert r.status_code == 400
    r = client.post("/api/wallet/passphrase-change",
                     json={"old_passphrase": "same", "new_passphrase": "same"},
                     headers=out["headers"])
    assert r.status_code == 400
    assert not mock_rpc.called("walletpassphrasechange")


def test_passphrase_change_happy_path(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    unlock(client, out["headers"])
    r = client.post("/api/wallet/passphrase-change",
                     json={"old_passphrase": "old passphrase",
                           "new_passphrase": "new passphrase 42"},
                     headers=out["headers"])
    assert r.status_code == 200
    assert mock_rpc.called("walletpassphrasechange")


def test_passphrase_change_wrong_old_is_401(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    mock_rpc.fail_methods.add("walletpassphrasechange")
    r = client.post("/api/wallet/passphrase-change",
                     json={"old_passphrase": "wrong",
                           "new_passphrase": "new passphrase 42"},
                     headers=out["headers"])
    assert r.status_code == 401


def test_peers_endpoint(client: TestClient):
    out = login(client, headers=LOCAL)
    r = client.get("/api/chain/peers", headers=out["headers"])
    assert r.status_code == 200
    peers = r.json()["peers"]
    assert len(peers) == 2
    assert peers[0]["inbound"] is True
    assert peers[0]["subver"] == "/B3Hive:1.1.4/"


def test_address_book_lists_labels(client: TestClient, mock_rpc: MockRPC):
    out = login(client, headers=LOCAL)
    r = client.get("/api/wallet/book", headers=out["headers"])
    assert r.status_code == 200
    addrs = r.json()["addresses"]
    assert len(addrs) == 1
    assert addrs[0]["label"] == "mining"
    assert addrs[0]["amount"] == "1.920000000"
    assert mock_rpc.called("listaddressgroupings")
