"""FastAPI dependency wiring: app state, session guards, and CSRF.

The app state is created once at startup (create_app_state) and lives on
request.app.state. Tests inject mocked components before startup."""

import secrets

from fastapi import HTTPException, Request

from app import db
from app.config import Settings
from app.rpc import B3RPCClient
from app.session import CSRF_COOKIE, SESSION_COOKIE, Session, SessionStore, client_is_localhost


class AppState:
    def __init__(self, settings: Settings, rpc: B3RPCClient,
                 sessions: SessionStore, username: str) -> None:
        self.settings = settings
        self.rpc = rpc
        self.sessions = sessions
        self.username = username

    # -- session helpers -------------------------------------------------

    def session_from_request(self, request: Request) -> tuple[str, Session] | None:
        signed = request.cookies.get(SESSION_COOKIE)
        if not signed:
            return None
        sid = self.sessions.unsign(signed)
        if sid is None:
            return None
        sess = self.sessions.get(sid)
        if sess is None:
            return None
        return sid, sess

    def needs_totp_now(self, request: Request) -> bool:
        """Does THIS client need a TOTP step? (localhost bypass honored)"""
        if client_is_localhost(request) and self.settings.localhost_skip_2fa:
            return False
        return True

    def require_2fa(self, request: Request) -> Session:
        sess = self.require_session(request)
        if self.needs_totp_now(request) and not sess.two_fa_verified:
            raise HTTPException(status_code=403, detail="2FA required")
        return sess

    # -- setup mode --------------------------------------------------------
    # Setup mode = no operator account yet (first run, no UI_PASSWORD).
    # Local clients get full access via a synthetic trusted session;
    # everyone else is locked out until the wizard Security step sets
    # a login password.

    def setup_mode(self) -> bool:
        return not db.user_exists(self.settings.db_path, self.username)

    def require_local(self, request: Request) -> None:
        if not client_is_localhost(request):
            raise HTTPException(status_code=403, detail="local access only during setup")

    def _setup_session(self) -> Session:
        s = Session(username=self.username)
        s.two_fa_verified = True
        return s

    def require_session(self, request: Request) -> Session:
        if self.setup_mode():
            self.require_local(request)
            return self._setup_session()
        pair = self.session_from_request(request)
        if pair is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return pair[1]

    def require_csrf(self, request: Request) -> Session:
        """Session + 2FA + CSRF check for mutating requests
        (double-submit cookie pattern)."""
        if self.setup_mode():
            self.require_local(request)
            return self._setup_session()
        sess = self.require_2fa(request)
        cookie_tok = request.cookies.get(CSRF_COOKIE, "")
        header_tok = request.headers.get("x-csrf-token", "")
        if not cookie_tok or not secrets.compare_digest(cookie_tok, header_tok):
            raise HTTPException(status_code=403, detail="CSRF token missing or invalid")
        return sess

    def require_wallet_unlocked(self, request: Request) -> Session:
        sess = self.require_csrf(request)
        if not sess.wallet_unlocked():
            raise HTTPException(status_code=423, detail="wallet is locked")
        return sess

    # -- data persistence guard ------------------------------------------
    # If /data is not a Docker volume or bind mount (e.g. user removed the
    # VOLUME instruction or the compose mapping), wallet actions are blocked
    # to prevent catastrophic fund loss on container removal.
    def data_persistent(self) -> bool:
        import os
        return os.path.ismount(self.settings.b3_data_dir) or self.settings.allow_ephemeral_data

    def require_persistent_data(self) -> None:
        if not self.data_persistent():
            raise HTTPException(
                status_code=403,
                detail="data directory is not persistent — wallet actions disabled for safety"
            )


def create_app_state(settings: Settings | None = None,
                     rpc: B3RPCClient | None = None) -> AppState:
    settings = settings or Settings()
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET must be set")
    rpc = rpc or B3RPCClient(settings.rpc_host, settings.rpc_port,
                              settings.rpc_user, settings.rpc_password)
    sessions = SessionStore(settings.session_secret)
    state = AppState(settings, rpc, sessions, username="admin")
    db.init_db(settings.db_path)
    # Passwordless first run: when no UI_PASSWORD is provided the operator
    # account is not created and the app boots in SETUP MODE (local clients
    # only) until the wizard Security step sets a login password. An explicit
    # UI_PASSWORD keeps operator mode (env is authoritative at startup).
    if settings.ui_password:
        db.ensure_user(settings.db_path, state.username, settings.ui_password)
    return state
