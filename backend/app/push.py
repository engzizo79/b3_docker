"""Web Push: VAPID key management and payload delivery.

Follows the same key-file pattern as app/vault.py's unattended-staking key:
generate once into <data_dir>/.vapid_private.pem (0400), never regenerate
(regenerating would silently invalidate every subscribed device — a
push service ties a subscription to the public key it was created with).

webpush() itself is synchronous (it uses `requests` under the hood); callers
in an async context MUST run send() via asyncio.to_thread, same as any other
blocking call in this codebase.
"""

import base64
import logging
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)

VAPID_KEY_FILE = ".vapid_private.pem"


def _key_path(data_dir: str) -> Path:
    return Path(data_dir) / VAPID_KEY_FILE


def _load_or_create(data_dir: str) -> Vapid:
    path = _key_path(data_dir)
    if path.is_file():
        return Vapid.from_file(str(path))
    v = Vapid()
    v.generate_keys()
    path.parent.mkdir(parents=True, exist_ok=True)
    v.save_key(str(path))
    os.chmod(path, 0o400)
    logger.info("push: generated VAPID key file %s", path)
    return v


def public_key_b64url(data_dir: str) -> str:
    """The public key as an uncompressed-point base64url string — exactly
    the shape PushManager.subscribe()'s applicationServerKey expects."""
    v = _load_or_create(data_dir)
    raw = v.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def send(data_dir: str, subject: str, subscription: dict, payload: dict) -> str:
    """Send one push message. Returns "ok", "expired" (subscription is dead
    - caller should delete it), or "error" (transient - leave it alone).
    Synchronous; call via asyncio.to_thread from async code."""
    import json
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps(payload),
            vapid_private_key=str(_key_path(data_dir)),
            vapid_claims={"sub": subject},
            timeout=10,
        )
        return "ok"
    except WebPushException as exc:
        status = exc.status_code
        if status in (404, 410):
            logger.info("push: subscription gone (status=%s), will be removed", status)
            return "expired"
        logger.warning("push: send failed (status=%s): %s", status, exc)
        return "error"
    except Exception as exc:  # network errors, etc. - never crash the caller
        logger.warning("push: send failed: %s", exc)
        return "error"
