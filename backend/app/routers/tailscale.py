"""Tailscale remote-access endpoints (v0.7.0).

The container bundles tailscaled (userspace networking, state under
<data>/ts so the tailnet identity survives container recreation). The
backend controls it through the local socket as the same non-root user.

- GET  /api/tailscale/status: connection state, tailnet name, HTTPS URL.
- POST /api/tailscale/join: join the tailnet with an auth key. The key is
  NEVER written to the audit log or returned; it is passed to the CLI once.
- POST /api/tailscale/serve: enable tailscale serve on the web port —
  gives a browser-valid HTTPS URL (Let's Encrypt cert for <host>.ts.net)
  without the user owning a domain.
- POST /api/tailscale/leave: leave the tailnet (disables remote access).

Security: join/serve/leave are authenticated + CSRF and audit-logged.
This is the product's answer to remote access over plain HTTP: the
.ts.net URL is real HTTPS, which also makes the browser a secure context,
so WebCrypto envelope encryption is always available remotely.
"""

import asyncio
import json
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import settings
from app.db import audit
from app.deps import AppState

router = APIRouter(prefix="/api/tailscale", tags=["tailscale"])

_TS_BIN = "/usr/local/bin/tailscale"


def _state(request: Request) -> AppState:
    return request.app.state.app_state


def _socket() -> str:
    return f"{settings.b3_data_dir}/ts/tailscaled.sock"


def _client_ip(request: Request) -> str:
    return request.headers.get("x-forwarded-for", "") or (
        request.client.host if request.client else "")


async def _ts(*args: str, timeout: float = 45.0) -> tuple[int, str]:
    """Run the tailscale CLI against the local socket. (rc, output)."""
    cmd = [_TS_BIN, "--socket", _socket(), *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "tailscale CLI timed out")
    return proc.returncode or 0, out.decode(errors="replace").strip()


class JoinBody(BaseModel):
    authkey: str


class ServeBody(BaseModel):
    enable: bool = True


@router.get("/status")
async def tailscale_status(request: Request):
    """Connection state + HTTPS URL. No secrets in output."""
    state = _state(request)
    state.require_2fa(request)
    try:
        rc, out = await _ts("status", "--json", "--peers=false")
    except FileNotFoundError:
        return {"available": False, "joined": False,
                "https_url": None, "serve_enabled": False,
                "detail": "tailscale not installed"}
    if rc != 0 or not out:
        return {"available": True, "joined": False,
                "https_url": None, "serve_enabled": False,
                "detail": "tailscaled not reachable"}
    try:
        st = json.loads(out)
    except ValueError:
        return {"available": True, "joined": False,
                "https_url": None, "serve_enabled": False,
                "detail": "unparsable status"}
    backend = st.get("BackendState") or ""
    joined = backend in ("Running", "NeedsLogin") and bool(st.get("Self", {}))
    https_url = None
    serve_enabled = False
    if joined:
        dns_name = (st.get("Self") or {}).get("DNSName", "")
        if dns_name:
            https_url = f"https://{dns_name.rstrip('.')}"
        try:
            src, s_out = await _ts("serve", "status", "--json")
            if src == 0 and s_out:
                serve_enabled = bool(json.loads(s_out))
        except (HTTPException, ValueError):
            pass
    return {"available": True, "joined": joined,
            "https_url": https_url, "serve_enabled": serve_enabled,
            "tailnet": (st.get("CurrentTailnet") or {}).get("Name"),
            "detail": backend}


@router.post("/join")
async def tailscale_join(body: JoinBody, request: Request):
    """Join the tailnet with an auth key. Key never logged or returned."""
    state = _state(request)
    sess = state.require_csrf(request)
    ip = _client_ip(request)
    key = (body.authkey or "").strip()
    if not key.startswith("tskey-"):
        raise HTTPException(422, "invalid auth key (must start with tskey-)")
    try:
        # --reset: a node with prior non-default settings otherwise fails
        # with "changing settings via 'tailscale up' requires mentioning
        # all non-default flags". Wait briefly for tailscaled's socket.
        for _ in range(10):
            if os.path.exists(_socket()):
                break
            await asyncio.sleep(1)
        rc, out = await _ts("up", "--reset", "--authkey", key,
                            "--timeout", "30s", timeout=60.0)
    except FileNotFoundError:
        raise HTTPException(503, "tailscale not installed")
    if rc != 0:
        db_audit_join_fail(state, sess, ip, out)
        raise HTTPException(409, f"join failed: {out[-300:]}")
    # serve persists in tailscaled state; no need to re-enable after join
    db_audit_join_ok(state, sess, ip)
    return {"ok": True}


def db_audit_join_ok(state, sess, ip):
    audit(state.settings.db_path, "tailscale.join", sess.username,
          detail=f"ip={ip}")


def db_audit_join_fail(state, sess, ip, out):
    # redact any accidental key material from CLI output before logging
    safe = out[-300:].replace("tskey-", "tskey-[REDACTED")
    audit(state.settings.db_path, "tailscale.join", sess.username,
          detail=f"ip={ip} error={safe}", success=False)


@router.post("/serve")
async def tailscale_serve(body: ServeBody, request: Request):
    """Enable/disable tailscale serve for the web port (HTTPS)."""
    state = _state(request)
    sess = state.require_csrf(request)
    ip = _client_ip(request)
    enable = body.enable
    try:
        if enable:
            port = os.environ.get("WEB_PORT", "8080")
            rc, out = await _ts("serve", "--https=443", port,
                                timeout=45.0)
        else:
            rc, out = await _ts("serve", "--https=443", "off",
                                timeout=45.0)
    except FileNotFoundError:
        raise HTTPException(503, "tailscale not installed")
    if rc != 0:
        audit(state.settings.db_path, "tailscale.serve", sess.username,
              detail=f"ip={ip} enable={enable} error={out[-300:]}",
              success=False)
        raise HTTPException(409, f"serve failed: {out[-300:]}")
    audit(state.settings.db_path, "tailscale.serve", sess.username,
          detail=f"ip={ip} enable={enable}")
    return {"ok": True, "enabled": enable}


@router.post("/leave")
async def tailscale_leave(request: Request):
    """Leave the tailnet (logout). Identity file is kept, not deleted."""
    state = _state(request)
    sess = state.require_csrf(request)
    ip = _client_ip(request)
    try:
        rc, out = await _ts("logout", timeout=30.0)
    except FileNotFoundError:
        raise HTTPException(503, "tailscale not installed")
    if rc != 0:
        audit(state.settings.db_path, "tailscale.leave", sess.username,
              detail=f"ip={ip} error={out[-300:]}", success=False)
        raise HTTPException(409, f"leave failed: {out[-300:]}")
    audit(state.settings.db_path, "tailscale.leave", sess.username,
          detail=f"ip={ip}")
    return {"ok": True}
