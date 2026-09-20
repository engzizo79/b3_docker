"""ECDH (P-256) + HKDF-SHA256 + AES-GCM envelope encryption for secrets in
transit (login password, wallet passphrase), WebCrypto-compatible.

Why: over plain HTTP the password and passphrase are readable on the wire.
With this envelope, the cleartext secret never crosses the wire even on
plain HTTP — only the ECDH-shared, AES-GCM-encrypted ciphertext does. An
active MITM can still swap page code (hence the transport banner/block),
but a *passive* observer sees only ciphertext, and replay is bounded by
the single-use iv + the rate limiter.

Interop:
  - Curve: P-256 (secp256r1), WebCrypto namedCurve "P-256".
  - Point format: raw uncompressed 0x04 || x || y (65 bytes), base64.
  - Shared secret: the x-coordinate of ECDH (32 bytes) — what both
    Python cryptography `.exchange()` and WebCrypto `deriveBits()` yield.
  - KDF: HKDF-SHA256, 32 bytes, salt + info from the request.
  - Cipher: AES-GCM 256, 12-byte iv, 16-byte tag appended (WebCrypto style).

Server key: a long-lived P-256 keypair persisted to <data>/.ecdh_key.pem
(0600). Rotating it just forces clients to re-fetch the public key.
"""

from __future__ import annotations

import base64
import json
import os
import pathlib

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_CURVE = ec.SECP256R1()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _ub64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def server_keypair(data_dir: str) -> tuple[ec.EllipticCurvePrivateKey, str]:
    """Load or create the server's long-lived P-256 keypair.
    Returns (private_key, public_key_raw_b64)."""
    keyfile = pathlib.Path(data_dir) / ".ecdh_key.pem"
    if keyfile.exists():
        with open(keyfile, "rb") as f:
            priv = serialization.load_pem_private_key(f.read(), password=None)
    else:
        priv = ec.generate_private_key(_CURVE)
        pem = priv.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        with open(keyfile, "wb") as f:
            f.write(pem)
        os.chmod(keyfile, 0o600)
    pub_raw = priv.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return priv, _b64(pub_raw)


def derive_key(priv: ec.EllipticCurvePrivateKey, client_pub_b64: str,
                salt: bytes, info: bytes) -> bytes:
    """ECDH + HKDF -> 32-byte AES-GCM key."""
    client_pub = ec.EllipticCurvePublicKey.from_encoded_point(
        _CURVE, _ub64(client_pub_b64))
    shared = priv.exchange(ec.ECDH(), client_pub)  # 32-byte x-coordinate
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info
                ).derive(shared)


def decrypt_envelope(priv: ec.EllipticCurvePrivateKey, env: dict) -> str:
    """Decrypt a {client_pub, iv, ct, salt, info} envelope -> plaintext str."""
    key = derive_key(priv, env["client_pub"], _ub64(env["salt"]), _ub64(env["info"]))
    iv = _ub64(env["iv"])
    ct = _ub64(env["ct"])
    pt = AESGCM(key).decrypt(iv, ct, None)
    return pt.decode("utf-8")


def parse_envelope_body(body: dict, field: str = "password") -> str:
    """Extract a secret from a request body: encrypted envelope if present,
    else a plain (legacy) value. Caller decides whether plain is acceptable
    for the transport context."""
    env = body.get("env")
    if isinstance(env, dict) and {"client_pub", "iv", "ct"} <= env.keys():
        return env  # caller decrypts with the server key
    return body.get(field, "")
