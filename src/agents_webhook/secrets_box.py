"""AES-256-GCM encryption of endpoint secrets at rest (no database access).

Blob layout: 12-byte random nonce, then ciphertext, then the 16-byte GCM tag. The endpoint
id is the AAD, so a ciphertext copied to another row fails to decrypt. The key is 32 bytes,
base64, read from a mounted file; nothing here logs or puts key or plaintext in an error.
"""

import base64
import binascii
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
_ENV = "WEBHOOK_ENDPOINT_KEY_FILE"


class SecretBoxError(Exception):
    """Bad key or a blob that does not authenticate; the message never carries secret data."""


class SecretBox:
    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_BYTES:
            raise SecretBoxError(f"key must be {KEY_BYTES} bytes")
        self._aes = AESGCM(key)
        self.kid = hashlib.sha256(b"agents-webhook-kid:" + key).hexdigest()[:16]
        """Short key fingerprint stored beside each ciphertext, so a later key rotation can
        tell which rows need re-encrypting. It does not reveal the key."""

    @classmethod
    def from_file(cls, path: str) -> "SecretBox":
        """Load the base64 key file; raises SecretBoxError (startup fails fast)."""
        if not path:
            raise SecretBoxError(f"{_ENV} is required")
        try:
            with open(path, "rb") as handle:
                text = handle.read().strip()
        except OSError as exc:
            raise SecretBoxError(f"{_ENV}: file not readable ({type(exc).__name__})") from None
        try:
            key = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError):
            raise SecretBoxError(f"{_ENV}: content is not base64") from None
        if len(key) != KEY_BYTES:
            raise SecretBoxError(f"{_ENV}: key must decode to {KEY_BYTES} bytes")
        return cls(key)

    def encrypt(self, plaintext: str, *, aad: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aes.encrypt(nonce, plaintext.encode(), aad)

    def decrypt(self, blob: bytes, *, aad: bytes) -> str:
        if len(blob) < NONCE_BYTES + TAG_BYTES:
            raise SecretBoxError("ciphertext too short")
        try:
            return self._aes.decrypt(blob[:NONCE_BYTES], blob[NONCE_BYTES:], aad).decode()
        except (InvalidTag, UnicodeDecodeError):
            raise SecretBoxError("ciphertext does not authenticate") from None
