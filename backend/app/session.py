"""Server-side sessions with a signed cookie handle, CSRF tokens, and
client classification (localhost vs remote).

Session store is in-memory: sessions die with the process (acceptable — the
user simply logs in again; it also invalidates stolen cookies on restart)."""

import ipaddress
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

from itsdangerous import BadSignature, URLSafeSerializer

SESSION_COOKIE = "b3_session"
CSRF_COOKIE = "b3_csrf"
SESSION_TTL_S = 8 * 3600 # 8 hours idle timeout

_LOCALHOST = {"127.0.0.1", "::1", "localhost"}

# Narrowest network an operator may declare "local" via B3_LOCAL_ADDRS.
# Anything broader (0.0.0.0/0, a whole /8) would silently make strangers
# "local" — which skips 2FA and opens first-run setup — so it is refused.
_MIN_PREFIX_V4 = 16
_MIN_PREFIX_V6 = 64

log = logging.getLogger(__name__)


def _extra_local_networks() -> list:
    """Networks the operator explicitly declared local via B3_LOCAL_ADDRS
    (comma-separated IPs or CIDRs). Invalid or overly broad entries are
    ignored with a warning — fail closed."""
    nets = []
    for tok in os.environ.get("B3_LOCAL_ADDRS", "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            net = ipaddress.ip_network(tok, strict=False)
        except ValueError:
            log.warning("B3_LOCAL_ADDRS: ignoring invalid entry %r", tok)
            continue
        floor = _MIN_PREFIX_V4 if net.version == 4 else _MIN_PREFIX_V6
        if net.prefixlen < floor:
            log.warning("B3_LOCAL_ADDRS: ignoring %r — broader than /%d would "
                        "make remote clients count as local", tok, floor)
            continue
        nets.append(net)
    return nets


def _default_gateway_addrs() -> set:
    """Default-route gateways from /proc/net/route. In Docker, connections
    from the HOST to a published port arrive with the bridge gateway as the
    source IP — that is the Docker host, i.e. the local machine. Real
    LAN/remote clients keep their own source IP (DNAT) and stay remote.
    Disabled by B3_TRUST_DOCKER_HOST=false (see docs/SECURITY.md)."""
    if os.environ.get("B3_TRUST_DOCKER_HOST", "true").strip().lower() == "false":
        return set()
    addrs = set()
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    hexgw = parts[2]
                    try:
                        addrs.add(".".join(str(int(hexgw[i:i + 2], 16))
                                           for i in (6, 4, 2, 0)))
                    except ValueError:
                        continue
    except OSError:
        pass
    return addrs


def is_local_address(host: str) -> bool:
    """Is this source address 'the local machine'? Loopback (127.0.0.1 /
    ::1), the Docker host (the container's default gateway — unless
    B3_TRUST_DOCKER_HOST=false), and addresses listed in B3_LOCAL_ADDRS."""
    if not host:
        return False
    if host in _LOCALHOST or host in _default_gateway_addrs():
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _extra_local_networks())


def peer_is_trusted_proxy(request) -> bool:
    """Is the DIRECT connection peer a proxy whose X-Forwarded-For we
    may believe? Only loopback and entries of B3_TRUSTED_PROXIES
    (CIDRs or exact names) qualify. Without this, any remote client could
    spoof X-Forwarded-For: 127.0.0.1 and become "localhost" — bypassing
    2FA and gaining full console trust. Fails closed."""
    host = (request.client.host if request.client else "") or ""
    if not host:
        return False
    if host in _LOCALHOST:  # the local machine itself
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
    """True when the request originates from the local machine: loopback,
    the Docker host, or an address listed in B3_LOCAL_ADDRS. A reverse proxy
    only counts when it forwards a local client — and only when the proxy
    itself is trusted (XFF is not spoofable)."""
    return is_local_address(effective_client_ip(request))

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
