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


def main() -> None:
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

    db.init_db(s.db_path)
    db.ensure_user(s.db_path, "admin", s.ui_password)

    state = AppState(s, MockRPC(), SessionStore(s.session_secret), username="admin")
    app = create_app(state)
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8899
    print(f"dev server on http://127.0.0.1:{port} (admin / correct horse battery staple)")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
