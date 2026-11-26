"""Settings and database durability: what survives a restart, a crash, a bad file."""

import json
import os
import stat
import threading

import pytest

from hub.config import Secrets
from hub.store import Store


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_settings_survive_a_restart_and_files_are_owner_only(tmp_path):
    path = tmp_path / "secrets.json"
    s = Secrets(path)
    s.set("schlage", {"email": "hub@example.com", "password": "pw"})
    s.set("location", {"name": "McKinney, Texas"})
    again = Secrets(path)  # a fresh process reading the file
    assert again.get("schlage")["email"] == "hub@example.com" and again.get("location")["name"] == "McKinney, Texas"
    assert mode(path) == 0o600 and mode(tmp_path / "secrets.json.bak") == 0o600
    assert again.keys() == ["location", "schlage"]
    s.set("schlage", None)  # disconnecting removes the value but not other settings
    assert Secrets(path).keys() == ["location"]


def test_a_corrupt_file_falls_back_to_the_last_good_backup(tmp_path):
    path = tmp_path / "secrets.json"
    s = Secrets(path)
    s.set("a", 1)
    s.set("b", 2)                       # now secrets.json = {a, b} and .bak = {a}
    path.write_text('{"a": 1, "b": 2,')  # power cut mid-write by something else
    recovered = Secrets(path)
    assert recovered.get("a") == 1 and recovered.get("b") is None  # last good version, not an empty store
    path.write_text("garbage")
    (tmp_path / "secrets.json.bak").write_text("garbage")
    with pytest.raises(RuntimeError):   # refuse to start empty and overwrite everything later
        Secrets(path)


def test_an_edit_made_outside_the_hub_is_kept(tmp_path):
    path = tmp_path / "secrets.json"
    s = Secrets(path)
    s.set("a", 1)
    edited = json.loads(path.read_text())
    edited["hand_added"] = "keep me"
    path.write_text(json.dumps(edited))
    s.set("b", 2)                        # must merge with the hand edit, not overwrite it
    assert json.loads(path.read_text()) == {"a": 1, "hand_added": "keep me", "b": 2}


def test_concurrent_writes_are_all_kept(tmp_path):
    s = Secrets(tmp_path / "secrets.json")
    threads = [threading.Thread(target=s.set, args=(f"k{i}", i)) for i in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert Secrets(tmp_path / "secrets.json").keys() == sorted(f"k{i}" for i in range(30))


def test_database_is_crash_safe_and_owner_only(tmp_path):
    path = tmp_path / "hub.db"
    store = Store(path)
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert mode(path) == 0o600
    store.add_room("Kitchen")
    store.db.close()
    assert [r["name"] for r in Store(path).rooms()] == ["Kitchen"]  # survives a restart


def test_storage_report_lists_names_and_counts_never_values(tmp_path, monkeypatch):
    monkeypatch.setenv("SMARTHOME_HOME", str(tmp_path))
    from fastapi.testclient import TestClient
    from hub import app as app_module
    with TestClient(app_module.app) as client:
        hub = app_module.app.state.hub
        hub.secrets.set("smt", {"username": "me", "password": "SuperSecret123"})
        hub.secrets.set("hue", {"app_key": "KEY123456", "host": "10.0.0.2"})
        hub.store.add_room("Kitchen")
        r = client.get("/api/storage", headers={"Authorization": f"Bearer {hub.secrets.access_token()}"})
        assert r.status_code == 200
        body = r.json()
        kinds = {x["key"]: x["kind"] for x in body["saved"]}
        assert kinds["smt"] == "password" and kinds["hue"] == "key" and kinds["access_token"] == "token"
        assert next(d for d in body["data"] if d["group"] == "Rooms and names")["rows"] == 1
        assert body["folder"] == str(tmp_path) and body["settings_file"]["backup"] is True
        text = r.text
        assert "SuperSecret123" not in text and "KEY123456" not in text and "10.0.0.2" not in text
