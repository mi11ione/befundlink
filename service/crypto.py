"""At-rest encryption for held uploads (AES-256-GCM via `cryptography`, a project dependency).
Mandatory: the service refuses to start without it."""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from .store import write_private

log = logging.getLogger("pgk.crypto")

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore
except ImportError:  # pragma: no cover
    AESGCM = None


class Box:
    def __init__(self, key_hex: str | None, key_file: Path):
        self.aes = None
        if AESGCM is None:
            raise RuntimeError("`cryptography` is required for encryption at rest of held uploads (uv sync)")
        if key_hex:
            key = bytes.fromhex(key_hex)
        elif key_file.exists():
            key = bytes.fromhex(key_file.read_text().strip())
        else:
            key = secrets.token_bytes(32)
            write_private(key_file, key.hex().encode())  # 0600 from creation on, in a 0700 directory
        self.aes = AESGCM(key)

    @property
    def enabled(self) -> bool:
        return self.aes is not None

    def seal(self, plaintext: str, aad: str) -> tuple[bytes, bool]:
        if not self.aes:
            return plaintext.encode(), False
        nonce = secrets.token_bytes(12)
        return nonce + self.aes.encrypt(nonce, plaintext.encode(), aad.encode()), True

    def open(self, blob: bytes, encrypted: bool, aad: str) -> str:
        if not encrypted:
            return bytes(blob).decode()
        blob = bytes(blob)
        return self.aes.decrypt(blob[:12], blob[12:], aad.encode()).decode()
