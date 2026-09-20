"""Test fixtures: mocked RPC layer, temp DB, app factory injection.
No test requires a real node or a funded wallet."""

from pathlib import Path

import pytest
import os

# The TestClient's direct peer is "testclient"; treat it as a trusted
# proxy so X-Forwarded-For fixtures behave like a real trusted proxy.
os.environ.setdefault("B3_TRUSTED_PROXIES", "testclient")

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
            "getstakinginfo": {
            "staking": {"available": True, "running": True, "state": "staking",
                        "finality_signing": False, "last_signed_height": -1,
                        "blocks_produced": 3},
            "stakes": [
                {"txid": "ab" * 32, "vout": 0, "amount": "1000.000000000",
                 "status": "ACTIVE", "owner_address": "SbtSJiDgE7kN4LetizjCLESg6acgubtMj2",
                 "confirmations": 500},
            ],
            "active": "1000.000000000", "pending": "0.000000000",
            "unconfirmed": "0.000000000",
        },
        "sendall": {"complete": True, "hex": "01000000000100000000" * 4},
        "createstake": {"txid": "cs" + "b" * 62, "vout": 1, "amount": "100.000000000",
                        "status": "UNCONFIRMED"},
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
			"getfinalityinfo": {
				"binding": {"bound": True, "revoked": False, "seq": 0},
				"validator_set": {"member": True, "weight": 1000, "total_weight": 2000},
			},
            "getwalletinfo": {"unlocked_until": 0},
            "getbalances": {"mine": {"trusted": 1.0}},
            "getaddressesbylabel": {},
            "listlabels": ["*"],
 "listaddressgroupings": [[{"address": "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv", "label": "mining", "amount": 1.92}]],
			"listreceivedbyaddress": [{"address": "SXyHHJ81ZbFJBzxvMNsjQgQwvKvQEucKSv", "label": "mining", "amount": 1.92, "confirmations": 500, "involvesWatchonly": False}],
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
 "stop": "B3 Hive server stopping",
 "getblockhash": "0000000000000000000000000000000000000000000000000000000000000000",
 "verifytxoutproof": {"txid": "a" * 64},
 "validateaddress": {"isvalid": True, "address": "SbtSJiDgE7kN4LetizjCLESg6acgubtMj2"},
 "help": "== Chain ==\ngetblockchaininfo",
            # Wallet lifecycle (setup wizard create/migrate)
            "listwallets": [],
            "createwallet": {"name": "wallet"},
            "loadwallet": {"name": "wallet"},
            "unloadwallet": None,
			"backupwallet": None,
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

    async def call_unrestricted(self, method: str, *params):
        # Console full-trust path: no allowlist check in the mock either.
        # Unknown methods fail like the real node (RPCError, not a mock
        # AssertionError) so error mapping is exercised realistically.
        self.calls.append((method, params))
        if method in self.fail_methods:
            raise RPCError(-14, "wallet passphrase entered was incorrect")
        if method not in self.responses:
            raise RPCError(-32601, "Method not found")
        return self.responses[method]

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
    # Expert console settings must mirror Settings.__init__ defaults so
    # tests model the real app (the fixture builds Settings via __new__).
    if not hasattr(s, "console_networks"):
        s.console_networks = "127.0.0.1/8,::1/128,172.16.0.0/12"
    if not hasattr(s, "extra_console_methods"):
        s.extra_console_methods = ""
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
    s.b3_data_dir = str(tmp_path)
    s.bootstrap_cmd_file = str(tmp_path / "bootstrap.cmd")
    s.bootstrap_progress_file = str(tmp_path / "bootstrap.progress.json")
    s.wizard_marker_file = str(tmp_path / ".wizard_complete")
    s.daemon_deferred_file = str(tmp_path / ".daemon_deferred")
    s.start_node_cmd_file = str(tmp_path / "start-node.cmd")
    s.bootstrap_manifest_url = ""
    # Model the real fresh-install state: the daemon is DEFERRED until the
    # wizard completes, so the marker EXISTS. The background monitor pauses
    # while it exists and makes no RPC calls — keeping the shared MockRPC
    # clean. (Before this attribute existed, the monitor instead crashed
    # with AttributeError every cycle, which accidentally had the same
    # effect — and masked this fixture requirement.) Tests that want the
    # daemon "started" delete the marker.
    (tmp_path / ".daemon_deferred").touch()
    s.wallet_vault_key = ""
    s.allow_ephemeral_data = True  # tests use tmp_path (not a mount)
    # Transport security (v0.5.0): mirror Settings.__init__ defaults
    if not hasattr(s, "require_secure_transport"):
        s.require_secure_transport = "warn"
    if not hasattr(s, "envelope_encryption"):
        s.envelope_encryption = True
    if not hasattr(s, "cookie_secure"):
        s.cookie_secure = "auto"
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

