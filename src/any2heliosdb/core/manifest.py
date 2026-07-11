"""Durable run manifest / checkpoint store.

Generalizes Ora2Pg's ``TABLES_SCN.log`` into a real ledger so an interrupted
migration resumes without re-loading completed work. The unit of resume is the
*chunk*, not the table.

Chunk state machine: ``pending → in_progress → loaded → verified`` (or
``failed``). On recovery, any ``in_progress`` chunk is reset to ``pending`` —
safe because chunk application is idempotent (truncate-and-reload or staged
merge), so a half-done chunk is simply redone.

**Backends.** The default is the standard library ``sqlite3`` (WAL mode,
crash-safe, zero-dependency — a single ``manifest.db`` file). Optionally
(``manifest_backend = "nano"``) the ledger runs on an **embedded HeliosDB-Nano**
(``heliosdb-nano-embedded``, in-process — a ``manifest.db`` *directory*), which
lets a2h dogfood its own database as its checkpoint store. The SQL is portable
(``ON CONFLICT`` upserts); a thin connection adapter maps qmark params to ``$n``,
dict rows to tuples, and ``commit()`` to ``flush()``. Read sites (``status`` /
``monitor``) auto-detect the backend from the path (a Nano manifest is a dir).
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Union

SQLITE = "sqlite"
NANO = "nano"


def detect_backend(path: str) -> str:
    """A Nano manifest is a RocksDB *directory*; a SQLite manifest is a file."""
    return NANO if os.path.isdir(path) else SQLITE


def manifest_path_for(output_dir: str, backend: str = SQLITE) -> str:
    """On-disk manifest location for a backend. SQLite is a single FILE
    (``manifest.db``); the embedded-Nano backend is a RocksDB DIRECTORY
    (``manifest.nano``). Distinct names mean the two never collide if a project
    switches backends, and keep ``detect_backend`` unambiguous."""
    return os.path.join(output_dir, "manifest.nano" if backend == NANO else "manifest.db")


def _qmark_to_dollar(sql: str) -> str:
    """Translate sqlite ``?`` placeholders to Nano ``$1..$n`` (positional). The
    manifest SQL never puts ``?`` inside a string literal, so a plain scan is safe."""
    out: List[str] = []
    n = 0
    for ch in sql:
        if ch == "?":
            n += 1
            out.append("${}".format(n))
        else:
            out.append(ch)
    return "".join(out)


class _NanoCursor:
    """A sqlite3-cursor-shaped view over an embedded-Nano result, so the Manifest
    code (``.fetchone()`` / ``.fetchall()`` / ``.rowcount``) is backend-agnostic."""

    def __init__(self, rows: List[tuple], rowcount: int) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


# Process-wide registry of shared embedded-Nano *writer* connections, keyed by
# the absolute manifest path. RocksDB permits a single writer per directory, so
# every Manifest RW handle to the same nano path in this process shares ONE
# EmbeddedDatabase guarded by a re-entrant lock; the underlying DB is dropped
# when the last handle closes (refcounted). This lets the multi-threaded loader
# — which opens a fresh Manifest per chunk — use the nano backend safely: the
# tiny ledger writes serialize on the lock while the target writes parallelize.
_NANO_WRITERS: Dict[str, "_NanoShared"] = {}
_NANO_WRITERS_LOCK = threading.Lock()


class _NanoShared:
    def __init__(self, db) -> None:
        self.db = db
        self.lock = threading.RLock()
        self.refs = 0


def _acquire_writer(path: str) -> "_NanoShared":
    import heliosdb_nano  # optional extra: any2heliosdb[nano-manifest]

    key = os.path.abspath(path)
    with _NANO_WRITERS_LOCK:
        shared = _NANO_WRITERS.get(key)
        if shared is None:
            os.makedirs(os.path.dirname(key), exist_ok=True)
            shared = _NanoShared(heliosdb_nano.EmbeddedDatabase(key))
            _NANO_WRITERS[key] = shared
        shared.refs += 1
        return shared


def _release_writer(path: str) -> None:
    key = os.path.abspath(path)
    with _NANO_WRITERS_LOCK:
        shared = _NANO_WRITERS.get(key)
        if shared is None:
            return
        shared.refs -= 1
        if shared.refs <= 0:
            _NANO_WRITERS.pop(key, None)
            shared.db = None  # drop -> RocksDB closes + releases the dir lock


class _NanoConn:
    """Adapts ``heliosdb_nano.EmbeddedDatabase`` to the small sqlite3.Connection
    surface the Manifest uses (``execute`` returning a cursor, ``executescript``,
    ``commit``, ``close``). RW handles share a process-wide, per-path writer
    (RocksDB single-writer) guarded by a lock so the multi-threaded loader is
    safe; RO handles (the live monitor / status — a separate process) open their
    own independent read-only view. Lazy-imports the optional dependency."""

    def __init__(self, path: str, readonly: bool = False) -> None:
        self._ro = readonly
        self._path = path
        if readonly:
            import heliosdb_nano  # optional extra: any2heliosdb[nano-manifest]

            self._shared = None
            self._lock = threading.RLock()
            self._db = heliosdb_nano.EmbeddedDatabase.open_read_only(path)
        else:
            self._shared = _acquire_writer(path)
            self._lock = self._shared.lock
            self._db = self._shared.db

    def execute(self, sql: str, params: Sequence[Any] = ()) -> _NanoCursor:
        nsql = _qmark_to_dollar(sql)
        p = list(params)
        with self._lock:
            if sql.lstrip()[:6].upper() == "SELECT":
                rows = self._db.query(nsql, p) if p else self._db.query(nsql)
                return _NanoCursor([tuple(r.values()) for r in rows], len(rows))
            affected = self._db.execute(nsql, p) if p else self._db.execute(nsql)
            return _NanoCursor([], affected if isinstance(affected, int) else -1)

    def executescript(self, script: str) -> None:
        with self._lock:
            for stmt in script.split(";"):
                if stmt.strip():
                    self._db.execute(stmt)

    def commit(self) -> None:
        # Persist + make committed rows visible to a fresh read-only open (the
        # live monitor reopens per tick). No-op on a read-only handle.
        if self._ro:
            return
        with self._lock:
            try:
                self._db.flush()
            except Exception:  # pragma: no cover - flush is best-effort
                pass

    def close(self) -> None:
        self._db = None
        if not self._ro and self._shared is not None:
            self._shared = None
            _release_writer(self._path)

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


@dataclass
class RecordedChunk:
    """One row of the recorded chunk PLAN (regardless of load state): the source
    predicate and the integer key bounds exactly as the original plan stored them.
    Replaying these on resume — instead of recomputing from live source bounds —
    is what keeps a resumed load from silently skipping or double-loading PK
    ranges when rows were inserted/deleted at the key edges mid-run."""
    table_fqn: str
    chunk_id: str
    predicate: Optional[str]
    bounds_lo: Optional[str]
    bounds_hi: Optional[str]


class Manifest:
    def __init__(self, path: str, readonly: bool = False, backend: str = SQLITE) -> None:
        self.path = path
        self.readonly = readonly
        self.backend = backend
        if backend == NANO:
            # Embedded HeliosDB-Nano (in-process). A read-only handle (RocksDB
            # read-only open) takes no lock, so the live monitor can read while
            # the loader holds the writer open. commit() flushes so a fresh
            # read-only open sees the latest committed rows.
            self._db: Union[_NanoConn, sqlite3.Connection] = _NanoConn(path, readonly=readonly)
            if not readonly:
                self._db.executescript(_SCHEMA)
                self._db.commit()
        elif readonly:
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
        """Open an existing manifest read-only (for the live monitor / status).

        The backend is auto-detected from the path (a Nano manifest is a RocksDB
        directory, a SQLite manifest is a file). Raises FileNotFoundError if the
        manifest does not exist yet."""
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        return cls(path, readonly=True, backend=detect_backend(path))

    def close(self) -> None:
        self._db.close()

    # --- run/table/chunk registration ---
    def start_run(self, run_id: str, config_hash: str = "", fingerprint: str = "",
                  started_at: str = "") -> None:
        self._db.execute(
            "INSERT INTO runs (run_id, config_hash, source_fingerprint, started_at, status) "
            "VALUES (?,?,?,?, 'running') "
            "ON CONFLICT (run_id) DO UPDATE SET config_hash=excluded.config_hash, "
            "source_fingerprint=excluded.source_fingerprint, started_at=excluded.started_at, "
            "status=excluded.status",
            (run_id, config_hash, fingerprint, started_at),
        )
        self._db.commit()

    def get_run(self, run_id: str):
        """``(config_hash, source_fingerprint)`` for a prior run, else ``(None, None)``.

        Lets the loader detect config/source drift before trusting LOADED chunks
        from a reused run id (resume), and reset rather than silently mix plans."""
        row = self._db.execute(
            "SELECT config_hash, source_fingerprint FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def set_snapshot(self, run_id: str, token: Optional[str]) -> None:
        """Persist the source read-consistency token (e.g. Oracle SCN) for a run so
        a resume reads the SAME snapshot the original plan captured."""
        self._db.execute(
            "INSERT INTO watermarks (run_id, table_fqn, kind, value, captured_at) "
            "VALUES (?, '__run__', 'snapshot', ?, '') "
            "ON CONFLICT (run_id, table_fqn) DO UPDATE SET kind=excluded.kind, "
            "value=excluded.value, captured_at=excluded.captured_at",
            (run_id, token or ""))
        self._db.commit()

    def get_snapshot(self, run_id: str) -> Optional[str]:
        row = self._db.execute(
            "SELECT value FROM watermarks WHERE run_id=? AND table_fqn='__run__' "
            "AND kind='snapshot'", (run_id,)).fetchone()
        return row[0] if row and row[0] else None

    def snapshot_decided(self, run_id: str) -> bool:
        """Whether a snapshot decision was already recorded for this run — a row
        exists even when its value is empty (= "no snapshot available; keep
        current-read mode"). Lets a resume reuse the ORIGINAL decision instead of
        re-capturing a new, inconsistent SCN on a later chunk."""
        row = self._db.execute(
            "SELECT 1 FROM watermarks WHERE run_id=? AND table_fqn='__run__' "
            "AND kind='snapshot'", (run_id,)).fetchone()
        return row is not None

    def mark_plan_complete(self, run_id: str, complete: bool = True) -> None:
        """Record whether this run's chunk plan is FULLY written to the ledger.

        add_chunk commits per row, so a crash mid-planning (kill -9 / OOM / disk
        full) leaves a syntactically-valid but PARTIAL plan. A resume must never
        replay a partial plan as if complete — the missing tail ranges would be
        silently skipped. The loader clears this flag before it starts (re)writing
        a plan and sets it only after the last chunk row is recorded; a resume
        replays ONLY when the flag is set (else it re-plans from scratch — safe,
        because loading never starts before planning finishes). Same portable
        ``watermarks`` KV row pattern as the snapshot token."""
        self._db.execute(
            "INSERT INTO watermarks (run_id, table_fqn, kind, value, captured_at) "
            "VALUES (?, '__plan__', 'plan_complete', ?, '') "
            "ON CONFLICT (run_id, table_fqn) DO UPDATE SET kind=excluded.kind, "
            "value=excluded.value, captured_at=excluded.captured_at",
            (run_id, "1" if complete else ""))
        self._db.commit()

    def plan_complete(self, run_id: str) -> bool:
        row = self._db.execute(
            "SELECT value FROM watermarks WHERE run_id=? AND table_fqn='__plan__' "
            "AND kind='plan_complete'", (run_id,)).fetchone()
        return bool(row and row[0] == "1")

    def table_has_progress(self, run_id: str, table_fqn: str) -> bool:
        """Whether any chunk of this table left the ``pending`` state.

        Load-progress is the proof that a table's recorded plan is COMPLETE:
        loading only ever starts after ``plan()`` fully finishes (marker set),
        and chunks are never added to an already-planned table afterwards — so a
        table with a loaded/in-progress/failed chunk cannot be mid-planning.
        Used by the salvage path to keep resume progress when the plan-complete
        marker was cleared by a crash during a later planning session."""
        row = self._db.execute(
            "SELECT 1 FROM chunks WHERE run_id=? AND table_fqn=? AND state != ? "
            "LIMIT 1", (run_id, table_fqn, PENDING)).fetchone()
        return row is not None

    def delete_table_chunks(self, run_id: str, table_fqn: str) -> None:
        """Drop one table's chunk rows so it can be re-planned from the live
        source (salvage of a possibly-partial plan; only ever called for tables
        with zero progress, so no LOADED bookkeeping is lost)."""
        self._db.execute("DELETE FROM chunks WHERE run_id=? AND table_fqn=?",
                         (run_id, table_fqn))
        self._db.execute(
            "UPDATE tables SET status='pending', total_chunks=0 "
            "WHERE run_id=? AND table_fqn=?", (run_id, table_fqn))
        self._db.commit()

    def add_table(self, run_id: str, table_fqn: str, target_table: str,
                  total_rows_est: int = 0) -> None:
        self._db.execute(
            "INSERT INTO tables (run_id, table_fqn, target_table, total_rows_est, status) "
            "VALUES (?,?,?,?, 'pending') "
            "ON CONFLICT (run_id, table_fqn) DO UPDATE SET target_table=excluded.target_table, "
            "total_rows_est=excluded.total_rows_est, status=excluded.status",
            (run_id, table_fqn, target_table, total_rows_est),
        )
        self._db.commit()

    def add_chunk(self, run_id: str, table_fqn: str, chunk_id: str,
                  predicate: Optional[str] = None, lo: Optional[str] = None,
                  hi: Optional[str] = None) -> None:
        self._db.execute(
            "INSERT INTO chunks (run_id, table_fqn, chunk_id, predicate, bounds_lo, bounds_hi) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT (run_id, table_fqn, chunk_id) DO NOTHING",
            (run_id, table_fqn, chunk_id, predicate, lo, hi),
        )
        # Maintain tables.total_chunks. Count-then-set rather than a scalar
        # subquery in the UPDATE SET: equivalent under the single manifest writer
        # and portable (the embedded-Nano backend doesn't bind placeholders inside
        # an UPDATE-SET subquery).
        (n_chunks,) = self._db.execute(
            "SELECT count(*) FROM chunks WHERE run_id=? AND table_fqn=?",
            (run_id, table_fqn),
        ).fetchone()
        self._db.execute(
            "UPDATE tables SET total_chunks=? WHERE run_id=? AND table_fqn=?",
            (n_chunks, run_id, table_fqn),
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

    def get_chunks(self, run_id: str, table_fqn: Optional[str] = None) -> List[RecordedChunk]:
        """The RECORDED chunk plan for a run (all states) — predicate + bounds as
        first stored. This is the single source of truth a resume replays so a
        drifted live source can't move a chunk's range out from under the ledger;
        the loader rebuilds its in-memory plan from these rows rather than calling
        ``compute_chunks`` again. Backend-agnostic (plain SELECT), so the sqlite
        and embedded-Nano manifests replay identically. Deterministically ordered."""
        sql = ("SELECT table_fqn, chunk_id, predicate, bounds_lo, bounds_hi FROM chunks "
               "WHERE run_id=?")
        params: list = [run_id]
        if table_fqn:
            sql += " AND table_fqn=?"
            params.append(table_fqn)
        sql += " ORDER BY table_fqn, chunk_id"
        return [RecordedChunk(*r) for r in self._db.execute(sql, params).fetchall()]

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
            "INSERT INTO watermarks (run_id, table_fqn, kind, value, captured_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT (run_id, table_fqn) DO UPDATE SET "
            "kind=excluded.kind, value=excluded.value, captured_at=excluded.captured_at",
            (run_id, table_fqn, kind, value, captured_at),
        )
        self._db.commit()

    def get_watermark(self, run_id: str, table_fqn: str):
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
