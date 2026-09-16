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

    def require_session(self, request: Request) -> Session:
        pair = self.session_from_request(request)
        if pair is None:
            raise HTTPException(status_code=401, detail="not authenticated")
        return pair[1]

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

    def require_csrf(self, request: Request) -> Session:
        """Session + 2FA + CSRF check for mutating requests
        (double-submit cookie pattern)."""
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


def create_app_state(settings: Settings | None = None,
                     rpc: B3RPCClient | None = None) -> AppState:
    settings = settings or Settings()
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET must be set")
    if not settings.ui_password:
        raise RuntimeError("UI_PASSWORD must be set")
    rpc = rpc or B3RPCClient(settings.rpc_host, settings.rpc_port,
                             settings.rpc_user, settings.rpc_password)
    sessions = SessionStore(settings.session_secret)
    state = AppState(settings, rpc, sessions, username="admin")
    db.init_db(settings.db_path)
    db.ensure_user(settings.db_path, state.username, settings.ui_password)
    return state
