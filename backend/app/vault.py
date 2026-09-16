"""Encrypted-at-rest wallet passphrase vault for unattended staking (S5).

Opt-in only. The passphrase is Fernet-encrypted with a vault key sourced
from the WALLET_VAULT_KEY env var (operator-managed: secrets sidecar or
read-only removable media) or, when absent, a generated key persisted to
<data>/.vault.key (0400). The stored passphrase is ONLY ever used for the
authorized unattended use cases — startstaking at startup, createstake
top-ups, consolidation sweeps — each within a short unlock window
followed by an immediate walletlock. It is NEVER used for sends,
unstaking, exports, or any interactive action (those require the
interactive unlock flow). Every auto-unlock is audit-logged.

Honest security caveat (surfaced in the UI when enabling): for the wallet
to unlock itself, the passphrase must be recoverable on this machine.
The env-var vault key is the recommended path for real funds; the
generated key file is convenient but an attacker with read access to the
data directory can recover the passphrase.
"""

import base64
import hashlib
import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

VAULT_KEY_FILE = ".vault.key"


def _key_bytes(raw: str) -> bytes:
 """Deterministic Fernet key from an arbitrary operator passphrase."""
 return base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest())


def _load_or_create_key(data_dir: Path, env_key: str | None) -> bytes:
 """Vault key source priority: WALLET_VAULT_KEY env var, then generated
 <data_dir>/.vault.key (0400). Never writes the key when env-provided."""
 if env_key:
  return _key_bytes(env_key)
 key_file = Path(data_dir) / VAULT_KEY_FILE
 if key_file.exists():
  raw = key_file.read_bytes().strip()
  if len(raw) == 44: # standard Fernet key
   return raw
  return _key_bytes(raw.decode("utf-8", "replace"))
 raw = Fernet.generate_key() # bytes, urlsafe-b64 (len 44)
 key_file.parent.mkdir(parents=True, exist_ok=True)
 key_file.write_bytes(raw)
 os.chmod(key_file, 0o400)
 logger.warning("vault: generated key file %s (operator-managed WALLET_VAULT_KEY is the recommended source)", key_file)
 return raw


class Vault:
 """Fernet-encrypted passphrase storage, persisted as a DB blob by the
 caller (staking settings row)."""

 def __init__(self, key: bytes) -> None:
  self._f = Fernet(key)

 def encrypt(self, passphrase: str) -> str:
  return self._f.encrypt(passphrase.encode("utf-8")).decode("ascii")

 def decrypt(self, blob: str) -> str | None:
  """None on any failure (wrong key, corrupted blob) — never raises."""
  try:
   return self._f.decrypt(blob.encode("ascii")).decode("utf-8")
  except (InvalidToken, ValueError, TypeError):
   return None

 def fingerprint(self, blob: str) -> str:
  """Short non-reversible fingerprint so the UI can show WHICH passphrase
  is stored without revealing anything about it."""
  return hashlib.sha256(blob.encode("ascii")).hexdigest()[:16]


def vault_from_settings(settings, data_dir: Path | str) -> Vault | None:
 """Build a Vault from app settings; None only if key generation is
 impossible (unwritable data dir) — in that case unattended mode is off."""
 try:
  key = _load_or_create_key(Path(data_dir), getattr(settings, "wallet_vault_key", None))
  return Vault(key)
 except OSError:
  logger.error("vault: cannot create key file in %s — unattended staking unavailable", data_dir)
  return None