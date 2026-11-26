"""Encrypted S3 backups: contents, encryption, restore safety, scheduling, and the API."""

import asyncio
import base64
import io
import json
import sqlite3
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hub import backup
from hub.backup import Backups, decrypt, encrypt, load_or_create_key, make_archive, restore, should_run
from hub.config import Secrets
from hub.models import HubError
from hub.store import Store

PASSWORD = "SuperSecretPassword123"


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    store = Store(home / "hub.db")
    store.add_room("Kitchen")
    store.db.close()
    Secrets(home / "secrets.json").set("schlage", {"email": "me@example.com", "password": PASSWORD})
    (home / "photos").mkdir()
    (home / "photos" / "zone1.jpg").write_bytes(b"\xff\xd8 not really a jpeg")
    load_or_create_key(home)
    return home


class FakeS3:
    def __init__(self, error=None):
        self.puts, self.error = [], error

    def put_object(self, **kw):
        if self.error:
            raise self.error
        self.puts.append(kw)


def client_error(code):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "PutObject")


def service(tmp_path, fake):
    home = make_home(tmp_path)
    db = sqlite3.connect(home / "hub.db")
    db.row_factory = sqlite3.Row
    return home, Backups(Secrets(home / "secrets.json"), db, home, client_factory=lambda cfg: fake)


CFG = {"bucket": "my-bucket", "region": "us-east-2", "prefix": "smarthome", "include_photos": True}


def test_archive_has_a_consistent_snapshot_and_never_the_key(tmp_path):
    home = make_home(tmp_path)
    with tarfile.open(fileobj=io.BytesIO(make_archive(home)), mode="r:gz") as tar:
        names = set(tar.getnames())
        assert {"hub.db", "secrets.json", "photos", "photos/zone1.jpg"} <= names and "backup.key" not in names
        db_bytes = tar.extractfile("hub.db").read()
    snapshot = tmp_path / "snap.db"
    snapshot.write_bytes(db_bytes)
    assert [r[0] for r in sqlite3.connect(snapshot).execute("SELECT name FROM rooms")] == ["Kitchen"]
    with tarfile.open(fileobj=io.BytesIO(make_archive(home, include_photos=False)), mode="r:gz") as tar:
        assert "photos/zone1.jpg" not in tar.getnames()


def test_encryption_round_trip_wrong_key_tamper_and_foreign_files():
    key, data = bytes(range(32)), b"secret archive"
    blob = encrypt(data, key)
    assert decrypt(blob, key) == data and PASSWORD.encode() not in blob
    assert encrypt(data, key) != blob  # a fresh nonce every time
    with pytest.raises(ValueError, match="Wrong key"):
        decrypt(blob, bytes(32))
    flipped = bytearray(blob)
    flipped[-1] ^= 1
    with pytest.raises(ValueError, match="changed"):
        decrypt(bytes(flipped), key)
    with pytest.raises(ValueError, match="isn't a smart home"):
        decrypt(b"hello world " * 8, key)


def test_upload_is_encrypted_with_s3_side_encryption_and_restores(tmp_path):
    fake = FakeS3()
    home, svc = service(tmp_path, fake)
    result = asyncio.run(svc.run(now=datetime(2026, 11, 22, 3, 30, 5), cfg=CFG))
    [put] = fake.puts
    assert put["Bucket"] == "my-bucket" and put["Key"] == "smarthome/hub-20261122-033005.enc"
    assert put["ServerSideEncryption"] == "AES256" and result["bytes"] == len(put["Body"])
    assert PASSWORD.encode() not in put["Body"] and b"SQLite format" not in put["Body"]  # only ciphertext leaves the Mac
    key = base64.b64decode(svc.recovery_key())
    written = restore(put["Body"], key, tmp_path / "new-mac")
    assert {"hub.db", "secrets.json", "photos/zone1.jpg"} <= set(written)
    assert json.loads((tmp_path / "new-mac" / "secrets.json").read_text())["schlage"]["password"] == PASSWORD
    assert svc.history()[0]["ok"] == 1


