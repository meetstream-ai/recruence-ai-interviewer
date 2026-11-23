"""Encryption at rest for keys and passwords saved from the Settings page.

Fernet (AES-128-CBC with HMAC-SHA256, from the `cryptography` package). The key comes from
SETTINGS_ENCRYPTION_KEY in .env, or, when that is empty, from a key file generated once in
DATA_DIR (`settings.key`), kept apart from the database. Losing the key means re-entering the
saved secrets; it never exposes them.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

KEY_FILE = "settings.key"


class SecretError(RuntimeError):
    pass


class SecretBox:
    def __init__(self, key: bytes, source: str):
        self._fernet = Fernet(key)
        self.source = source  # "SETTINGS_ENCRYPTION_KEY" or the key file's path, for the Settings page

    def encrypt(self, text: str) -> str:
        return self._fernet.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise SecretError("A saved secret can't be decrypted with the current encryption key") from e


def load_box(data_dir: Path, env_key: str = "") -> SecretBox:
    if env_key:
        try:
            return SecretBox(env_key.encode(), "SETTINGS_ENCRYPTION_KEY")
        except ValueError as e:
            raise SecretError("SETTINGS_ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes)") from e
    path = data_dir / KEY_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Fernet.generate_key())
        try:
            os.chmod(path, 0o600)  # owner only; best effort on Windows
        except OSError:
            pass
        logger.info("generated an encryption key for saved settings at %s", path)
    return SecretBox(path.read_bytes().strip(), str(path))
