"""Transactional workbook versions for one local/shared MVP instance."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .data import read_workbook


def compare(old, new):
    before = old.sites.set_index("site_id")
    after = new.sites.set_index("site_id")
    rows = []
    for sid in sorted(set(before.index) | set(after.index)):
        if sid not in before.index:
            rows.append({"site_id": sid, "change": "Добавлена площадка", "fields": ""})
            continue
        if sid not in after.index:
            rows.append({"site_id": sid, "change": "Удалена площадка", "fields": ""})
            continue
        fields = [
            col
            for col in [
                "name",
                "address",
                "city",
                "street",
                "house",
                "latitude",
                "longitude",
                "service_type",
            ]
            if col in before.columns
            and col in after.columns
            and str(before.loc[sid, col]) != str(after.loc[sid, col])
        ]
        if fields:
            rows.append(
                {
                    "site_id": sid,
                    "change": "Изменена площадка",
                    "fields": ", ".join(fields),
                    "old_address": before.loc[sid, "address"],
                    "new_address": after.loc[sid, "address"],
                    "old_type": before.loc[sid, "service_type"],
                    "new_type": after.loc[sid, "service_type"],
                }
            )
    services_changed = not old.services.fillna("").equals(new.services.fillna(""))
    return {
        "sites_added": len(set(after.index) - set(before.index)),
        "sites_removed": len(set(before.index) - set(after.index)),
        "sites_changed": sum(r["change"] == "Изменена площадка" for r in rows),
        "equipment_delta": len(new.equipment) - len(old.equipment),
        "services_changed": services_changed,
        "details": rows,
    }


class ImportStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(exist_ok=True, parents=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS versions (id TEXT PRIMARY KEY, content BLOB NOT NULL, filename TEXT NOT NULL, created_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )

    def apply(self, content, filename, expected_current=None):
        # Validate before opening a write transaction; invalid data never become active.
        parsed = read_workbook(content)
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute(
                "SELECT value FROM settings WHERE key='active'"
            ).fetchone()
            current = active[0] if active else None
            if expected_current is not None and current != expected_current:
                raise ValueError(
                    "Набор данных уже изменён в другой сессии. Обновите страницу и повторите импорт."
                )
            db.execute(
                "INSERT OR IGNORE INTO versions VALUES (?,?,?,?)",
                (
                    parsed.fingerprint,
                    content,
                    Path(filename).name,
                    datetime.now(UTC).isoformat(),
                ),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('active',?)",
                (parsed.fingerprint,),
            )
        return parsed.fingerprint

    def active(self):
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT v.content FROM versions v JOIN settings s ON s.value=v.id WHERE s.key='active'"
            ).fetchone()
        return bytes(row[0]) if row else None

    def history(self):
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT id,filename,created_at FROM versions ORDER BY created_at DESC"
            ).fetchall()
        return [
            {"id": row[0], "filename": row[1], "created_at": row[2]} for row in rows
        ]

    def activate(self, version):
        with sqlite3.connect(self.path) as db:
            if not db.execute(
                "SELECT 1 FROM versions WHERE id=?", (version,)
            ).fetchone():
                raise ValueError("Версия не найдена")
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('active',?)", (version,)
            )


def display_time(value):
    return (
        datetime.fromisoformat(value)
        .astimezone(ZoneInfo("Asia/Yekaterinburg"))
        .isoformat(timespec="seconds")
    )
