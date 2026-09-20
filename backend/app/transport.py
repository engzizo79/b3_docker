"""Transport security detection and policy.

Threat model: the browser-to-backend connection carries the login
password, the wallet passphrase, and the session cookie. Over plain
HTTP on a hostile network, all three are readable and the page code
is swappable. Localhost plain HTTP is the expected desktop/dev case
(no network exposure); REMOTE plain HTTP is the threat this layer
addresses.

X-Forwarded-Proto is honored ONLY when the direct peer is a trusted
proxy (peer_is_trusted_proxy) — a spoofed header from any other peer
is ignored, matching the XFF hardening already in session.py.
"""

from __future__ import annotations

from fastapi import Request

from app.session import client_is_localhost, peer_is_trusted_proxy


def request_scheme(request: Request) -> str:
    """The effective transport scheme, trusted-proxy aware.

    Behind a reverse proxy the direct connection is HTTP (proxy ->
    backend), so request.url.scheme is 'http' even when the browser
    used HTTPS. We trust X-Forwarded-Proto ONLY from a trusted proxy;
    a spoofed header from anyone else is ignored and the direct
    scheme stands (which, for a non-local direct peer, is 'http' ->
    correctly flagged insecure)."""
    if peer_is_trusted_proxy(request):
        proto = request.headers.get("x-forwarded-proto", "")
        if proto:
            return proto.split(",")[0].strip().lower()
    return request.url.scheme.lower()


def transport_is_secure(request: Request) -> bool:
    """True when the transport is encrypted OR the client is local.

    Localhost plain HTTP is the normal desktop/dev case (the browser
    and the node are the same machine; no network segment is exposed).
    A remote client over plain HTTP is the insecure case this layer
    flags and (optionally) blocks."""
    if client_is_localhost(request):
        return True
    return request_scheme(request) == "https"


def insecure_remote(request: Request) -> bool:
    """A remote (non-local) client on an unencrypted transport."""
    return not transport_is_secure(request)
