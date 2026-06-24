"""Persistent catalog of named CDC extracts (sqlite).

Each extract row carries its capture **watermark** (highest SCN captured) and
the replicat **apply cursor** (trail lines already applied), so capture and
apply advance independently and survive process restarts.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Extract:
    name: str
    schema: str
    tables: List[str]
    watermark: int
    apply_cursor: int
    state: str


class CdcRegistry:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS extracts ("
            "  name TEXT PRIMARY KEY,"
            "  schema TEXT,"
            "  tables_csv TEXT,"
            "  watermark INTEGER NOT NULL DEFAULT 0,"
            "  apply_cursor INTEGER NOT NULL DEFAULT 0,"
            "  state TEXT NOT NULL DEFAULT 'registered')"
        )
        self._db.commit()

    def register(self, name: str, schema: str, tables: List[str]) -> None:
        """Create the extract if absent; refresh its table set if it exists."""
        self._db.execute(
            "INSERT INTO extracts (name, schema, tables_csv) VALUES (?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET schema=excluded.schema, tables_csv=excluded.tables_csv",
            (name, schema, ",".join(tables)),
        )
        self._db.commit()

    def get(self, name: str) -> Optional[Extract]:
        row = self._db.execute(
            "SELECT name, schema, tables_csv, watermark, apply_cursor, state "
            "FROM extracts WHERE name=?", (name,)
        ).fetchone()
        if not row:
            return None
        return Extract(row[0], row[1], [t for t in (row[2] or "").split(",") if t],
                       int(row[3]), int(row[4]), row[5])

    def list(self) -> List[Extract]:
        return [Extract(r[0], r[1], [t for t in (r[2] or "").split(",") if t],
                        int(r[3]), int(r[4]), r[5])
                for r in self._db.execute(
                    "SELECT name, schema, tables_csv, watermark, apply_cursor, state "
                    "FROM extracts ORDER BY name").fetchall()]

    def set_watermark(self, name: str, scn: int) -> None:
        self._db.execute("UPDATE extracts SET watermark=?, state='capturing' WHERE name=?", (scn, name))
        self._db.commit()

    def set_apply_cursor(self, name: str, cursor: int) -> None:
        self._db.execute("UPDATE extracts SET apply_cursor=?, state='applying' WHERE name=?", (cursor, name))
        self._db.commit()

    def close(self) -> None:
        self._db.close()
