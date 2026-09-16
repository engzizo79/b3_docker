"""Server-side sessions with a signed cookie handle, CSRF tokens, and
client classification (localhost vs remote).

Session store is in-memory: sessions die with the process (acceptable — the
user simply logs in again; it also invalidates stolen cookies on restart)."""

import secrets
import time
from dataclasses import dataclass, field

from itsdangerous import BadSignature, URLSafeSerializer

SESSION_COOKIE = "b3_session"
CSRF_COOKIE = "b3_csrf"
SESSION_TTL_S = 8 * 3600  # 8 hours idle timeout

_LOCALHOST = {"127.0.0.1", "::1", "localhost"}


def client_is_localhost(request) -> bool:
    """True when the request originates from the container itself or the
    host loopback (e.g. via published port on the same machine)."""
    host = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "")
    # Behind a reverse proxy the direct client is the proxy; only proxy-
    # forwarded loopback counts as local.
    if forwarded:
        first = forwarded.split(",")[0].strip()
        return first in _LOCALHOST
    return host in _LOCALHOST


@dataclass
class Session:
    username: str
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    two_fa_verified: bool = False
    wallet_unlocked_until: float = 0.0
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    def expired(self) -> bool:
        return (time.time() - self.last_seen) > SESSION_TTL_S

    def wallet_unlocked(self) -> bool:
        return time.time() < self.wallet_unlocked_until


class SessionStore:
    def __init__(self, secret: str) -> None:
        self._signer = URLSafeSerializer(secret, salt="b3hive-session")
        self._sessions: dict[str, Session] = {}

    def create(self, username: str) -> tuple[str, Session]:
        sid = secrets.token_urlsafe(32)
        sess = Session(username=username)
        self._sessions[sid] = sess
        return sid, sess

    def get(self, sid: str) -> Session | None:
        sess = self._sessions.get(sid)
        if sess is None or sess.expired():
            self._sessions.pop(sid, None)
            return None
        sess.last_seen = time.time()
        return sess

    def destroy(self, sid: str) -> None:
        self._sessions.pop(sid, None)

    def sign(self, sid: str) -> str:
        return self._signer.dumps(sid)

    def unsign(self, signed: str) -> str | None:
        try:
            return self._signer.loads(signed)
        except BadSignature:
            return None
