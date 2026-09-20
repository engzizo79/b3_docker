"""Auth endpoints: login, TOTP 2FA, wallet unlock, logout, session status.

Transport security: remote plain-HTTP sessions are flagged (warn) or refused
(block) per REQUIRE_SECURE_TRANSPORT. The login password and wallet
passphrase may be envelope-encrypted (ECDH P-256 + AES-GCM) so the cleartext
never crosses the wire even on plain HTTP."""

import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app import db, totp
from app.deps import AppState
from app.ratelimit import limiter
from app.session import CSRF_COOKIE, SESSION_COOKIE
from app.transport import insecure_remote, transport_is_secure
from app.crypto_envelope import decrypt_envelope

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/pubkey")
async def auth_pubkey(request: Request):
    """Server ECDH public key (P-256, raw uncompressed point, base64) for
    client-side envelope encryption of the login password and wallet
    passphrase. Returns 404 when envelope encryption is disabled so the
    frontend falls back to plain (with the transport banner if remote-HTTP)."""
    state: AppState = request.app.state.app_state
    if not state.settings.envelope_encryption or not state.ecdh_pub_b64:
        raise HTTPException(status_code=404, detail="envelope encryption disabled")
    return {"pubkey": state.ecdh_pub_b64}


class TOTPBody(BaseModel):
    code: str


class UnlockBody(BaseModel):
    passphrase: str
    timeout: int | None = None


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _cookie_secure(request: Request, state: AppState) -> bool:
    """Secure cookie flag. 'true'/'false' force it; 'auto' (default) sets it
    when the request arrived over HTTPS, directly or via a trusted reverse
    proxy passing X-Forwarded-Proto. HTTP localhost dev stays functional."""
    mode = getattr(state.settings, "cookie_secure", "auto")
    if mode == "true":
        return True
    if mode == "false":
        return False
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    if proto:
        return proto == "https"
    return request.url.scheme == "https"


def _set_session_cookie(response: Response, state: AppState, sid: str,
                         request: Request) -> None:
    response.set_cookie(
        SESSION_COOKIE, state.sessions.sign(sid),
        httponly=True, samesite="strict", path="/",
        secure=_cookie_secure(request, state),
    )


@router.post("/login")
async def login(request: Request, response: Response):
    """Login with optional ECDH envelope encryption. The request body is read
    as raw JSON so an `env` envelope can be extracted alongside the legacy
    `password` field. Transport policy: block mode refuses remote plain-HTTP."""
    state: AppState = request.app.state.app_state
    if state.settings.require_secure_transport == "block" and insecure_remote(request):
        raise HTTPException(status_code=426,
                            detail="secure transport required: use HTTPS or access from localhost")
    ip = _client_ip(request)
    if not limiter.allow("login", ip, limit=5, window_s=60):
        raise HTTPException(status_code=429, detail="too many attempts, wait a minute")

    body = await request.json()
    password = body.get("password", "")
    env = body.get("env")
    if isinstance(env, dict) and state.settings.envelope_encryption and state.ecdh_priv:
        try:
            password = decrypt_envelope(state.ecdh_priv, env)
        except Exception:
            raise HTTPException(status_code=400, detail="could not decrypt password envelope")

    user = db.verify_password(state.settings.db_path, state.username, password)
    if user is None:
        db.audit(state.settings.db_path, "login", state.username, ip, success=False)
        raise HTTPException(status_code=401, detail="invalid credentials")

    sid, sess = state.sessions.create(state.username)
    needs_totp = state.needs_totp_now(request) and bool(user["totp_enabled"])
    if not needs_totp:
        sess.two_fa_verified = True
    # Session + CSRF cookies are ALWAYS set: a TOTP-pending session must
    # exist so the 2FA step can verify against it.
    _set_session_cookie(response, state, sid, request)
    # CSRF double-submit cookie: readable by JS (not httponly) by design.
    response.set_cookie(CSRF_COOKIE, sess.csrf_token, samesite="strict", path="/",
                        secure=_cookie_secure(request, state))
    db.audit(state.settings.db_path, "login", state.username, ip)
    return {"ok": True, "totp_required": needs_totp}


