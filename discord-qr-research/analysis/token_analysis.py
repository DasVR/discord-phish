"""Post-capture authentication token analysis utilities."""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

TOKEN_PATTERN = re.compile(r"^[\w-]{20,}\.[\w-]{4,}\.[\w-]{20,}$")


@dataclass
class TokenAnalysis:
    """Structured breakdown of a captured Discord user token."""

    raw: str
    valid_format: bool
    segment_count: int
    segments: list[str]
    estimated_user_id: str | None
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid_format": self.valid_format,
            "segment_count": self.segment_count,
            "segments": self.segments,
            "estimated_user_id": self.estimated_user_id,
            "notes": self.notes,
            "token_length": len(self.raw),
        }


def _try_decode_user_id(first_segment: str) -> str | None:
    """Attempt to decode the snowflake user ID from the first token segment."""
    try:
        padded = first_segment + "=" * (-len(first_segment) % 4)
        decoded = base64.b64decode(padded.replace("-", "+").replace("_", "/"))
        candidate = decoded.decode("utf-8", errors="ignore").strip()
        if candidate.isdigit():
            return candidate
    except Exception:
        pass

    if first_segment.isdigit():
        return first_segment

    return None


def analyze_token(token: str) -> TokenAnalysis:
    """
    Analyze token structure for authorized security research.

    Does not validate the token against Discord — only inspects format/segments.
    """
    cleaned = token.strip().strip('"')
    segments = cleaned.split(".")
    valid_format = bool(TOKEN_PATTERN.match(cleaned))
    notes: list[str] = []

    if not valid_format:
        notes.append("Token does not match the common three-segment Discord format")
    if len(cleaned) < 50:
        notes.append("Token appears unusually short")
    if any(ch.isspace() for ch in cleaned):
        notes.append("Token contains whitespace")

    user_id = _try_decode_user_id(segments[0]) if segments else None
    if user_id:
        notes.append(f"First segment decodes to snowflake user ID {user_id}")
    else:
        notes.append("Could not derive user ID from first segment")

    logger.debug("Analyzed token (%d segments, valid=%s)", len(segments), valid_format)
    return TokenAnalysis(
        raw=cleaned,
        valid_format=valid_format,
        segment_count=len(segments),
        segments=segments,
        estimated_user_id=user_id,
        notes=notes,
    )
