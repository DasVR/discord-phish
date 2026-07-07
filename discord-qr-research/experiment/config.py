"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent


class ConfigurationError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigurationError(f"{name} is required")
    return value


def _parse_admin_ids() -> list[int]:
    raw = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_USER_IDS", "")
    if not raw.strip():
        raise ConfigurationError("ADMIN_IDS must contain at least one comma-separated user ID")

    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise ConfigurationError(f"Invalid admin ID in ADMIN_IDS: {part!r}")
        ids.append(int(part))

    if not ids:
        raise ConfigurationError("ADMIN_IDS must contain at least one comma-separated user ID")
    return ids


def _parse_positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the remote auth research toolkit."""

    discord_bot_token: str
    admin_ids: list[int]
    webhook_url: str
    session_timeout: int = 300
    qr_refresh: int = 120
    embed_refresh: int = 5

    qr_server_host: str = "0.0.0.0"
    qr_server_port: int = 8080
    public_base_url: str = "http://localhost:8080"
    remote_auth_gateway: str = "wss://remote-auth-gateway.discord.gg/?v=2"
    discord_api_base: str = "https://discord.com/api/v9"
    log_level: str = "INFO"
    output_dir: Path = field(default_factory=lambda: BASE_DIR / "output")
    state_file: Path = field(default_factory=lambda: BASE_DIR / "state.json")

    @property
    def admin_user_ids(self) -> frozenset[int]:
        """Backward-compatible alias used by command modules."""
        return frozenset(self.admin_ids)

    @property
    def session_ttl_seconds(self) -> int:
        """Backward-compatible alias used by protocol and session code."""
        return self.session_timeout

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            discord_bot_token=_require("DISCORD_BOT_TOKEN"),
            admin_ids=_parse_admin_ids(),
            webhook_url=os.getenv("WEBHOOK_URL", "").strip(),
            session_timeout=_parse_positive_int("SESSION_TIMEOUT", 300),
            qr_refresh=_parse_positive_int("QR_REFRESH", 120),
            embed_refresh=_parse_positive_int("EMBED_REFRESH", 5),
            qr_server_host=os.getenv("QR_SERVER_HOST", "0.0.0.0").strip() or "0.0.0.0",
            qr_server_port=int(os.getenv("QR_SERVER_PORT", "8080")),
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").strip()
            or "http://localhost:8080",
            remote_auth_gateway=os.getenv(
                "REMOTE_AUTH_GATEWAY", "wss://remote-auth-gateway.discord.gg/?v=2"
            ).strip()
            or "wss://remote-auth-gateway.discord.gg/?v=2",
            discord_api_base=os.getenv("DISCORD_API_BASE", "https://discord.com/api/v9").strip()
            or "https://discord.com/api/v9",
            log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )


def load_settings() -> Settings:
    """Load and validate settings from the environment."""
    return Settings.from_env()


settings = load_settings()
