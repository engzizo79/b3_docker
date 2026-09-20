"""Transport security (v0.5.0): detection, envelope encryption, policy."""

import base64
import os

import pytest
from fastapi.testclient import TestClient

from app import db
from app.crypto_envelope import server_keypair
from app.deps import AppState
from app.main import create_app
from app.session import SessionStore
from tests.conftest import LOCAL, REMOTE

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


@pytest.fixture()
def ecdh_state(settings, mock_rpc) -> AppState:
    """AppState with the ECDH server key loaded (mirrors create_app_state)."""
    state = AppState(settings, mock_rpc,
                     SessionStore(settings.session_secret), username="admin")
    db.init_db(settings.db_path)
    db.ensure_user(settings.db_path, "admin", settings.ui_password)
    if settings.envelope_encryption:
        priv, pub_b64 = server_keypair(settings.b3_data_dir)
        state.ecdh_priv = priv
        state.ecdh_pub_b64 = pub_b64
    return state


def make_envelope(server_pub_b64: str, secret: str,
                  salt: bytes = b"salt", info: bytes = b"info") -> dict:
    """Client-side envelope, exactly as the browser's WebCrypto code builds it."""
    server_pub_raw = base64.b64decode(server_pub_b64)
    server_pub = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), server_pub_raw)
    client_priv = ec.generate_private_key(ec.SECP256R1())
    client_pub_raw = client_priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = client_priv.exchange(ec.ECDH(), server_pub)
    key = HKDF(algorithm=hashes.SHA256(), length=32,
               salt=salt, info=info).derive(shared)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, secret.encode("utf-8"), None)
    return {
        "client_pub": base64.b64encode(client_pub_raw).decode("ascii"),
        "iv": base64.b64encode(iv).decode("ascii"),
        "ct": base64.b64encode(ct).decode("ascii"),
        "salt": base64.b64encode(salt).decode("ascii"),
        "info": base64.b64encode(info).decode("ascii"),
    }


def test_pubkey_endpoint(ecdh_state):
    with TestClient(create_app(ecdh_state)) as tc:
        r = tc.get("/api/auth/pubkey")
    assert r.status_code == 200
    assert r.json()["pubkey"] == ecdh_state.ecdh_pub_b64


def test_pubkey_404_when_disabled(ecdh_state):
    ecdh_state.settings.envelope_encryption = False
    with TestClient(create_app(ecdh_state)) as tc:
        r = tc.get("/api/auth/pubkey")
    assert r.status_code == 404


def test_login_with_envelope(ecdh_state):
    env = make_envelope(ecdh_state.ecdh_pub_b64, "correct horse battery staple")
    with TestClient(create_app(ecdh_state)) as tc:
        r = tc.post("/api/auth/login", json={"env": env}, headers=dict(LOCAL))
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_login_with_garbage_envelope_rejected(ecdh_state):
    env = make_envelope(ecdh_state.ecdh_pub_b64, "correct horse battery staple")
    env["ct"] = base64.b64encode(os.urandom(48)).decode("ascii")
    with TestClient(create_app(ecdh_state)) as tc:
        r = tc.post("/api/auth/login", json={"env": env}, headers=dict(LOCAL))
    assert r.status_code == 400
    assert "envelope" in r.json()["detail"]


def test_login_envelope_with_wrong_password(ecdh_state):
    env = make_envelope(ecdh_state.ecdh_pub_b64, "wrong password")
    with TestClient(create_app(ecdh_state)) as tc:
        r = tc.post("/api/auth/login", json={"env": env}, headers=dict(LOCAL))
    assert r.status_code == 401


def test_status_reports_transport_fields(ecdh_state):
    with TestClient(create_app(ecdh_state)) as tc:
        r_local = tc.get("/api/auth/status", headers=dict(LOCAL))
        r_remote = tc.get("/api/auth/status", headers=dict(REMOTE))
        r_https = tc.get("/api/auth/status",
                         headers={"x-forwarded-for": "203.0.113.7",
                                  "x-forwarded-proto": "https"})
    assert r_local.json()["transport_secure"] is True
    assert r_remote.json()["transport_secure"] is False
    assert r_https.json()["transport_secure"] is True
    for r in (r_local, r_remote, r_https):
        assert r.json()["transport_policy"] == "warn"
        assert r.json()["envelope_encryption"] is True


def test_block_mode_refuses_remote_plain_http(ecdh_state):
    ecdh_state.settings.require_secure_transport = "block"
    with TestClient(create_app(ecdh_state)) as tc:
        r_login = tc.post("/api/auth/login",
                          json={"password": "correct horse battery staple"},
                          headers=dict(REMOTE))
        r_health = tc.get("/api/health", headers=dict(REMOTE))
        r_status = tc.get("/api/auth/status", headers=dict(REMOTE))
    assert r_login.status_code == 426
    assert r_health.status_code == 200
    assert r_status.status_code == 200
    assert r_status.json()["transport_secure"] is False


def test_block_mode_allows_local(ecdh_state):
    ecdh_state.settings.require_secure_transport = "block"
    with TestClient(create_app(ecdh_state)) as tc:
        r = tc.post("/api/auth/login",
                    json={"password": "correct horse battery staple"},
                    headers=dict(LOCAL))
    assert r.status_code == 200


def test_block_mode_allows_remote_https_via_trusted_proxy(ecdh_state):
    ecdh_state.settings.require_secure_transport = "block"
    with TestClient(create_app(ecdh_state)) as tc:
        # testclient is a trusted proxy (conftest env); https proto honored
        r = tc.post("/api/auth/login",
                    json={"password": "correct horse battery staple"},
                    headers={"x-forwarded-for": "203.0.113.7",
                             "x-forwarded-proto": "https"})
    assert r.status_code == 200


def test_warn_mode_lets_remote_login_through(ecdh_state):
    with TestClient(create_app(ecdh_state)) as tc:
        r = tc.post("/api/auth/login",
                    json={"password": "correct horse battery staple"},
                    headers=dict(REMOTE))
    assert r.status_code == 200


def test_unlock_with_envelope(ecdh_state):
    env = make_envelope(ecdh_state.ecdh_pub_b64, "test-passphrase")
    with TestClient(create_app(ecdh_state)) as tc:
        r_login = tc.post("/api/auth/login",
                          json={"password": "correct horse battery staple"},
                          headers=dict(LOCAL))
        csrf = r_login.cookies.get("b3_csrf")
        r_unlock = tc.post("/api/wallet/unlock", json={"env": env},
                           headers={**dict(LOCAL), "x-csrf-token": csrf})
    assert r_login.status_code == 200
    assert r_unlock.status_code == 200
    assert ecdh_state.rpc.called("walletpassphrase")
    call = [c for c in ecdh_state.rpc.calls if c[0] == "walletpassphrase"][0]
    assert call[1][0] == "test-passphrase"


def test_spoofed_proto_from_untrusted_peer_ignored(ecdh_state, monkeypatch):
    # An untrusted direct peer cannot claim https via X-Forwarded-Proto.
    monkeypatch.setenv("B3_TRUSTED_PROXIES", "")
    ecdh_state.settings.require_secure_transport = "block"
    import importlib
    import app.session as session_mod
    importlib.reload(session_mod)
    try:
        with TestClient(create_app(ecdh_state)) as tc:
            r = tc.post("/api/auth/login",
                        json={"password": "correct horse battery staple"},
                        headers={"x-forwarded-for": "203.0.113.7",
                                 "x-forwarded-proto": "https"})
        assert r.status_code == 426
    finally:
        monkeypatch.undo()
        importlib.reload(session_mod)
