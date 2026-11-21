"""Data directory and secrets.

Everything private (the hub access token, brand keys) lives in ~/.smarthome,
outside the git repo, readable only by the current user.
"""

from __future__ import annotations

import json
import os
import secrets as pysecrets
from pathlib import Path
from typing import Any


def data_dir() -> Path:
    path = Path(os.environ.get("SMARTHOME_HOME", Path.home() / ".smarthome"))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


class Secrets:
    """A small JSON key store written with owner-only permissions."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "secrets.json"
        self._data: dict[str, Any] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text())

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._data, f, indent=2)
        os.replace(tmp, self.path)

    def access_token(self) -> str:
        """The token the web app must send. Created on first run."""
        token = self.get("access_token")
        if not token:
            token = pysecrets.token_urlsafe(24)
            self.set("access_token", token)
        return token
