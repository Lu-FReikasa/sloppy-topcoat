"""Secrets the hub must be able to use again (a token for a repository), kept encrypted at rest.

A key file next to the hub's CA (``tls_dir/secrets.key``, mode 0600, created on first use) encrypts
each value with Fernet. This is not a vault: whoever can read the key file and the database can read
the secrets. It keeps a database dump or a backup from carrying tokens in clear text. Losing the
key file means adding the secrets again; nothing else breaks.
"""

import hashlib
import hmac
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEY_FILE = "secrets.key"


class SecretError(Exception):
    """A stored secret cannot be read (wrong or lost key)."""


class SecretBox:
    def __init__(self, directory: Path) -> None:
        self._path = directory / KEY_FILE
        self._fernet: Fernet | None = None
        self._key: bytes = b""

    def _load(self) -> Fernet:
        if self._fernet is None:
            if self._path.exists():
                key = self._path.read_bytes().strip()
            else:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                key = Fernet.generate_key()
                # mode 0600 from the first byte, never world-readable even for a moment
                import os

                fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as f:
                    f.write(key)
            self._key = key
            self._fernet = Fernet(key)
        return self._fernet

    def sign(self, payload: str, length: int = 10) -> str:
        """A short keyed hash (hex) of ``payload``: nobody without the key file can make one, so
        a text that carries it was handed out by this hub."""
        self._load()
        mac_key = hashlib.sha256(b"topcoat-signature|" + self._key).digest()
        return hmac.new(mac_key, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:length]

    def encrypt(self, plain: str) -> str:
        return self._load().encrypt(plain.encode("utf-8")).decode("ascii")

    def decrypt(self, sealed: str) -> str:
        try:
            return self._load().decrypt(sealed.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise SecretError("the stored secret cannot be decrypted with this hub's key") from exc
