"""Durable run manifest / checkpoint store (SQLite-WAL).

Generalizes Ora2Pg's ``TABLES_SCN.log`` into a real ledger so an interrupted
migration resumes without re-loading completed work. The unit of resume is the
*chunk*, not the table.

Chunk state machine: ``pending → in_progress → loaded → verified`` (or
``failed``). On recovery, any ``in_progress`` chunk is reset to ``pending`` —
safe because chunk application is idempotent (truncate-and-reload or staged
merge), so a half-done chunk is simply redone.

Uses only the standard library (``sqlite3``); WAL mode gives crash-safe
per-transition commits across worker processes.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional

PENDING = "pending"
IN_PROGRESS = "in_progress"
LOADED = "loaded"
VERIFIED = "verified"
FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    config_hash TEXT,
    source_fingerprint TEXT,
    started_at TEXT,
    status TEXT
);
CREATE TABLE IF NOT EXISTS tables (
    run_id TEXT, table_fqn TEXT, target_table TEXT,
    total_chunks INTEGER DEFAULT 0, total_rows_est INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    PRIMARY KEY (run_id, table_fqn)
);
CREATE TABLE IF NOT EXISTS chunks (
    run_id TEXT, table_fqn TEXT, chunk_id TEXT,
    predicate TEXT, bounds_lo TEXT, bounds_hi TEXT,
    state TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0, rows_loaded INTEGER DEFAULT 0,
    checksum TEXT, error TEXT,
    PRIMARY KEY (run_id, table_fqn, chunk_id)
);
CREATE TABLE IF NOT EXISTS watermarks (
    run_id TEXT, table_fqn TEXT, kind TEXT, value TEXT, captured_at TEXT,
    PRIMARY KEY (run_id, table_fqn)
);
"""


@dataclass
class ChunkRow:
    table_fqn: str
    chunk_id: str
    predicate: Optional[str]
    state: str
    attempts: int


