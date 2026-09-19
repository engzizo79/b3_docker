"""Server-side sessions with a signed cookie handle, CSRF tokens, and
client classification (localhost vs remote).

Session store is in-memory: sessions die with the process (acceptable — the
user simply logs in again; it also invalidates stolen cookies on restart)."""

import ipaddress
import os
import secrets
import time
from dataclasses import dataclass, field

from itsdangerous import BadSignature, URLSafeSerializer

SESSION_COOKIE = "b3_session"
CSRF_COOKIE = "b3_csrf"
SESSION_TTL_S = 8 * 3600 # 8 hours idle timeout

_LOCALHOST = {"127.0.0.1", "::1", "localhost"}


def _default_gateway_addrs() -> set:
    """Default-route gateways from /proc/net/route. In Docker, connections
    from the HOST to a published port arrive with the bridge gateway as
    the source IP — that is the local machine, so it counts as local.
    Real LAN/remote clients keep their own source IP (DNAT) and stay
    remote. """
    addrs = set()
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    hexgw = parts[2]
                    try:
                        ip = ".".join(str(int(hexgw[i:i + 2], 16)) for i in (6, 4, 2, 0))
                        addrs.add(ip)
                    except ValueError:
                        continue
    except OSError:
        pass
    return addrs


def local_addresses() -> set:
    """Addresses treated as 'the local machine': loopback, container default
    gateways (Docker host), and B3_LOCAL_ADDRS overrides (comma-separated)."""
    addrs = set(_LOCALHOST)
    addrs |= _default_gateway_addrs()
    extra = os.environ.get("B3_LOCAL_ADDRS", "")
    addrs |= {a.strip() for a in extra.split(",") if a.strip()}
    return addrs


def peer_is_trusted_proxy(request) -> bool:
    """Is the DIRECT connection peer a proxy whose X-Forwarded-For we
    may believe? Only the local machine and entries of B3_TRUSTED_PROXIES
    (CIDRs or exact names) qualify. Without this, any remote client could
    spoof X-Forwarded-For: 127.0.0.1 and become "localhost" — bypassing
    2FA and gaining full console trust. Fails closed."""
    host = (request.client.host if request.client else "") or ""
    if not host:
        return False
    if host in local_addresses():  # the local machine itself
        return True
    names, nets = [], []
    for tok in os.environ.get("B3_TRUSTED_PROXIES", "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            nets.append(ipaddress.ip_network(tok, strict=False))
        except ValueError:
            names.append(tok.lower())
    if host.lower() in names:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in nets)


def effective_client_ip(request) -> str:
    """The real client IP. X-Forwarded-For is honored ONLY when the
    direct peer is a trusted proxy (see peer_is_trusted_proxy); a
    spoofed XFF from any other peer is ignored and the peer IP itself
    is the answer."""
    host = (request.client.host if request.client else "") or ""
    if peer_is_trusted_proxy(request):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return host


def client_is_localhost(request) -> bool:
    """True when the request originates from the local machine: the
    container itself, the host loopback, or the Docker bridge gateway
    (host connections to a published port arrive from the gateway IP).
    A reverse proxy only counts when it forwards a loopback client —
    and only when the proxy itself is trusted (XFF is not spoofable)."""
    return effective_client_ip(request) in local_addresses()

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
