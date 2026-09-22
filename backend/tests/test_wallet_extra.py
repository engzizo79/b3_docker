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


def test_utxos_flags_plain_p2pkh_for_coin_control(client: TestClient, mock_rpc: MockRPC):
    """The Send coin-control picker needs to know which outputs it may
    offer: plain P2PKH only, never a stake/asset/metadata carrier."""
    out = login(client, headers=LOCAL)
    mock_rpc.responses["listunspent"] = mock_rpc.responses["listunspent"] + [
        {"txid": "55" * 32, "vout": 0, "address": "SbtSJiDgE7kN4LetizjCLESg6acgubtMj2",
         "amount": 495.0, "confirmations": 500, "spendable": True,
         "scriptPubKey": "4c5c42334d4300070001a77117d0"}]  # B3MC-style, not P2PKH
    r = client.get("/api/wallet/utxos", headers=out["headers"])
    assert r.status_code == 200
    by_txid = {u["txid"]: u for u in r.json()["utxos"]}
    assert by_txid["11" * 32]["p2pkh"] is True
    assert by_txid["55" * 32]["p2pkh"] is False


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
    # listreceivedbyaddress (primary, works on descriptor wallets) returns
    # the mining address; getstakinginfo stakes merge adds the owner.
    assert mock_rpc.called("listreceivedbyaddress")
    by_addr = {a["address"]: a for a in addrs}
    assert "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv" in by_addr
    assert by_addr["SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"]["label"] == "mining"
    assert by_addr["SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"]["amount"] == "1.920000000"
    # Stake owner address merged from getstakinginfo.
    assert "SbtSJiDgE7kN4LetizjCLESg6acgubtMj2" in by_addr
    assert by_addr["SbtSJiDgE7kN4LetizjCLESg6acgubtMj2"]["stake"] is True
    assert by_addr["SbtSJiDgE7kN4LetizjCLESg6acgubtMj2"]["label"] == "staking"


def test_address_book_balance_is_sum_of_unspent(client: TestClient, mock_rpc: MockRPC):
    """`balance` is what the address holds now (unspent outputs), not the
    lifetime `amount` received; addresses with nothing unspent read 0."""
    out = login(client, headers=LOCAL)
    r = client.get("/api/wallet/book", headers=out["headers"])
    by_addr = {a["address"]: a for a in r.json()["addresses"]}
    assert mock_rpc.called("listunspent")
    assert by_addr["SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"]["balance"] == "1.920000000"
    assert by_addr["SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"]["spendable"] == "1.920000000"
    # Stake owner: not in listunspent, balance comes from the stake (locked).
    assert by_addr["SbtSJiDgE7kN4LetizjCLESg6acgubtMj2"]["balance"] == "1000.000000000"
    assert by_addr["SbtSJiDgE7kN4LetizjCLESg6acgubtMj2"]["spendable"] == "0.000000000"


def test_address_book_balance_excludes_spent_and_unspendable(client: TestClient, mock_rpc: MockRPC):
    """Received > balance once coins are spent; unspendable (watch-only)
    outputs never count toward the balance."""
    out = login(client, headers=LOCAL)
    addr = "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"
    orig_call = mock_rpc.call
    async def call(method, *params):
        if method == "listunspent":
            return [
                {"txid": "u1", "vout": 0, "address": addr, "amount": 0.1,
                 "spendable": True},
                {"txid": "u2", "vout": 0, "address": addr, "amount": 9.0,
                 "spendable": False},
            ]
        return await orig_call(method, *params)
    mock_rpc.call = call
    r = client.get("/api/wallet/book", headers=out["headers"])
    entry = {a["address"]: a for a in r.json()["addresses"]}[addr]
    assert entry["amount"] == "1.920000000"      # lifetime received
    assert entry["balance"] == "0.100000000"     # what it holds now


def test_address_book_locked_is_non_p2pkh_outputs(client: TestClient, mock_rpc: MockRPC):
    """Outputs that are not plain payments (stake carriers, asset envelopes,
    metadata cells) count toward the balance but never toward `spendable`."""
    out = login(client, headers=LOCAL)
    addr = "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv"
    plain = "76a914751f0b64ad7c395e05652b72101102cf0da491e888ac"
    carrier = "6a0c4233533100000000000000"  # OP_RETURN B3S1 ... (not P2PKH)
    orig_call = mock_rpc.call
    async def call(method, *params):
        if method == "listunspent":
            return [
                {"txid": "u1", "vout": 0, "address": addr, "amount": 2.5,
                 "spendable": True, "scriptPubKey": plain},
                {"txid": "u2", "vout": 1, "address": addr, "amount": 500.0,
                 "spendable": True, "scriptPubKey": carrier},
            ]
        return await orig_call(method, *params)
    mock_rpc.call = call
    r = client.get("/api/wallet/book", headers=out["headers"])
    entry = {a["address"]: a for a in r.json()["addresses"]}[addr]
    assert entry["balance"] == "502.500000000"
    assert entry["spendable"] == "2.500000000"
    assert entry["locked"] == "500.000000000"


