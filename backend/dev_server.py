"""Development launcher: runs the backend with a mocked RPC layer and a
temp DB so the UI can be exercised without a node. NOT shipped in the image.

Usage: python dev_server.py [port]
Login: admin / b3hive dev x7
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))

from app import db
from app.config import Settings
from app.deps import AppState
from app.session import SessionStore
from app.main import create_app
from tests.conftest import MockRPC
DEV_RPC_PASS = "devpass"  # dev-only dummy value, never a real node credential


def main() -> None:
    import os
    import uvicorn

    tmp = Path(tempfile.mkdtemp(prefix="b3hive-dev-"))
    s = Settings.__new__(Settings)
    s.rpc_host = "127.0.0.1"
    s.rpc_port = 32647
    s.rpc_user = "u"
    s.rpc_password = "p"
    s.db_path = str(tmp / "dev.db")
    s.ui_password = "b3hive dev x7"
    s.session_secret = "dev-session-secret"
    s.totp_key = "dev-totp-key"
    s.localhost_skip_2fa = True
    s.wallet_unlock_timeout = 300
    s.auth_required = True
    s.stall_alert_minutes = 10
    s.stall_level = "alert"
    s.explorer_url = "https://explorer.b3hive.io"
    s.webhook_url = ""
    s.monitor_interval = 3600
    s.recovery_cmd_file = str(tmp / "recovery.cmd")
    s.b3_data_dir = str(tmp)
    s.daemon_deferred_file = str(tmp / ".daemon_deferred")
    s.app_version = "dev"
    s.daemon_log_file = os.environ.get("B3_DAEMON_LOG", str(tmp / "daemon.log"))
    s.explorer_tip_file = str(tmp / "explorer_tip_height")
    s.start_node_cmd_file = str(tmp / "start-node.cmd")
    s.bootstrap_cmd_file = str(tmp / "bootstrap.cmd")
    s.bootstrap_progress_file = str(tmp / "bootstrap.progress.json")
    s.wizard_marker_file = str(tmp / ".wizard_complete")
    s.bootstrap_manifest_url = "https://explorer.b3hive.io/bootstraps/manifest.json"
    s.allow_ephemeral_data = os.environ.get('ALLOW_EPHEMERAL_DATA', 'true').lower() == 'true'  # dev server uses temp dir (not a mount)

    # Dev-only sample node conf so the wizard config view has content.
    (tmp / "b3coin.conf").write_text(
    	"# dev sample\n"
    	"txindex=1\n"
    	"listen=1\n"
    	"listenonion=0\n"
    	"rpcbind=127.0.0.1\n"
    	"rpcallowip=127.0.0.1\n"
    	"rpcport=32647\n"
    	"rpcuser=dev\n"
    	f"rpcpassword={DEV_RPC_PASS}\n"
    	"disablewallet=0\n")

    db.init_db(s.db_path)
    # B3DEV_SETUP_MODE=1 boots with NO operator account: passwordless first
    # run, local-only (setup mode) — for E2E-verifying the wizard Security step.
    import os
    if not os.environ.get("B3DEV_SETUP_MODE"):
        db.ensure_user(s.db_path, "admin", s.ui_password)

    mock = MockRPC()
    mock.responses["listwallets"] = ["wallet"]
    mock.responses["getnetworkinfo"] = {
        "version": 1010500, "subversion": "/B3Hive:1.1.5/", "protocolversion": 70016}
    mock.responses["getbalances"] = {"mine": {"trusted": 5000.0, "untrusted_pending": 0, "immature": 0}}
    mock.responses["getwalletassets"] = {"assets": [
        {"asset_id": "cd" * 32, "kind": "fn", "ticker": "FN", "name": "FN Coin",
         "decimals": 0, "confirmed": 1200, "unconfirmed": 0, "spendable": 1200, "utxos": []},
        {"asset_id": "ab" * 32, "kind": "colored", "ticker": "bUSD", "name": "Bridge USD",
         "decimals": 6, "confirmed": 5000000, "unconfirmed": 0, "spendable": 5000000, "utxos": []},
    ]}
    mock.responses["getassetstate"] = {"next_height": 825000, "fn": {
        "configured": True, "active": True, "asset_id": "cd" * 32, "pod_active": True,
        "counter_known": True, "modern_issued": 200, "modern_capacity": 5000},
        "colored": {"configured": True, "active": True, "issuance_fee": "50.000000000"}}
    mock.responses["listflowmeshmarkets"] = [
        {"market_id": "ef" * 32, "asset_id": "ab" * 32, "state": "open"}]
    if os.environ.get("B3DEV_VALIDATOR_UNBOUND"):
        # Fresh-install staking reality: no stake weight, key never bound, loop off.
        mock.responses["getblockchaininfo"] = {"blocks": 828553, "chain": "main", "headers": 828553}
        mock.responses["getstakinginfo"] = {
            "staking": {"available": True, "running": False, "state": "idle",
                        "finality_signing": False, "last_signed_height": -1,
                        "blocks_produced": 0, "min_stake_amount": "100.000000000"},
            "stakes": [], "active": "0.000000000", "pending": "0.000000000",
            "unconfirmed": "0.000000000"}
        mock.responses["getfinalityinfo"] = {
            "binding": {"bound": False, "revoked": False}}
        mock.responses["createstake"] = {
            "txid": "cs" + "b" * 62, "vout": 1, "amount": "100.000000000",
            "status": "UNCONFIRMED"}
        mock.responses["bindfinalitykey"] = {"txid": "bf" + "b" * 62, "action": "bind"}
        mock.responses["revokefinalitykey"] = {"txid": "rv" + "b" * 62, "action": "revoke"}
        _orig_call = mock.call

        async def _stateful_call(method, *params):
            # The frozen mock chain must track the live explorer tip the
            # monitor writes, or the node looks perpetually behind in dev.
            if method == "getblockchaininfo":
                try:
                    tip = int(Path(s.explorer_tip_file).read_text().strip())
                except Exception:
                    tip = 828553
                mock.responses["getblockchaininfo"] = {
                    "blocks": tip, "headers": tip, "chain": "main"}
            if method == "walletpassphrase" and params and params[0] == "wrong-pass":
                from tests.conftest import RPCError
                raise RPCError(-14, "wallet passphrase entered was incorrect")
            r = await _orig_call(method, *params)
            if method == "createstake":
                mock.responses["getstakinginfo"]["active"] = "100.000000000"
                mock.responses["getstakinginfo"]["stakes"] = [
                    {"txid": "cs" + "b" * 62, "vout": 1, "amount": "100.000000000",
                     "status": "ACTIVE", "confirmations": 500}]
            elif method == "bindfinalitykey":
                mock.responses["getfinalityinfo"]["binding"] = {"bound": True, "revoked": False, "seq": 0}
            elif method == "startstaking":
                mock.responses["getstakinginfo"]["staking"]["running"] = True
                mock.responses["getstakinginfo"]["staking"]["state"] = "staking"
            elif method == "revokefinalitykey":
                mock.responses["getfinalityinfo"]["binding"] = {"bound": False, "revoked": True, "seq": 1}
            return r

        mock.call = _stateful_call

    state = AppState(s, mock, SessionStore(s.session_secret), username="admin")
    app = create_app(state)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    print(f"dev server on http://127.0.0.1:{port} (admin / correct horse battery staple)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
