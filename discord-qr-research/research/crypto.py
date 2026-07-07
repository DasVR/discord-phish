"""RSA-2048 key operations for Discord remote auth protocol analysis."""

from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey

logger = logging.getLogger(__name__)


@dataclass
class KeyPair:
    """RSA-2048 keypair used for a single remote auth session."""

    private_key: RSAPrivateKey
    public_key: RSAPublicKey

    def encoded_public_key(self) -> str:
        """Export SPKI DER as base64 (Discord init payload format)."""
        der = self.public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return base64.b64encode(der).decode("ascii")

    def decrypt_oaep(self, encrypted_b64: str) -> bytes:
        """Decrypt a base64 RSA-OAEP (SHA-256) payload from the gateway."""
        try:
            ciphertext = base64.b64decode(encrypted_b64)
            plaintext = self.private_key.decrypt(
                ciphertext,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=None,
                ),
            )
            return plaintext
        except Exception as exc:
            logger.exception("Failed to decrypt OAEP payload")
            raise ValueError("Unable to decrypt encrypted payload") from exc


def generate_keypair() -> KeyPair:
    """Generate a fresh 2048-bit RSA keypair for remote auth."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    logger.debug("Generated RSA-2048 keypair")
    return KeyPair(private_key=private_key, public_key=public_key)


def build_nonce_proof(decrypted_nonce: bytes) -> str:
    """
    Build the client nonce_proof value.

    Discord expects SHA-256(decrypted_nonce) encoded as URL-safe base64 without padding.
    """
    digest = hashlib.sha256(decrypted_nonce).digest()
    proof = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return proof
