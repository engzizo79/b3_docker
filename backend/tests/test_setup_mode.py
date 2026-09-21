"""Setup mode (passwordless first run, local-only) contract tests.

Covers the user journey: fresh boot with NO UI_PASSWORD -> no operator
account -> local clients get full access without a login screen; remote
clients are locked out; the wizard Security step sets the password and
exits setup mode; afterwards the normal login flow applies again.
"""

from fastapi.testclient import TestClient

from app import db
from app.main import create_app
from app.deps import AppState
from app.session import SessionStore
from tests.conftest import LOCAL, REMOTE


def _setup_state(settings, mock_rpc) -> AppState:
    """App state WITHOUT an operator account (passwordless first run)."""
    state = AppState(settings, mock_rpc,
                     SessionStore(settings.session_secret), username="admin")
    db.init_db(settings.db_path)
    # NB: deliberately NO db.ensure_user -> setup mode
    return state


_setup_client: TestClient | None = None


def test_setup_mode_local_gets_full_access(settings, mock_rpc):
    state = _setup_state(settings, mock_rpc)
    app = create_app(state)
    app.state.app_state = state
    with TestClient(app) as tc:
        # status reports setup_required and authenticated for local clients
        r = tc.get("/api/auth/status", headers=LOCAL)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["setup_required"] is True
        assert body["authenticated"] is True
        # a protected GET endpoint works without a login (setup session)
        r = tc.get("/api/setup/status", headers=LOCAL)
        assert r.status_code == 200, r.text


def test_setup_mode_remote_is_locked_out(settings, mock_rpc):
    state = _setup_state(settings, mock_rpc)
    app = create_app(state)
    app.state.app_state = state
    with TestClient(app) as tc:
        r = tc.get("/api/auth/status", headers=REMOTE)
        assert r.status_code == 403, r.text
        assert "setup" in r.json()["detail"].lower()
        r = tc.get("/api/setup/status", headers=REMOTE)
        assert r.status_code == 403, r.text
        # SPA shell is also blocked remotely
        r = tc.get("/", headers=REMOTE)
        assert r.status_code == 403, r.text


def test_setup_password_sets_and_exits(settings, mock_rpc):
    state = _setup_state(settings, mock_rpc)
    app = create_app(state)
    app.state.app_state = state
    with TestClient(app) as tc:
        # short password rejected
        r = tc.post("/api/auth/setup-password", json={"password": "short"},
                    headers=LOCAL)
        assert r.status_code == 400, r.text
        # remote cannot set the password
        r = tc.post("/api/auth/setup-password", json={"password": "longenough123"},
                    headers=REMOTE)
        assert r.status_code == 403, r.text
        # local sets it
        r = tc.post("/api/auth/setup-password",
                    json={"password": "new-strong-pass-123"}, headers=LOCAL)
        assert r.status_code == 200, r.text
        assert state.setup_mode() is False
        # status no longer reports setup_required (session still valid path)
        r = tc.get("/api/auth/status", headers=LOCAL)
        assert r.json()["setup_required"] is False
        # login with the new password works
        r = tc.post("/api/auth/login", json={"password": "new-strong-pass-123"},
                    headers=LOCAL)
        assert r.status_code == 200, r.text


def test_wizard_complete_blocked_until_password_set(settings, mock_rpc):
    state = _setup_state(settings, mock_rpc)
    app = create_app(state)
    app.state.app_state = state
    with TestClient(app) as tc:
        r = tc.post("/api/setup/complete", headers=LOCAL)
        assert r.status_code == 403, r.text
        assert "password" in r.json()["detail"].lower()
        # set the password, log in (real session), then complete works
        tc.post("/api/auth/setup-password",
                json={"password": "wizard-pass-123"}, headers=LOCAL)
        r = tc.post("/api/auth/login",
                json={"password": "wizard-pass-123"}, headers=LOCAL)
        assert r.status_code == 200, r.text
        hdr = dict(LOCAL)
        hdr["x-csrf-token"] = tc.cookies.get("b3_csrf")
        r = tc.post("/api/setup/complete", headers=hdr)
        assert r.status_code == 200, r.text


def test_operator_mode_unchanged(settings, mock_rpc):
    """Explicit UI_PASSWORD keeps the old operator behavior."""
    state = AppState(settings, mock_rpc,
                     SessionStore(settings.session_secret), username="admin")
    db.init_db(settings.db_path)
    db.ensure_user(settings.db_path, "admin", settings.ui_password)
    app = create_app(state)
    app.state.app_state = state
    with TestClient(app) as tc:
        assert state.setup_mode() is False
        r = tc.get("/api/auth/status", headers=LOCAL)
        assert r.json()["setup_required"] is False
        # unauthenticated client cannot access protected endpoints
        r = tc.get("/api/setup/status", headers=LOCAL)
        assert r.status_code == 401, r.text
        # remote SPA not blocked (operator mode has no local-only window)
        r = tc.get("/", headers=REMOTE)
        assert r.status_code in (200, 404), r.text


def test_setup_session_cannot_unlock_wallet(settings, mock_rpc):
    """Node-level unlock is local-allowed, but unlock STATE never sticks:
    the synthetic session is per-request; signing stays blocked until a
    real login session exists."""
    state = _setup_state(settings, mock_rpc)
    app = create_app(state)
    app.state.app_state = state
    with TestClient(app) as tc:
        r = tc.post("/api/wallet/unlock",
                    json={"passphrase": "whatever"}, headers=LOCAL)
        # node-level unlock succeeds for the local trusted client...
        assert r.status_code == 200, r.text
        # ...but the unlock state does NOT persist: fresh synthetic session
        r = tc.get("/api/auth/status", headers=LOCAL)
        assert r.json()["wallet_unlocked"] is False
        # and a send (signing) action is still blocked without a real session
        r = tc.post("/api/wallet/send",
                json={"recipients": [{
                        "address": "SbtSJiDgE7kN4LetizjCLESg6acgubtMj2", "amount": "1"}],
                        "confirm": False},
                headers=LOCAL)
        assert r.status_code in (401, 423, 403), r.text

def test_setup_lockout_tells_operator_what_the_server_saw(settings, mock_rpc):
    """A Docker-host browser arrives from the bridge gateway, which is NOT
    local by default. The lockout must say which address was seen and how to
    opt in (B3_LOCAL_ADDRS), and must not reflect it unescaped."""
    state = _setup_state(settings, mock_rpc)
    app = create_app(state)
    app.state.app_state = state
    gw = {"x-forwarded-for": "172.17.0.1"}
    with TestClient(app) as tc:
        r = tc.get("/", headers=gw)
        assert r.status_code == 403
        assert "172.17.0.1" in r.text and "B3_LOCAL_ADDRS" in r.text
        r = tc.get("/api/setup/status", headers=gw)
        assert r.status_code == 403
        assert r.json()["client_ip"] == "172.17.0.1"
        r = tc.get("/", headers={"x-forwarded-for": "<script>x</script>"})
        assert "<script>x</script>" not in r.text
