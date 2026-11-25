"""Our own auth (contracts decision 6).

* Passwords: argon2id, m=19456 KiB, t=2, p=1 (PHC string in ``biz.users``).
* Access token: HS256 JWT, 15 minutes; claims ``sub``, ``sid``, ``iat``, ``exp``,
  ``iss`` = riskpredict-api, ``aud`` = riskpredict-web.
* Refresh token: 32 random bytes (43 base64url chars), 30 days, stored as
  sha256 only, rotated on every use; presenting a rotated one revokes its family.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from api.core import crypto
from api.core.errors import unauthenticated

ISSUER = "riskpredict-api"
AUDIENCE = "riskpredict-web"
ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=30)

_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, hash_len=32, salt_len=16, type=Type.ID)
# Verified against when the email is unknown, so both paths take the same time.
_DUMMY_HASH = _hasher.hash("riskpredict-dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash or _DUMMY_HASH, password) and stored_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return True


@dataclass(frozen=True)
class AccessClaims:
    user_id: UUID
    session_id: UUID


def issue_access(key: bytes, user_id: UUID, session_id: UUID, now: datetime) -> tuple[str, datetime]:
    exp = now + ACCESS_TTL
    token = jwt.encode(
        {
            "sub": str(user_id),
            "sid": str(session_id),
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "iss": ISSUER,
            "aud": AUDIENCE,
        },
        key,
        algorithm="HS256",
    )
    return token, exp.replace(microsecond=0)


def verify_access(key: bytes, token: str) -> AccessClaims:
    try:
        claims = jwt.decode(
            token, key, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER,
            options={"require": ["sub", "sid", "exp", "iat", "iss", "aud"]},
        )  # fmt: skip
        return AccessClaims(UUID(claims["sub"]), UUID(claims["sid"]))
    except jwt.ExpiredSignatureError:
        raise unauthenticated("the access token expired; refresh it", code="token_expired") from None
    except (jwt.InvalidTokenError, ValueError, KeyError):
        raise unauthenticated("invalid access token") from None


def new_refresh_token() -> tuple[str, bytes]:
    token = crypto.random_token(32)
    return token, crypto.sha256(token)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
