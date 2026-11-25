"""Hashing, random tokens and AES-256-GCM for stored OAuth refresh tokens.

Refresh tokens are encrypted by the app (contracts decision 7). The stored
value is ``12-byte nonce || ciphertext || 16-byte tag`` and the key id goes in
``refresh_token_key_id``. The associated data binds a ciphertext to its
workspace and connection, so a ciphertext copied to another row will not
decrypt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class DecryptError(Exception):
    pass


def sha256(value: str | bytes) -> bytes:
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(value).digest()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def random_token(nbytes: int = 32) -> str:
    """32 random bytes → 43 base64url characters."""
    return b64url(secrets.token_bytes(nbytes))


def equal(a: bytes | None, b: bytes | None) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(bytes(a), bytes(b))


def _aad(workspace_id: UUID | str, connection_id: UUID | str) -> bytes:
    return f"riskpredict:connection:{workspace_id}:{connection_id}".encode()


def encrypt_token(
    plaintext: str, keys: tuple[tuple[str, bytes], ...], workspace_id: UUID | str, connection_id: UUID | str
) -> tuple[bytes, str]:
    if not keys:
        raise DecryptError("TOKEN_ENCRYPTION_KEYS is not set")
    key_id, key = keys[0]
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), _aad(workspace_id, connection_id))
    return nonce + ct, key_id


def decrypt_token(
    blob: bytes, key_id: str, keys: tuple[tuple[str, bytes], ...], workspace_id: UUID | str, connection_id: UUID | str
) -> str:
    key = dict(keys).get(key_id)
    if key is None:
        raise DecryptError(f"no key {key_id} in TOKEN_ENCRYPTION_KEYS")
    blob = bytes(blob)
    if len(blob) < 12 + 16:
        raise DecryptError("ciphertext too short")
    try:
        return AESGCM(key).decrypt(blob[:12], blob[12:], _aad(workspace_id, connection_id)).decode()
    except InvalidTag:
        raise DecryptError("token does not decrypt") from None


def derive(secret: bytes, label: str, data: str) -> str:
    """A value derived from a server secret (e.g. the PKCE verifier from the OAuth state)."""
    return b64url(hmac.new(secret, f"{label}:{data}".encode(), hashlib.sha256).digest())
