"""HMAC signing utilities for exchange API authentication."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from datetime import datetime, timezone


def hmac_sha256_hex(secret: str, message: str) -> str:
    """HMAC-SHA256, return hex digest. Used by Binance, Bybit, MEXC."""
    return hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def hmac_sha256_base64(secret: str, message: str) -> str:
    """HMAC-SHA256, return base64 digest. Used by OKX."""
    signature = hmac.new(
        secret.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(signature).decode("utf-8")


def timestamp_ms() -> int:
    """Current UTC timestamp in milliseconds."""
    return int(time.time() * 1000)


def timestamp_iso() -> str:
    """Current UTC timestamp in ISO format (for OKX)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
