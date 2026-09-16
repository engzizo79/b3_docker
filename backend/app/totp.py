"""TOTP 2FA: secret generation, Fernet-encrypted storage, verification with
replay protection (one code = one use)."""

import base64
import hashlib
import time

import pyotp
from cryptography.fernet import Fernet, InvalidToken


class TOTPError(Exception):
    pass


def _fernet(key_hex: str) -> Fernet:
    """Derive a Fernet key from the hex env secret (any length, hashed to 32)."""
    digest = hashlib.sha256(key_hex.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def generate_secret() -> str:
    return pyotp.random_base32()


def encrypt_secret(key_hex: str, secret: str) -> str:
    return _fernet(key_hex).encrypt(secret.encode()).decode()


def decrypt_secret(key_hex: str, secret_enc: str) -> str:
    try:
        return _fernet(key_hex).decrypt(secret_enc.encode()).decode()
    except InvalidToken as exc:
        raise TOTPError("TOTP_ENCRYPTION_KEY does not match the stored secret") from exc


def provisioning_uri(secret: str, username: str, issuer: str = "B3 Hive") -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


class TOTPVerifier:
    """Stateful verifier with replay protection: within the valid window a
    code may only be used once (defeats MITM relay within the 30s window)."""

    def __init__(self) -> None:
        self._used: dict[str, int] = {}  # code-hash -> expiry ts

    def verify(self, secret: str, code: str) -> bool:
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=1):
            return False
        key = hashlib.sha256(f"{secret}:{code}".encode()).hexdigest()
        now = time.time()
        # Purge expired entries.
        self._used = {k: exp for k, exp in self._used.items() if exp > now}
        if key in self._used:
            return False  # replay
        self._used[key] = now + 60
        return True


verifier = TOTPVerifier()
