"""QR code generation from remote auth fingerprints."""

from __future__ import annotations

import io
import logging
from pathlib import Path

import qrcode
from qrcode.constants import ERROR_CORRECT_M

logger = logging.getLogger(__name__)

DISCORD_RA_BASE = "https://discord.com/ra"


def fingerprint_url(fingerprint: str) -> str:
    """Build the Discord remote auth URL embedded in login QR codes."""
    return f"{DISCORD_RA_BASE}/{fingerprint}"


def generate_qr_bytes(fingerprint: str, box_size: int = 8, border: int = 2) -> bytes:
    """Render a PNG QR code for the given remote auth fingerprint."""
    url = fingerprint_url(fingerprint)
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(url)
    qr.make(fit=True)

    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    png_bytes = buffer.getvalue()
    logger.debug("Generated QR PNG (%d bytes) for fingerprint prefix %s", len(png_bytes), fingerprint[:8])
    return png_bytes


def save_qr_png(fingerprint: str, output_path: Path) -> Path:
    """Write a QR PNG to disk for offline analysis."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(generate_qr_bytes(fingerprint))
    logger.info("Saved QR image to %s", output_path)
    return output_path
