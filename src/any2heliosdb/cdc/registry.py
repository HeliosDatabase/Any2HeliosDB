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

from ..errors import Any2HeliosError


@dataclass
class Extract:
    name: str
    schema: str
    tables: List[str]
    watermark: int
    apply_cursor: int
    state: str


def _reject_comma_table_names(tables: List[str]) -> None:
    """Refuse to persist a table name containing a comma. The registry stores the
    captured table set as a single comma-separated ``tables_csv`` column; a name
    with a comma would be split on load and silently drop that table from capture,
    so fail closed (this is cheaper than migrating the storage format, and a comma
    in a real table name is exotic)."""
    bad = [t for t in tables if "," in t]
    if bad:
        raise Any2HeliosError(
            "CDC registry: cannot register/adopt table name(s) {} — a comma collides "
            "with the registry's comma-separated tables_csv storage and would silently "
            "split the name, excluding the table from capture. Rename the table(s) or "
            "exclude them from the extract's schema.".format(bad))


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

    def register(self, name: str, schema: str, tables: List[str],
                 adopt_tables: bool = True) -> None:
        """Create the extract if absent; update it if it exists.

        ``adopt_tables`` controls what happens to the registered table set on an
        EXISTING extract (tier-2 H2). With ``True`` (first registration, or an
        explicit ``a2h extract NAME --refresh-tables``) the pinned table set is
        replaced with *tables*. With ``False`` (a routine cycle) the table set is
        left **pinned** — the schema is still refreshed, but a table that appeared
        in the source after registration is NOT silently absorbed; the engine
        detects it, warns each cycle, and only adopts + snapshot-loads it on an
        explicit ``--refresh-tables``. A brand-new extract always adopts (there is
        nothing pinned yet)."""
        existing = self.get(name)
        if existing is None:
            _reject_comma_table_names(tables)
            self._db.execute(
                "INSERT INTO extracts (name, schema, tables_csv) VALUES (?,?,?)",
                (name, schema, ",".join(tables)),
            )
        elif adopt_tables:
            _reject_comma_table_names(tables)
            self._db.execute(
                "UPDATE extracts SET schema=?, tables_csv=? WHERE name=?",
                (schema, ",".join(tables), name),
            )
        else:
            self._db.execute("UPDATE extracts SET schema=? WHERE name=?", (schema, name))
        self._db.commit()

    def remove(self, name: str) -> bool:
        """Delete the extract's registry row (``a2h extract NAME --drop``). Returns
        ``True`` if a row existed. The trail and dead-letter files are separate
        (the caller removes them only on ``--purge-trail``)."""
        cur = self._db.execute("DELETE FROM extracts WHERE name=?", (name,))
        self._db.commit()
        return cur.rowcount > 0

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
