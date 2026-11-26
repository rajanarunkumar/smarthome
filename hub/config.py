"""Data directory and settings store.

Everything private (the hub access token, brand keys and passwords) lives in
~/.smarthome, outside the git repo, readable only by the current user.

Secrets is an in-memory store that is written through to disk: reads come from
memory, and every change is saved to secrets.json straight away, so a restart,
crash or power cut keeps it. A save is crash-safe (written to a temp file,
flushed to disk, then swapped in), the previous version is kept as
secrets.json.bak, and if the file is ever unreadable the backup is loaded. A
change made to the file by something else is picked up and kept instead of
being overwritten.
"""

from __future__ import annotations

import json
import logging
import os
import secrets as pysecrets
import shutil
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


def data_dir() -> Path:
    path = Path(os.environ.get("SMARTHOME_HOME", Path.home() / ".smarthome"))
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:  # not every filesystem allows it; the file itself is already flushed
        pass


class Secrets:
    """Settings held in memory and persisted to ~/.smarthome/secrets.json (owner-only)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / "secrets.json"
        self.backup = self.path.with_name(self.path.name + ".bak")
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {}
        self._seen = None  # (mtime_ns, size) of the file as last read or written
        self._load()

    # Reading
    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _stat(self):
        try:
            st = self.path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self) -> None:
        with self._lock:
            if self.path.exists():
                data = self._read(self.path)
                if data is None:
                    log.warning("%s is unreadable; using %s", self.path.name, self.backup.name)
                    data = self._read(self.backup)
                    if data is None:
                        raise RuntimeError(
                            f"{self.path} and {self.backup} are both unreadable. "
                            "Fix or move them aside; the hub won't start with an empty store and overwrite your settings.")
                self._data = data
            self._seen = self._stat()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(k for k, v in self._data.items() if v is not None)

    # Writing
    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if self._stat() != self._seen:  # the file was changed by something else: keep those changes
                data = self._read(self.path)
                if data is not None:
                    self._data = data
            self._data[key] = value
            self._save()

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if self.path.exists() and self._read(self.path) is not None:
            shutil.copy2(self.path, self.backup)  # the last good version
            os.chmod(self.backup, 0o600)
        os.replace(tmp, self.path)
        _fsync_dir(self.path.parent)
        self._seen = self._stat()

    def access_token(self) -> str:
        """The token the web app must send. Created on first run."""
        token = self.get("access_token")
        if not token:
            token = pysecrets.token_urlsafe(24)
            self.set("access_token", token)
        return token
