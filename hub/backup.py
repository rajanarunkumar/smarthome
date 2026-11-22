"""Encrypted nightly backups of ~/.smarthome to your own S3 bucket.

What is backed up: a consistent snapshot of hub.db (SQLite's online backup, so
it's safe while the hub is running), secrets.json, and optionally the zone
photos. The archive is encrypted on the Mac with AES-256-GCM before it leaves,
so S3 only ever holds ciphertext. The key is created on first use and kept in
~/.smarthome/backup.key (never inside a backup). Save a copy of it in a
password manager: without it a backup can't be opened, and if the Mac dies the
key file goes with it.

The hub needs only s3:PutObject. It never lists, reads or deletes, so a leaked
hub key can add backups but can't read or destroy the existing ones. Expire
old backups with an S3 lifecycle rule instead of letting the hub delete.

Restore:  python -m hub.backup restore FILE.enc --key-file backup.key --to DIR
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import logging
import os
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import Secrets
from .models import HubError

log = logging.getLogger(__name__)
MAGIC = b"SHB1"
AAD = b"smarthome-backup-v1"
KEY_FILE = "backup.key"
RUN_HOUR, RUN_MINUTE = 3, 30
RETRY_AFTER_FAILURE_MINUTES = 60
ALLOWED_TOP = {"hub.db", "secrets.json", "photos"}


# Key
def key_path(folder: Path) -> Path:
    return folder / KEY_FILE


def load_or_create_key(folder: Path) -> bytes:
    path = key_path(folder)
    if path.exists():
        return base64.b64decode(path.read_text().strip())
    key = AESGCM.generate_key(256)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(key).decode())
        f.flush()
        os.fsync(f.fileno())
    return key


# Archive and encryption
def make_archive(folder: Path, include_photos: bool = True) -> bytes:
    """A tar.gz with a consistent hub.db snapshot, secrets.json and optionally photos/."""
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = Path(tmp) / "hub.db"
        src = sqlite3.connect(folder / "hub.db")
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)  # safe while the hub is writing
        finally:
            dst.close()
            src.close()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(snapshot, arcname="hub.db")
            if (folder / "secrets.json").exists():
                tar.add(folder / "secrets.json", arcname="secrets.json")
            if include_photos and (folder / "photos").exists():
                tar.add(folder / "photos", arcname="photos")
    return buf.getvalue()


def encrypt(data: bytes, key: bytes) -> bytes:
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, AAD)


def decrypt(blob: bytes, key: bytes) -> bytes:
    if blob[:4] != MAGIC or len(blob) < 32:
        raise ValueError("This isn't a smart home hub backup.")
    try:
        return AESGCM(key).decrypt(blob[4:16], blob[16:], AAD)
    except InvalidTag:
        raise ValueError("Wrong key, or the backup was changed after it was made.") from None


def restore(blob: bytes, key: bytes, target: Path, force: bool = False) -> list[str]:
    """Decrypt and unpack into `target`. Refuses a non-empty folder unless `force`; rejects unsafe paths."""
    data = decrypt(blob, key)
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(target.iterdir()) and not force:
        raise ValueError(f"{target} isn't empty. Pick an empty folder, or use --force to overwrite.")
    written = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if (not parts or parts[0] not in ALLOWED_TOP or member.name.startswith("/") or ".." in parts
                    or not (member.isfile() or member.isdir())):
                raise ValueError(f"Unsafe or unexpected item in the backup: {member.name!r}")
        for member in tar.getmembers():
            dest = target / member.name
            if member.isdir():
                dest.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(tar.extractfile(member).read())
            written.append(member.name)
    return written


# S3
def make_client(cfg: dict) -> Any:
    import boto3

    kwargs: dict[str, Any] = {"region_name": cfg["region"]}
    if cfg.get("access_key_id") and cfg.get("secret_access_key"):
        kwargs.update(aws_access_key_id=cfg["access_key_id"], aws_secret_access_key=cfg["secret_access_key"])
    # Otherwise boto3's normal chain: ~/.aws/credentials, SSO, environment.
    return boto3.client("s3", **kwargs)


def explain(exc: Exception, cfg: dict) -> str:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "") if hasattr(exc, "response") else ""
    where = f"s3://{cfg['bucket']}/{cfg.get('prefix', '')}"
    if code in ("AccessDenied", "AllAccessDisabled"):
        return f"Access denied. The AWS key needs s3:PutObject on {where.rstrip('/')}/*."
    if code == "NoSuchBucket":
        return f"The bucket {cfg['bucket']} doesn't exist in {cfg['region']}."
    if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch"):
        return "AWS rejected that access key. Check the key ID and secret."
    if code in ("PermanentRedirect", "AuthorizationHeaderMalformed"):
        return "The bucket is in a different region. Set the region it lives in."
    if type(exc).__name__ == "NoCredentialsError":
        return "No AWS credentials found. Enter an access key here, or run aws configure on the Mac."
    return f"S3 upload failed: {exc}"


def should_run(now: datetime, last_ok: datetime | None, last_attempt: datetime | None) -> bool:
    """Once a day after 03:30; after a failure, try again hourly until it works."""
    if (now.hour, now.minute) < (RUN_HOUR, RUN_MINUTE):
        return False
    if last_ok and last_ok.date() == now.date():
        return False
    if last_attempt and (now - last_attempt).total_seconds() < RETRY_AFTER_FAILURE_MINUTES * 60 \
            and last_attempt.date() == now.date():
        return False
    return True


class Backups:
    def __init__(self, secrets: Secrets, db, folder: Path, client_factory: Callable[[dict], Any] | None = None) -> None:
        self.secrets, self.db, self.folder = secrets, db, folder
        self._client = client_factory
        db.execute("""CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY, at TEXT NOT NULL, ok INTEGER NOT NULL, bytes INTEGER, key TEXT, note TEXT)""")
        db.commit()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def config(self) -> dict:
        return self.secrets.get("s3_backup") or {}

    @property
    def active(self) -> bool:
        return bool(self.config.get("bucket"))

    def history(self, limit: int = 10) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM backups ORDER BY id DESC LIMIT ?", (limit,))]

    def _last(self, ok: bool | None = None) -> datetime | None:
        sql = "SELECT at FROM backups" + ("" if ok is None else f" WHERE ok = {int(ok)}") + " ORDER BY id DESC LIMIT 1"
        row = self.db.execute(sql).fetchone()
        return datetime.fromisoformat(row["at"]) if row else None

    async def run(self, now: datetime | None = None, cfg: dict | None = None, record: bool = True) -> dict:
        """Archive, encrypt and upload one backup. Raises HubError on failure (and records it)."""
        cfg = cfg or self.config
        now = now or datetime.now()
        async with self._lock:
            name = f"{cfg.get('prefix', '').strip('/')}/hub-{now.strftime('%Y%m%d-%H%M%S')}.enc".lstrip("/")
            try:
                key = await asyncio.to_thread(load_or_create_key, self.folder)
                archive = await asyncio.to_thread(make_archive, self.folder, cfg.get("include_photos", True))
                blob = await asyncio.to_thread(encrypt, archive, key)
                client = (self._client or make_client)(cfg)
                await asyncio.to_thread(client.put_object, Bucket=cfg["bucket"], Key=name, Body=blob,
                                        ServerSideEncryption="AES256", ContentType="application/octet-stream")
            except HubError:
                raise
            except Exception as exc:  # noqa: BLE001 - boto raises many types; explain() maps the common ones
                message = explain(exc, cfg)
                if record:
                    self._record(now, False, None, None, message)
                raise HubError(502, message) from exc
            if record:
                self._record(now, True, len(blob), name, "")
            return {"key": name, "bytes": len(blob)}

    def _record(self, now: datetime, ok: bool, size, key, note: str) -> None:
        self.db.execute("INSERT INTO backups (at, ok, bytes, key, note) VALUES (?, ?, ?, ?, ?)",
                        (now.isoformat(timespec="seconds"), int(ok), size, key, note))
        self.db.execute("DELETE FROM backups WHERE id NOT IN (SELECT id FROM backups ORDER BY id DESC LIMIT 60)")
        self.db.commit()

    async def connect(self, cfg: dict) -> dict:
        """Save the settings only if a first backup uploads, so permissions are proven."""
        result = await self.run(cfg=cfg)
        self.secrets.set("s3_backup", cfg)
        return result

    def disconnect(self) -> None:
        self.secrets.set("s3_backup", None)

    def recovery_key(self) -> str:
        return base64.b64encode(load_or_create_key(self.folder)).decode()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                if self.active and should_run(datetime.now(), self._last(ok=True), self._last()):
                    await self.run()
            except Exception as exc:  # noqa: BLE001 - recorded already; keep the loop alive
                log.warning("Backup: %s", exc)

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m hub.backup", description="Restore an encrypted hub backup")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("restore", help="decrypt a backup file and unpack it into a folder")
    r.add_argument("file", type=Path)
    r.add_argument("--to", type=Path, required=True, help="empty folder to unpack into (use ~/.smarthome on the new Mac)")
    group = r.add_mutually_exclusive_group(required=True)
    group.add_argument("--key-file", type=Path)
    group.add_argument("--key", help="the recovery key (base64)")
    r.add_argument("--force", action="store_true", help="overwrite a folder that isn't empty")
    args = parser.parse_args(argv)
    try:
        key = base64.b64decode(args.key or args.key_file.read_text().strip())
        written = restore(args.file.read_bytes(), key, args.to.expanduser(), args.force)
    except (OSError, ValueError) as exc:
        print(f"Restore failed: {exc}", file=sys.stderr)
        return 1
    print(f"Restored {len(written)} file(s) into {args.to}. Stop the hub before restoring over a live folder; "
          "then copy the recovery key back as backup.key if you want backups to continue with the same key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