class Manifest:
    def __init__(self, path: str, readonly: bool = False) -> None:
        self.path = path
        self.readonly = readonly
        if readonly:
            # Attach to an existing manifest a concurrent loader may be writing.
            # WAL permits one reader alongside the writer; open the connection
            # truly read-only (mode=ro URI) so we can never take a write lock or
            # mutate the ledger. No makedirs / no schema DDL / no PRAGMA writes.
            uri = "file:{}?mode=ro".format(os.path.abspath(path))
            self._db = sqlite3.connect(uri, uri=True)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._db = sqlite3.connect(path)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    @classmethod
    def open_readonly(cls, path: str) -> "Manifest":
        """Open an existing manifest read-only (for the live monitor).

        Raises FileNotFoundError if the manifest does not exist yet."""
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        return cls(path, readonly=True)

    def close(self) -> None:
        self._db.close()

    # --- run/table/chunk registration ---
    def start_run(self, run_id: str, config_hash: str = "", fingerprint: str = "",
                  started_at: str = "") -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO runs (run_id, config_hash, source_fingerprint, started_at, status) "
            "VALUES (?,?,?,?, 'running')",
            (run_id, config_hash, fingerprint, started_at),
        )
        self._db.commit()

    def add_table(self, run_id: str, table_fqn: str, target_table: str,
                  total_rows_est: int = 0) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO tables (run_id, table_fqn, target_table, total_rows_est, status) "
            "VALUES (?,?,?,?, 'pending')",
            (run_id, table_fqn, target_table, total_rows_est),
        )
        self._db.commit()

    def add_chunk(self, run_id: str, table_fqn: str, chunk_id: str,
                  predicate: Optional[str] = None, lo: Optional[str] = None,
                  hi: Optional[str] = None) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO chunks (run_id, table_fqn, chunk_id, predicate, bounds_lo, bounds_hi) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, table_fqn, chunk_id, predicate, lo, hi),
        )
        self._db.execute(
            "UPDATE tables SET total_chunks = (SELECT count(*) FROM chunks "
            "WHERE run_id=? AND table_fqn=?) WHERE run_id=? AND table_fqn=?",
            (run_id, table_fqn, run_id, table_fqn),
        )
        self._db.commit()

    # --- state transitions ---
    def set_chunk_state(self, run_id: str, table_fqn: str, chunk_id: str, state: str,
                        rows_loaded: int = 0, checksum: Optional[str] = None,
                        error: Optional[str] = None, bump_attempt: bool = False) -> None:
        attempt_sql = ", attempts = attempts + 1" if bump_attempt else ""
        self._db.execute(
            "UPDATE chunks SET state=?, rows_loaded=?, checksum=?, error=?{} "
            "WHERE run_id=? AND table_fqn=? AND chunk_id=?".format(attempt_sql),
            (state, rows_loaded, checksum, error, run_id, table_fqn, chunk_id),
        )
        self._db.commit()

    def reset_run(self, run_id: str) -> None:
        """Clear a run's chunk state so a drop_existing re-migrate reloads from
        scratch. Recreating the tables invalidates any prior LOADED chunks — if
        they survived (add_chunk is INSERT OR IGNORE) the loader would skip them
        and leave the just-emptied tables unloaded (silent data loss)."""
        self._db.execute("DELETE FROM chunks WHERE run_id=?", (run_id,))
        self._db.execute(
            "UPDATE tables SET status='pending', total_chunks=0 WHERE run_id=?", (run_id,))
        self._db.commit()

    def recover(self, run_id: str) -> int:
        """Reset in_progress chunks to pending; return how many were reset."""
        cur = self._db.execute(
            "UPDATE chunks SET state=? WHERE run_id=? AND state=?",
            (PENDING, run_id, IN_PROGRESS),
        )
        self._db.commit()
        return cur.rowcount

    def pending_chunks(self, run_id: str, table_fqn: Optional[str] = None) -> List[ChunkRow]:
        sql = ("SELECT table_fqn, chunk_id, predicate, state, attempts FROM chunks "
               "WHERE run_id=? AND state IN (?, ?)")
        params: list = [run_id, PENDING, FAILED]
        if table_fqn:
            sql += " AND table_fqn=?"
            params.append(table_fqn)
        sql += " ORDER BY table_fqn, chunk_id"
        return [ChunkRow(*r) for r in self._db.execute(sql, params).fetchall()]

    def is_chunk_done(self, run_id: str, table_fqn: str, chunk_id: str) -> bool:
        row = self._db.execute(
            "SELECT state FROM chunks WHERE run_id=? AND table_fqn=? AND chunk_id=?",
            (run_id, table_fqn, chunk_id),
        ).fetchone()
        return bool(row) and row[0] in (LOADED, VERIFIED)

    # --- watermarks (P2 incremental / CDC anchor) ---
    def set_watermark(self, run_id: str, table_fqn: str, kind: str, value: str,
                      captured_at: str = "") -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO watermarks (run_id, table_fqn, kind, value, captured_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, table_fqn, kind, value, captured_at),
        )
        self._db.commit()

    def get_watermark(self, run_id: str, table_fqn: str):  # type: ignore[no-untyped-def]
        return self._db.execute(
            "SELECT kind, value FROM watermarks WHERE run_id=? AND table_fqn=?",
            (run_id, table_fqn),
        ).fetchone()

    # --- reporting ---
    def rows_by_table(self, run_id: str) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for fqn, n in self._db.execute(
            "SELECT table_fqn, coalesce(sum(rows_loaded), 0) FROM chunks "
            "WHERE run_id=? GROUP BY table_fqn", (run_id,)
        ).fetchall():
            out[fqn] = int(n)
        return out

    def summary(self, run_id: str) -> dict:
        counts = {}
        for state, n in self._db.execute(
            "SELECT state, count(*) FROM chunks WHERE run_id=? GROUP BY state", (run_id,)
        ).fetchall():
            counts[state] = n
        rows = self._db.execute(
            "SELECT coalesce(sum(rows_loaded),0) FROM chunks WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        return {"chunk_states": counts, "rows_loaded": rows}

    def progress_snapshot(self, run_id: str) -> dict:
        """Read-only progress snapshot for the live monitor.

        Single LEFT-JOIN/GROUP-BY pass so a concurrent reader (sqlite WAL allows
        one) gets a consistent per-table view: chunk totals, how many are
        loaded/verified vs failed, rows loaded so far, and the planned row
        estimate. Also returns roll-up totals. Pure read; never writes.

        A table counts as *done* once it has chunks and every chunk is
        loaded/verified (matches the loader's notion of completion); a table
        with at least one failed chunk is reported as failed. ``status`` is the
        derived live status used for display: one of ``pending`` /
        ``in_progress`` / ``loaded`` / ``failed`` / ``verified``.
        """
        # One row per planned table; aggregate its chunks in the same statement.
        sql = (
            "SELECT t.table_fqn, t.target_table, t.status, t.total_chunks, t.total_rows_est, "
            "       count(c.chunk_id) AS chunks_seen, "
            "       coalesce(sum(CASE WHEN c.state IN (?, ?) THEN 1 ELSE 0 END), 0) AS chunks_loaded, "
            "       coalesce(sum(CASE WHEN c.state = ? THEN 1 ELSE 0 END), 0) AS chunks_failed, "
            "       coalesce(sum(CASE WHEN c.state = ? THEN 1 ELSE 0 END), 0) AS chunks_in_progress, "
            "       coalesce(sum(c.rows_loaded), 0) AS rows_loaded "
            "FROM tables t LEFT JOIN chunks c "
            "  ON c.run_id = t.run_id AND c.table_fqn = t.table_fqn "
            "WHERE t.run_id = ? "
            "GROUP BY t.table_fqn, t.target_table, t.status, t.total_chunks, t.total_rows_est "
            "ORDER BY t.table_fqn"
        )
        params = (LOADED, VERIFIED, FAILED, IN_PROGRESS, run_id)
        tables: List[dict] = []
        tot_tables_done = 0
        tot_rows_loaded = 0
        tot_rows_est = 0
        tot_chunks = 0
        tot_chunks_loaded = 0
        tot_chunks_failed = 0
        for row in self._db.execute(sql, params).fetchall():
            (fqn, target, tstatus, total_chunks, rows_est, chunks_seen,
             chunks_loaded, chunks_failed, chunks_in_progress, rows_loaded) = row
            # total_chunks is maintained by add_chunk; fall back to what we see.
            chunks_total = int(total_chunks or 0) or int(chunks_seen or 0)
            chunks_loaded = int(chunks_loaded or 0)
            chunks_failed = int(chunks_failed or 0)
            chunks_in_progress = int(chunks_in_progress or 0)
            rows_loaded = int(rows_loaded or 0)
            rows_est = int(rows_est or 0)
            # Derived live status (independent of the stored table.status, which
            # the loader may not advance during the run).
            if chunks_failed:
                status = FAILED
            elif chunks_total > 0 and chunks_loaded >= chunks_total:
                status = VERIFIED if (tstatus == VERIFIED) else LOADED
            elif chunks_in_progress or (chunks_loaded > 0 and chunks_loaded < chunks_total):
                status = IN_PROGRESS
            else:
                status = PENDING
            done = chunks_total > 0 and chunks_loaded >= chunks_total and not chunks_failed
            if done:
                tot_tables_done += 1
            tot_rows_loaded += rows_loaded
            tot_rows_est += rows_est
            tot_chunks += chunks_total
            tot_chunks_loaded += chunks_loaded
            tot_chunks_failed += chunks_failed
            tables.append({
                "table_fqn": fqn,
                "target_table": target,
                "status": status,
                "chunks_total": chunks_total,
                "chunks_loaded": chunks_loaded,
                "chunks_failed": chunks_failed,
                "chunks_in_progress": chunks_in_progress,
                "rows_loaded": rows_loaded,
                "rows_est": rows_est,
            })
        n_tables = len(tables)
        return {
            "run_id": run_id,
            "tables": tables,
            "tables_total": n_tables,
            "tables_done": tot_tables_done,
            "rows_loaded": tot_rows_loaded,
            "rows_est": tot_rows_est,
            "chunks_total": tot_chunks,
            "chunks_loaded": tot_chunks_loaded,
            "chunks_failed": tot_chunks_failed,
            # Overall completion: every planned table is done.
            "complete": n_tables > 0 and tot_tables_done == n_tables,
        }