def test_address_book_balance_null_when_listunspent_fails(client: TestClient, mock_rpc: MockRPC):
    from app.rpc import RPCError
    out = login(client, headers=LOCAL)
    orig_call = mock_rpc.call
    async def call(method, *params):
        if method == "listunspent":
            raise RPCError(-1, "boom")
        return await orig_call(method, *params)
    mock_rpc.call = call
    r = client.get("/api/wallet/book", headers=out["headers"])
    assert r.status_code == 200
    assert all(a["balance"] is None and a["spendable"] is None
               for a in r.json()["addresses"])


def test_address_book_fallback_to_groupings(client: TestClient, mock_rpc: MockRPC):
    """If listreceivedbyaddress raises (e.g. on a legacy wallet), the
    endpoint falls back to listaddressgroupings."""
    from app.rpc import RPCError
    out = login(client, headers=LOCAL)
    # Simulate a legacy wallet where listreceivedbyaddress is not
    # supported: raise an RPCError, which the endpoint catches.
    orig_call = mock_rpc.call
    async def fail_call(method, *params):
        if method == "listreceivedbyaddress":
            raise RPCError(-32601, "Method not found")
        return await orig_call(method, *params)
    mock_rpc.call = fail_call
    r = client.get("/api/wallet/book", headers=out["headers"])
    assert r.status_code == 200
    assert mock_rpc.called("listaddressgroupings")
    addrs = r.json()["addresses"]
    assert any(a["address"] == "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv" for a in addrs)


from pathlib import Path


class TestWalletManagement:
    """Wallet create / load / unload / backup — regular features, not wizard-only."""

    def test_manage_lists_loaded_and_on_disk(self, client, mock_rpc):
        out = login(client, headers=LOCAL)
        st = client.app.state.app_state
        wallets_dir = Path(st.settings.node_datadir) / "wallets"
        wallets_dir.mkdir(parents=True, exist_ok=True)
        (wallets_dir / "my-wallet.dat").write_bytes(b"")
        mock_rpc.responses["listwallets"] = ["my-wallet"]
        mock_rpc.responses["listwalletdir"] = {"wallets": [
            {"name": "my-wallet.dat"},
            {"name": "coldstash"},
        ]}
        r = client.get("/api/wallet/manage", headers=out["headers"])
        assert r.status_code == 200
        assert "my-wallet" in r.json()["loaded"]
        assert "my-wallet.dat" in r.json()["on_disk"]
        # subdirectory wallets (wallets/<name>/wallet.dat) are reported
        assert "coldstash" in r.json()["on_disk"]
        assert r.json()["persistent_data"] is True

    def test_manage_create_persistence_guard(self, client, monkeypatch):
        out = login(client, headers=LOCAL)
        st = client.app.state.app_state
        monkeypatch.setattr(st, "data_persistent", lambda: False)
        r = client.post("/api/wallet/manage/create",
                        json={"wallet_name": "w", "passphrase": "p" * 8},
                        headers=out["headers"])
        # Middleware intercepts with 503 before the per-endpoint guard.
        assert r.status_code == 503
        assert r.json().get("storage_blocked") is True

    def test_manage_create_bad_name(self, client):
        out = login(client, headers=LOCAL)
        r = client.post("/api/wallet/manage/create",
                        json={"wallet_name": "../etc", "passphrase": "p" * 8},
                        headers=out["headers"])
        assert r.status_code == 422

    def test_manage_create_short_passphrase(self, client):
        out = login(client, headers=LOCAL)
        r = client.post("/api/wallet/manage/create",
                        json={"wallet_name": "w", "passphrase": "short"},
                        headers=out["headers"])
        assert r.status_code == 422

    def test_manage_create_success(self, client, mock_rpc):
        out = login(client, headers=LOCAL)
        r = client.post("/api/wallet/manage/create",
                        json={"wallet_name": "mywallet", "passphrase": "p" * 12},
                        headers=out["headers"])
        assert r.status_code == 200
        assert r.json()["wallet"] == "mywallet"
        assert mock_rpc.called("createwallet")

    def test_manage_load_success(self, client, mock_rpc):
        out = login(client, headers=LOCAL)
        r = client.post("/api/wallet/manage/load",
                        json={"filename": "mywallet.dat"},
                        headers=out["headers"])
        assert r.status_code == 200
        assert mock_rpc.called("loadwallet")

    def test_manage_load_by_path_inside_data_dir(self, client, mock_rpc):
        '''A wallet moved from another machine, placed under the data
        dir, can be loaded by path without touching settings.json.'''
        out = login(client, headers=LOCAL)
        st = client.app.state.app_state
        moved = Path(st.settings.node_datadir) / "moved-wallet"
        moved.mkdir(parents=True, exist_ok=True)
        (moved / "wallet.dat").write_bytes(b"")
        r = client.post("/api/wallet/manage/load",
                        json={"filename": str(moved)},
                        headers=out["headers"])
        assert r.status_code == 200
        load_calls = [c for c in mock_rpc.calls if c[0] == "loadwallet"]
        assert load_calls, "loadwallet was not called"

    def test_manage_load_traversal_rejected(self, client, mock_rpc):
        '''Paths escaping the data dir are rejected with 422.'''
        out = login(client, headers=LOCAL)
        for bad in ["../outside", "/etc/passwd"]:
            r = client.post("/api/wallet/manage/load",
                            json={"filename": bad},
                            headers=out["headers"])
            assert r.status_code == 422, f"{bad} should be rejected"

    def test_manage_load_fallback_scan_sees_subdir_wallets(self, client, mock_rpc):
        '''When the node is down (listwalletdir fails), the disk scan
        still discovers subdirectory wallets.'''
        out = login(client, headers=LOCAL)
        st = client.app.state.app_state
        wallets_dir = Path(st.settings.node_datadir) / "wallets"
        sub = wallets_dir / "dirwallet"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "wallet.dat").write_bytes(b"")
        mock_rpc.fail_methods.add("listwalletdir")
        r = client.get("/api/wallet/manage", headers=out["headers"])
        assert r.status_code == 200
        assert "dirwallet" in r.json()["on_disk"]

    def test_manage_unload_last_refused(self, client, mock_rpc):
        out = login(client, headers=LOCAL)
        mock_rpc.responses["listwallets"] = ["only"]
        r = client.post("/api/wallet/manage/unload",
                        json={"filename": "only"}, headers=out["headers"])
        assert r.status_code == 409

    def test_manage_backup_success(self, client, mock_rpc):
        out = login(client, headers=LOCAL)
        r = client.post("/api/wallet/manage/backup", headers=out["headers"])
        assert r.status_code == 200
        assert "backup-" in r.json()["path"]
        assert mock_rpc.called("backupwallet")

    def test_manage_backup_persistence_guard(self, client, monkeypatch):
        out = login(client, headers=LOCAL)
        st = client.app.state.app_state
        monkeypatch.setattr(st, "data_persistent", lambda: False)
        r = client.post("/api/wallet/manage/backup", headers=out["headers"])
        # Middleware intercepts with 503 before the per-endpoint guard.
        assert r.status_code == 503
        assert r.json().get("storage_blocked") is True

    def test_backup_download_success(self, client, mock_rpc):
        out = login(client, headers=LOCAL)
        st = client.app.state.app_state
        backups_dir = Path(st.settings.node_datadir) / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        (backups_dir / "backup-20260101T000000Z.dat").write_bytes(b"WALLET")
        r = client.get("/api/wallet/manage/backup/download?file=backup-20260101T000000Z.dat",
                       headers=out["headers"])
        assert r.status_code == 200
        assert r.content == b"WALLET"

    def test_backup_download_traversal_blocked(self, client, mock_rpc):
        out = login(client, headers=LOCAL)
        r = client.get("/api/wallet/manage/backup/download?file=../../etc/passwd",
                       headers=out["headers"])
        assert r.status_code == 400

    def test_backup_download_not_found(self, client, mock_rpc):
        out = login(client, headers=LOCAL)
        r = client.get("/api/wallet/manage/backup/download?file=nope.dat",
                       headers=out["headers"])
        assert r.status_code == 404

    def test_manage_requires_auth(self, client):
        r = client.get("/api/wallet/manage")
        assert r.status_code == 401

    def test_manage_create_requires_csrf(self, client):
        out = login(client, headers=LOCAL)
        hdr = {k: v for k, v in out["headers"].items() if k != "x-csrf-token"}
        r = client.post("/api/wallet/manage/create",
                        json={"wallet_name": "w", "passphrase": "p" * 8},
                        headers=hdr)
        assert r.status_code == 403


