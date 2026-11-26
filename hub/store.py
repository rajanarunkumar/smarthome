"""Rooms and device-to-room assignments. The hub is the source of truth for these."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path: Path | str) -> None:
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        # Write-ahead log: a crash or power cut mid-write can't corrupt the database, and the
        # background collectors don't block the web app. NORMAL is the recommended pairing with WAL.
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA synchronous = NORMAL")
        if str(path) != ":memory:":
            try:
                os.chmod(path, 0o600)  # owner only; the -wal and -shm files copy this
            except OSError:
                pass
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS rooms (
                id   INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE
            );
            CREATE TABLE IF NOT EXISTS device_rooms (
                device_id TEXT PRIMARY KEY,
                room_id   INTEGER REFERENCES rooms(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS device_names (
                device_id TEXT PRIMARY KEY,
                name      TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    # Rooms
    def rooms(self) -> list[dict]:
        rows = self.db.execute("SELECT id, name FROM rooms ORDER BY name COLLATE NOCASE")
        return [dict(r) for r in rows]

    def room(self, room_id: int) -> dict | None:
        row = self.db.execute("SELECT id, name FROM rooms WHERE id = ?", (room_id,)).fetchone()
        return dict(row) if row else None

    def room_by_name(self, name: str) -> dict | None:
        row = self.db.execute("SELECT id, name FROM rooms WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None

    def add_room(self, name: str) -> dict:
        cur = self.db.execute("INSERT INTO rooms (name) VALUES (?)", (name.strip(),))
        self.db.commit()
        return {"id": cur.lastrowid, "name": name.strip()}

    def rename_room(self, room_id: int, name: str) -> None:
        self.db.execute("UPDATE rooms SET name = ? WHERE id = ?", (name.strip(), room_id))
        self.db.commit()

    def delete_room(self, room_id: int) -> None:
        self.db.execute("DELETE FROM rooms WHERE id = ?", (room_id,))
        self.db.commit()

    # Assignments
    def room_map(self) -> dict[str, int]:
        rows = self.db.execute("SELECT device_id, room_id FROM device_rooms")
        return {r["device_id"]: r["room_id"] for r in rows}

    def set_device_room(self, device_id: str, room_id: int | None) -> None:
        if room_id is None:
            self.db.execute("DELETE FROM device_rooms WHERE device_id = ?", (device_id,))
        else:
            self.db.execute(
                "INSERT INTO device_rooms (device_id, room_id) VALUES (?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET room_id = excluded.room_id",
                (device_id, room_id),
            )
        self.db.commit()

    # Names for brands that can't store a name themselves (Govee LAN, LG TV)
    def device_names(self) -> dict[str, str]:
        rows = self.db.execute("SELECT device_id, name FROM device_names")
        return {r["device_id"]: r["name"] for r in rows}

    def set_device_name(self, device_id: str, name: str) -> None:
        self.db.execute(
            "INSERT INTO device_names (device_id, name) VALUES (?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET name = excluded.name",
            (device_id, name.strip()),
        )
        self.db.commit()
