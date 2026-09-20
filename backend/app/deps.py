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
        # ECDH server key for envelope decryption (loaded by create_app_state).
        self.ecdh_priv = None
        self.ecdh_pub_b64 = ""

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
    # A plain os.path.ismount() is NOT sufficient: the Dockerfile used to
    # declare VOLUME ["/data"], so removing the compose mapping silently
    # created an ANONYMOUS volume — still a mount, but one that dies with
    # `docker compose down` (or gets orphaned on image swaps). That is
    # exactly the fund-loss trap this guard exists for. So the mount source
    # is inspected directly:
    #   - not a mount at all            -> image layer          -> not persistent
    #   - fstype overlay or tmpfs       -> image layer / RAM     -> not persistent
    #   - /var/lib/docker/volumes/<64-hex>/_data -> anonymous vol -> not persistent
    #   - anything else (bind mount, named volume)                -> persistent
    # B3_ALLOW_EPHEMERAL_DATA=true is the explicit operator override for
    # throwaway/demo deployments; the shipped image defaults it to false.
    def data_persistent(self) -> bool:
        if self.settings.allow_ephemeral_data:
            return True
        return _mount_is_persistent(self.settings.b3_data_dir)

    def require_persistent_data(self, action: str = "wallet actions") -> None:
        if not self.data_persistent():
            raise HTTPException(
                status_code=403,
                detail=("data directory is not persistent — " + action
                        + " disabled for safety")
            )


def _mount_is_persistent(data_dir: str,
                         mountinfo_path: str = "/proc/self/mountinfo") -> bool:
    """True only when data_dir sits on a bind mount or a NAMED Docker
    volume — never on the image layer, tmpfs, or an anonymous volume.
    Pure function over mountinfo so it is unit-testable: tests pass a fake
    mountinfo file, production reads /proc/self/mountinfo."""
    import os
    data_dir = os.path.abspath(data_dir)
    best = None  # (mount_point, fstype, source) of the longest matching prefix
    try:
        with open(mountinfo_path, "r", encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 4:
                    continue
                # mountinfo: ... "sep" source fstype ... — find the separator
                try:
                    sep = fields.index("-")
                except ValueError:
                    continue
                mount_point = os.path.abspath(fields[4])
                # mountinfo after the separator: FSTYPE, then SOURCE.
                fstype = fields[sep + 1] if len(fields) > sep + 1 else ""
                source = fields[sep + 2] if len(fields) > sep + 2 else ""
                if data_dir == mount_point or data_dir.startswith(mount_point.rstrip("/") + "/"):
                    if best is None or len(mount_point) > len(best[0]):
                        best = (mount_point, fstype, source)
    except OSError:
        return False  # no mountinfo (non-Linux dev host): refuse to claim persistence
    if best is None:
        return False  # plain image-layer directory (no mount covers it)
    _, fstype, source = best
    if fstype in ("overlay", "tmpfs", "ramfs"):
        return False
    # Anonymous Docker volumes have a 64-hex directory name; named volumes
    # and bind mounts do not. /dev/* sources are real block devices.
    if source.startswith("/var/lib/docker/volumes/"):
        rest = source[len("/var/lib/docker/volumes/"):]
        vol_id = rest.split("/", 1)[0]
        import re as _re
        if _re.fullmatch(r"[0-9a-f]{64}", vol_id):
            return False
    return True


def create_app_state(settings: Settings | None = None,
                     rpc: B3RPCClient | None = None) -> AppState:
    settings = settings or Settings()
    if not settings.session_secret:
        raise RuntimeError("SESSION_SECRET must be set")
    # v0.6.0: external (UI-only) mode connects to the operator-configured
    # node; managed mode stays on loopback (entrypoint wires B3_RPC_*).
    if settings.daemon_mode == "external":
        rpc = rpc or B3RPCClient(settings.ext_rpc_host, settings.ext_rpc_port,
                                 settings.ext_rpc_user, settings.ext_rpc_password)
    else:
        rpc = rpc or B3RPCClient(settings.rpc_host, settings.rpc_port,
                                 settings.rpc_user, settings.rpc_password)
    sessions = SessionStore(settings.session_secret)
    state = AppState(settings, rpc, sessions, username="admin")
    db.init_db(settings.db_path)
    if settings.envelope_encryption:
        priv, pub_b64 = server_keypair(settings.b3_data_dir)
        state.ecdh_priv = priv
        state.ecdh_pub_b64 = pub_b64
    # Passwordless first run: when no UI_PASSWORD is provided the operator
    # account is not created and the app boots in SETUP MODE (local clients
    # only) until the wizard Security step sets a login password. An explicit
    # UI_PASSWORD keeps operator mode (env is authoritative at startup).
    if settings.ui_password:
        db.ensure_user(settings.db_path, state.username, settings.ui_password)
    return state