def test_address_book_lists_change_addresses_holding_coins(client: TestClient, mock_rpc: MockRPC):
    """Change addresses are absent from listreceivedbyaddress but hold
    coins; they must still appear with their balance."""
    out = login(client, headers=LOCAL)
    change = "SQ32gwB3rpYaAtWabhAHgD4obMRnRjNFqF"
    orig_call = mock_rpc.call
    async def call(method, *params):
        if method == "listunspent":
            return [{"txid": "c1", "vout": 1, "address": change, "amount": 4.5,
                     "spendable": True,
                     "scriptPubKey": "76a914" + "00" * 20 + "88ac"}]
        return await orig_call(method, *params)
    mock_rpc.call = call
    r = client.get("/api/wallet/book", headers=out["headers"])
    entry = {a["address"]: a for a in r.json()["addresses"]}[change]
    assert entry["balance"] == "4.500000000"
    assert entry["spendable"] == "4.500000000"


def test_address_book_stake_owner_shows_staked_amount_as_locked(client: TestClient, mock_rpc: MockRPC):
    """The stake carrier output is absent from listunspent; the owner
    address must still show the staked amount, all of it locked."""
    out = login(client, headers=LOCAL)
    r = client.get("/api/wallet/book", headers=out["headers"])
    stake = [a for a in r.json()["addresses"] if a.get("stake")]
    assert stake, "mock has no stake owner"
    e = stake[0]
    assert e["balance"] == "1000.000000000"
    assert e["spendable"] == "0.000000000"
    assert e["locked"] == e["balance"]
