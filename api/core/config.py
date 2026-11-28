"""Settings from environment variables.

Nothing here is ever logged. ``Settings.describe()`` lists which optional
integrations are configured (names only, never values).
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass, field


class ConfigError(Exception):
    """Safe to print: names the variable, never its value."""


def _b64(value: str) -> bytes:
    v = value.strip()
    pad = "=" * (-len(v) % 4)
    try:
        return base64.urlsafe_b64decode(v.replace("+", "-").replace("/", "_") + pad)
    except (binascii.Error, ValueError):
        raise ConfigError("not valid base64") from None


def parse_key_ring(value: str) -> tuple[tuple[str, bytes], ...]:
    """``k1:<base64 32 bytes>[,k2:...]``. The first entry encrypts, all decrypt."""
    ring: list[tuple[str, bytes]] = []
    for part in (p.strip() for p in value.split(",")):
        if not part:
            continue
        key_id, sep, material = part.partition(":")
        if not sep or not key_id or len(key_id) > 40:
            raise ConfigError("TOKEN_ENCRYPTION_KEYS entries must look like <id>:<base64 key>")
        try:
            key = _b64(material)
        except ConfigError:
            raise ConfigError(f"TOKEN_ENCRYPTION_KEYS entry {key_id} is not valid base64") from None
        if len(key) != 32:
            raise ConfigError(f"TOKEN_ENCRYPTION_KEYS entry {key_id} must decode to 32 bytes")
        if any(k == key_id for k, _ in ring):
            raise ConfigError(f"TOKEN_ENCRYPTION_KEYS has {key_id} twice")
        ring.append((key_id, key))
    return tuple(ring)


def parse_pairs(value: str) -> dict[str, str]:
    """``airtable:ac_123,notion:ac_456`` → ``{"airtable": "ac_123", ...}``."""
    out: dict[str, str] = {}
    for part in (p.strip() for p in value.split(",")):
        if not part:
            continue
        k, sep, v = part.partition(":")
        if sep and k.strip() and v.strip():
            out[k.strip().lower()] = v.strip()
    return out


# Groq's free-plan models (Llama was retired for free accounts). Used when the variables
# are unset; set GROQ_MAPPING_MODEL / GROQ_ADVICE_MODEL to override.
DEFAULT_GROQ_MAPPING_MODEL = "openai/gpt-oss-120b"
DEFAULT_GROQ_ADVICE_MODEL = "openai/gpt-oss-20b"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None


@dataclass(frozen=True)
class Settings:
    database_url: str = ""
    jwt_signing_key: bytes = b""
    token_keys: tuple[tuple[str, bytes], ...] = ()
    web_origin: str = ""
    public_api_url: str = ""
    # The request header that holds the client's address, set by the platform's edge and
    # not forgeable by the client (Vercel: x-real-ip). Empty: the socket peer address.
    client_ip_header: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_picker_api_key: str = ""
    google_project_number: str = ""
    composio_api_key: str = ""
    composio_webhook_secret: str = ""
    composio_auth_configs: dict[str, str] = field(default_factory=dict)
    composio_trigger_slugs: dict[str, str] = field(default_factory=dict)
    composio_base_url: str = "https://backend.composio.dev/api/v3.1"
    groq_api_key: str = ""
    groq_mapping_model: str = ""
    groq_advice_model: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    expected_schema_version: str = "060_advice_version"
    db_max_connections: int = 5
    db_max_idle_seconds: int = 60
    # work done inside a request (serverless: no background loop); Vercel stops at 300 s
    inline_budget_seconds: int = 240
    webhook_budget_seconds: int = 20
    # worker timing (the loop for docker compose; `--once` for GitHub Actions)
    worker_db_interval_seconds: int = 1800
    source_recheck_seconds: int = 900
    worker_once_budget_seconds: int = 480
    advice_per_run: int = 20

    @classmethod
    def from_env(cls) -> Settings:
        jwt_raw = _env("JWT_SIGNING_KEY")
        jwt_key = b""
        if jwt_raw:
            try:
                jwt_key = _b64(jwt_raw)
            except ConfigError:
                raise ConfigError("JWT_SIGNING_KEY must be base64") from None
            if len(jwt_key) < 32:
                raise ConfigError("JWT_SIGNING_KEY must decode to at least 32 bytes")
        return cls(
            database_url=_env("DATABASE_URL_APP"),
            jwt_signing_key=jwt_key,
            token_keys=parse_key_ring(_env("TOKEN_ENCRYPTION_KEYS")),
            web_origin=_env("WEB_ORIGIN").rstrip("/"),
            public_api_url=_env("PUBLIC_API_URL").rstrip("/"),
            client_ip_header=_env("CLIENT_IP_HEADER").lower(),
            google_client_id=_env("GOOGLE_OAUTH_CLIENT_ID"),
            google_client_secret=_env("GOOGLE_OAUTH_CLIENT_SECRET"),
            google_picker_api_key=_env("GOOGLE_PICKER_API_KEY"),
            google_project_number=_env("GOOGLE_CLOUD_PROJECT_NUMBER"),
            composio_api_key=_env("COMPOSIO_API_KEY"),
            composio_webhook_secret=_env("COMPOSIO_WEBHOOK_SECRET"),
            composio_auth_configs=parse_pairs(_env("COMPOSIO_AUTH_CONFIGS")),
            composio_trigger_slugs=parse_pairs(_env("COMPOSIO_TRIGGER_SLUGS")),
            groq_api_key=_env("GROQ_API_KEY"),
            groq_mapping_model=_env("GROQ_MAPPING_MODEL") or DEFAULT_GROQ_MAPPING_MODEL,
            groq_advice_model=_env("GROQ_ADVICE_MODEL") or DEFAULT_GROQ_ADVICE_MODEL,
            expected_schema_version=_env("EXPECTED_SCHEMA_VERSION", "060_advice_version"),
            db_max_connections=_int("DB_MAX_CONNECTIONS", 5),
            inline_budget_seconds=_int("INLINE_BUDGET_SECONDS", 240),
            webhook_budget_seconds=_int("WEBHOOK_BUDGET_SECONDS", 20),
            worker_db_interval_seconds=_int("WORKER_DB_INTERVAL_SECONDS", 1800),
            source_recheck_seconds=_int("SOURCE_RECHECK_SECONDS", 900),
            worker_once_budget_seconds=_int("WORKER_ONCE_BUDGET_SECONDS", 480),
            advice_per_run=_int("ADVICE_PER_RUN", 20),
        )

    # --- what is configured -------------------------------------------------------
    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.token_keys)

    @property
    def composio_enabled(self) -> bool:
        return bool(self.composio_api_key)

    @property
    def drive_watch_enabled(self) -> bool:
        # Google only delivers push notifications to an HTTPS address.
        return self.public_api_url.startswith("https://")

    def describe(self) -> dict[str, bool]:
        """Which integrations are on. Names only; safe to log."""
        return {
            "database": bool(self.database_url),
            "jwt": bool(self.jwt_signing_key),
            "token_encryption": bool(self.token_keys),
            "google": self.google_enabled,
            "drive_watch": self.drive_watch_enabled,
            "composio": self.composio_enabled,
            "groq_mapping": bool(self.groq_api_key and self.groq_mapping_model),
            "groq_advice": bool(self.groq_api_key and self.groq_advice_model),
        }