def test_connect_only_saves_settings_after_a_working_upload(tmp_path):
    home, svc = service(tmp_path, FakeS3(error=client_error("AccessDenied")))
    with pytest.raises(HubError) as err:
        asyncio.run(svc.connect(dict(CFG)))
    assert "s3:PutObject" in err.value.message and "smarthome" in err.value.message
    assert not svc.active and svc.history()[0]["ok"] == 0
    for code, text in (("NoSuchBucket", "doesn't exist"), ("InvalidAccessKeyId", "rejected"), ("PermanentRedirect", "region")):
        svc._client = lambda cfg, code=code: FakeS3(error=client_error(code))
        with pytest.raises(HubError, match=text):
            asyncio.run(svc.run(cfg=CFG))
    svc._client = lambda cfg: FakeS3()
    asyncio.run(svc.connect(dict(CFG)))
    assert svc.active and svc.config["bucket"] == "my-bucket"


def test_restore_refuses_nonempty_folders_and_unsafe_archives(tmp_path):
    key = bytes(range(32))
    ok = encrypt(make_archive(make_home(tmp_path)), key)
    target = tmp_path / "target"
    target.mkdir()
    (target / "existing.txt").write_text("keep")
    with pytest.raises(ValueError, match="isn't empty"):
        restore(ok, key, target)
    assert restore(ok, key, target, force=True) and (target / "existing.txt").read_text() == "keep"

    evil = io.BytesIO()
    with tarfile.open(fileobj=evil, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"evil"))
    with pytest.raises(ValueError, match="Unsafe"):
        restore(encrypt(evil.getvalue(), key), key, tmp_path / "clean")
    assert not (tmp_path / "escape.txt").exists()


def test_schedule_runs_once_a_day_after_0330_and_retries_hourly_after_failure():
    day = datetime(2026, 11, 22)
    at = lambda h, m: day.replace(hour=h, minute=m)
    assert not should_run(at(3, 29), None, None)
    assert should_run(at(3, 30), None, None)
    assert not should_run(at(9, 0), at(3, 31), at(3, 31))                    # already succeeded today
    assert should_run(at(3, 30), at(3, 31) - timedelta(days=1), None)        # yesterday's doesn't count
    assert not should_run(at(4, 0), None, at(3, 40))                          # failed 20 minutes ago
    assert should_run(at(4, 45), None, at(3, 40))                             # failed over an hour ago


def test_api_never_returns_aws_secrets_and_the_script_token_cannot_read_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    fake = FakeS3()
    monkeypatch.setattr(backup, "make_client", lambda cfg: fake)
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        token = {"Authorization": f"Bearer {hub.secrets.access_token()}"}
        body = {"bucket": "my-bucket", "region": "us-east-2", "prefix": "smarthome",
                "access_key_id": "AKIAEXAMPLE", "secret_access_key": "wJalrXUtnFEMI/K7MDENG"}
        assert client.put("/api/backup", json={**body, "secret_access_key": ""}, headers=token).status_code == 400
        assert client.put("/api/backup", json=body, headers=token).status_code == 200
        status = client.get("/api/backup", headers=token)
        assert status.json()["configured"] and status.json()["own_keys"] and status.json()["last_ok"]["ok"] == 1
        assert "wJalrXUtnFEMI" not in status.text and "AKIAEXAMPLE" not in status.text
        assert client.get("/api/backup/key", headers=token).status_code == 403
        hub.secrets.set("auth", {"origins": ["https://hub.example.ts.net"]})
        monkeypatch.setattr(hub.auth, "session", lambda cookie: {"email": "me@example.com", "method": "passkey"})
        key = client.get("/api/backup/key").json()["key"]
        assert len(base64.b64decode(key)) == 32
        assert client.post("/api/backup/run", headers={"Origin": "https://hub.example.ts.net"}).status_code == 200
        assert len(fake.puts) == 2
        assert client.put("/api/backup", json={**body, "bucket": "Bad Bucket!"}, headers=token).status_code == 422
        assert client.get("/api/storage", headers=token).json()["saved"][-1]["key"] in {"s3_backup", "access_token", "auth"}


def test_restore_command_line(tmp_path, capsys):
    home = make_home(tmp_path)
    key = base64.b64decode(Path(home / "backup.key").read_text().strip())
    blob = tmp_path / "b.enc"
    blob.write_bytes(encrypt(make_archive(home), key))
    assert backup.main(["restore", str(blob), "--key-file", str(home / "backup.key"), "--to", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "hub.db").exists()
    assert backup.main(["restore", str(blob), "--key", base64.b64encode(bytes(32)).decode(), "--to", str(tmp_path / "out2")]) == 1
    assert "Wrong key" in capsys.readouterr().err
