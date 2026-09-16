"""Test fixtures: mocked RPC layer, temp DB, app factory injection.
No test requires a real node or a funded wallet."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import Settings
from app.deps import AppState
from app.main import create_app
from app.rpc import RPCError, RPCNotAllowed, assert_allowed
from app.session import SessionStore


class MockRPC:
    """Records every call; returns canned results."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_methods: set[str] = set()
        self.responses: dict[str, object] = {
            "getblockchaininfo": {"blocks": 822000, "chain": "main"},
            "getnetworkinfo": {"version": 114000, "connections": 8},
            "getmempoolinfo": {"size": 3, "bytes": 1000},
            "getfinalitystatus": {"epoch": 100},
            "getbridgeinfo": {"active": True},
            "gettxoutsetinfo": {"total_amount": 1000000},
            "getstakinginfo": {"staking": True},
 "getnewaddress": "SbtSJiDgE7kN4LetizjCLESg6acgubtMj2",
 "setlabel": None,
 "gettransaction": {"txid": "a" * 64, "amount": 1.5, "confirmations": 10, "fee": 0.000034, "time": 1700000000, "category": "receive", "details": []},
 "signmessage": "SIGBASE64==",
 "verifymessage": True,
 "walletpassphrasechange": None,
 "getpeerinfo": [
  {"addr": "1.2.3.4:38647", "subver": "/B3Hive:1.1.4/", "pingtime": 0.05, "bytesrecv": 1000, "bytessent": 2000, "inbound": True},
  {"addr": "5.6.7.8:38647", "subver": "/B3Hive:1.1.4/", "pingtime": 0.08, "bytesrecv": 3000, "bytessent": 500, "inbound": False},
 ],
			"startstaking": None,
			"stopstaking": None,
            "getwalletinfo": {"unlocked_until": 0},
            "getbalances": {"mine": {"trusted": 1.0}},
            "getaddressesbylabel": {},
            "listlabels": ["*"],
 "listaddressgroupings": [[{"address": "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv", "label": "mining", "amount": 1.92}]],
            "listtransactions": [],
 "listunspent": [
 {"txid": "utxo1", "vout": 0, "address": "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv",
 "amount": 0.42, "confirmations": 120, "spendable": True,
 "scriptPubKey": "76a914751f0b64ad7c395e05652b72101102cf0da491e888ac"},
 {"txid": "utxo2", "vout": 1, "address": "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv",
 "amount": 1.5, "confirmations": 90, "spendable": True,
 "scriptPubKey": "76a914751f0b64ad7c395e05652b72101102cf0da491e888ac"},
 ],
 "estimatesmartfee": {"feerate": 0.0001, "blocks": 6},
            "walletpassphrase": None,
            "walletlock": None,
            "createrawtransaction": "rawhex",
            "fundrawtransaction": {"hex": "fundedhex", "fee": 0.00001},
            "signrawtransactionwithwallet": {"hex": "signedhex",
                                             "complete": True,
                                             "txid": "deadbeef"},
            "testmempoolaccept": [{"allowed": True}],
            "sendrawtransaction": "deadbeef",
        }

    async def call(self, method: str, *params):
        self.calls.append((method, params))
        if method in self.fail_methods:
            raise RPCError(-14, "wallet passphrase entered was incorrect")
        if method not in self.responses:
            raise AssertionError(f"unexpected RPC: {method}")
        return self.responses[method]

    async def call_optional(self, method: str, *params):
        try:
            assert_allowed(method)
        except RPCNotAllowed:
            return None
        return await self.call(method, *params)

    def called(self, method: str) -> bool:
        return any(m == method for m, _ in self.calls)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    s = Settings.__new__(Settings)
    s.rpc_host = "127.0.0.1"
    s.rpc_port = 32647
    s.rpc_user = "u"
    s.rpc_password = "p"
    s.db_path = str(tmp_path / "test.db")
    s.ui_password = "correct horse battery staple"
    s.session_secret = "test-session-secret"
    s.totp_key = "test-totp-key"
    s.localhost_skip_2fa = True
    s.wallet_unlock_timeout = 60
    s.auth_required = True
    s.stall_alert_minutes = 10
    s.stall_level = "alert"
    s.explorer_url = ""
    s.webhook_url = ""
    s.monitor_interval = 3600
    s.recovery_cmd_file = str(tmp_path / "recovery.cmd")
    return s


@pytest.fixture()
def mock_rpc() -> MockRPC:
    return MockRPC()


@pytest.fixture()
def app_state(settings: Settings, mock_rpc: MockRPC) -> AppState:
    state = AppState(settings, mock_rpc,
                      SessionStore(settings.session_secret), username="admin")
    db.init_db(settings.db_path)
    db.ensure_user(settings.db_path, "admin", settings.ui_password)
    return state


@pytest.fixture()
def client(app_state: AppState) -> TestClient:
    app = create_app(app_state)
    app.state.app_state = app_state
    with TestClient(app) as tc:
        yield tc


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from app.ratelimit import limiter
    limiter._events.clear()
    yield
    limiter._events.clear()


LOCAL = {"x-forwarded-for": "127.0.0.1"}
REMOTE = {"x-forwarded-for": "203.0.113.7"}


def login(client: TestClient, password: str = "correct horse battery staple",
          headers: dict | None = LOCAL) -> dict:
    r = client.post("/api/auth/login", json={"password": password},
                    headers=dict(headers or {}))
    assert r.status_code == 200, r.text
    csrf = r.cookies.get("b3_csrf")
    hdr = dict(headers or {})
    hdr["x-csrf-token"] = csrf
    return {"headers": hdr, "body": r.json()}


def unlock(client: TestClient, headers: dict,
           passphrase: str = "test-passphrase") -> None:
    r = client.post("/api/wallet/unlock",
                    json={"passphrase": passphrase}, headers=headers)
    assert r.status_code == 200, r.text

