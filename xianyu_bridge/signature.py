from __future__ import annotations

import hashlib
import hmac


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def sign(secret: str, timestamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    canonical = "\n".join((timestamp, nonce, method.upper(), path, body_hash(body)))
    return hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()


def verify(
    secret: str,
    timestamp: str,
    nonce: str,
    method: str,
    path: str,
    body: bytes,
    signature: str,
) -> bool:
    expected = sign(secret, timestamp, nonce, method, path, body)
    return hmac.compare_digest(expected, signature)