@router.post("/2fa")
async def verify_2fa(body: TOTPBody, request: Request, response: Response):
    state: AppState = request.app.state.app_state
    ip = _client_ip(request)
    sess = state.require_session(request)
    if not limiter.allow("totp", ip, limit=5, window_s=60):
        raise HTTPException(status_code=429, detail="too many attempts, wait a minute")

    user = db.get_user(state.settings.db_path, state.username)
    if not user or not user["totp_secret_enc"]:
        raise HTTPException(status_code=400, detail="TOTP not configured")
    secret = totp.decrypt_secret(state.settings.totp_key, user["totp_secret_enc"])

    if totp.verifier.verify(secret, body.code.strip()):
        sess.two_fa_verified = True
        db.audit(state.settings.db_path, "2fa_verify", state.username, ip)
        return {"ok": True}

    # Fall back to one-time recovery codes.
    if db.consume_recovery_code(state.settings.db_path, state.username, body.code):
        sess.two_fa_verified = True
        db.audit(state.settings.db_path, "2fa_recovery_code_used", state.username, ip)
        return {"ok": True, "recovery_code": True}

    db.audit(state.settings.db_path, "2fa_verify", state.username, ip, success=False)
    raise HTTPException(status_code=401, detail="invalid code")


@router.get("/status")
async def auth_status(request: Request):
    state: AppState = request.app.state.app_state
    secure_transport = transport_is_secure(request)
    if state.setup_mode():
        # Local client in setup mode: full access, no login step.
        return {
            "authenticated": True,
            "username": state.username,
            "setup_required": True,
            "totp_required": False,
            "totp_configured": False,
            "wallet_unlocked": False,
            "transport_secure": secure_transport,
            "transport_policy": state.settings.require_secure_transport,
            "envelope_encryption": bool(state.settings.envelope_encryption and state.ecdh_pub_b64),
        }
    pair = state.session_from_request(request)
    if pair is None:
        return {"authenticated": False, "setup_required": False,
                "transport_secure": secure_transport,
                "transport_policy": state.settings.require_secure_transport,
                "envelope_encryption": bool(state.settings.envelope_encryption and state.ecdh_pub_b64)}
    sid, sess = pair
    return {
        "authenticated": True,
        "username": sess.username,
        "totp_required": state.needs_totp_now(request) and not sess.two_fa_verified,
        "totp_configured": bool(
            (db.get_user(state.settings.db_path, state.username) or {}).get("totp_enabled")
        ),
        "wallet_unlocked": sess.wallet_unlocked(),
        "setup_required": False,
        "transport_secure": secure_transport,
        "transport_policy": state.settings.require_secure_transport,
        "envelope_encryption": bool(state.settings.envelope_encryption and state.ecdh_pub_b64),
    }


@router.post("/totp/setup")
async def totp_setup(request: Request):
    """Generate a new TOTP secret. Returns provisioning URI + recovery codes.
    The secret is stored encrypted; the QR is shown ONCE. Requires login+2FA
    (localhost) or login+2FA (remote) — i.e. a verified session."""
    state: AppState = request.app.state.app_state
    state.require_2fa(request)
    secret = totp.generate_secret()
    enc = totp.encrypt_secret(state.settings.totp_key, secret)
    db.set_totp_secret(state.settings.db_path, state.username, enc, enabled=True)
    codes = db.generate_recovery_codes(state.settings.db_path, state.username)
    db.audit(state.settings.db_path, "totp_setup", state.username)
    return {
        "provisioning_uri": totp.provisioning_uri(secret, state.username),
        "recovery_codes": codes,
    }


@router.post("/logout")
async def logout(request: Request, response: Response):
    state: AppState = request.app.state.app_state
    pair = state.session_from_request(request)
    if pair is not None:
        state.sessions.destroy(pair[0])
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    db.audit(state.settings.db_path, "logout", state.username)
    return {"ok": True}


class SetPasswordBody(BaseModel):
    password: str


@router.post("/setup-password")
async def setup_password(body: SetPasswordBody, request: Request):
    """First-run security step: set the login password. Local client only,
    exits setup mode. Length is enforced; the audit log records the event."""
    state: AppState = request.app.state.app_state
    if not state.setup_mode():
        raise HTTPException(status_code=400, detail="password already set")
    state.require_local(request)
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")
    db.set_password(state.settings.db_path, state.username, body.password)
    db.audit(state.settings.db_path, "security.password_set", state.username,
             _client_ip(request), success=True)
    return {"ok": True, "setup_required": False}
