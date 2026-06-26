"""Native (Oracle-wire) target driver.

Connects to HeliosDB through its **Oracle** protocol (``oracledb`` thin mode),
so HeliosDB performs the dialect translation and the tool sends near-passthrough
Oracle SQL/DDL — the "transform almost nothing" path. Differences from the
psycopg driver:

* No ``COPY FROM STDIN``; bulk load is array INSERT (``executemany``).
* Identifiers keep their source (upper) case, double-quoted.
* Bind placeholders are ``:1, :2`` (Oracle style). Oracle binds are reliable in
  WHERE clauses and ``oracledb`` adapts ``datetime``/``bytes`` natively, so the
  apply path uses ordinary binds (no literal-SQL/text-coercion workarounds the
  psycopg path needs for HeliosDB-Lite).

Availability is edition/protocol-gated (Oracle listener = Lite/Full). The
psycopg driver stays the portable default across every edition.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple

from ..emit.oracle_ddl import oracle_ident
from ..errors import TargetConnectionError
from .base import CapabilityMatrix, TargetDriver, TargetDsn, detect_edition


def _oq(table: str) -> str:
    """Quote a possibly schema-qualified table name in Oracle dialect."""
    return ".".join(oracle_ident(p) for p in table.split("."))


class NativeOracleDriver(TargetDriver):
    """HeliosDB target over the Oracle (TNS/TTC) wire protocol via oracledb."""

    dialect = "oracle"

    def __init__(self, dsn: TargetDsn) -> None:
        super().__init__(dsn)
        self._conn = None  # type: ignore[assignment]

    # --- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        import oracledb  # lazy

        dsn = "{}:{}/{}".format(self.dsn.host, self.dsn.port, self.dsn.dbname)
        try:
            self._conn = oracledb.connect(
                user=self.dsn.user, password=self.dsn.password or "heliosdb", dsn=dsn)
            self._conn.autocommit = True
            # Bound every round-trip. Two reasons: (1) a native migrate must never
            # hang forever on a stalled server response, and (2) setting
            # call_timeout switches oracledb thin to a timeout-driven read loop that
            # is resilient to HeliosDB's TTC response framing — without it the bulk
            # array-INSERT round-trip can block indefinitely (the DDL/SELECT path is
            # unaffected). 120s is generous for one array-INSERT batch yet still
            # fails fast on a true stall.
            try:
                self._conn.call_timeout = 300_000  # ms; generous safety net for a data round-trip
            except Exception:  # noqa: BLE001 -- very old oracledb without call_timeout
                pass
        except Exception as e:  # noqa: BLE001
            raise TargetConnectionError(
                "could not connect to HeliosDB Oracle listener at {} as {}: {}".format(
                    dsn, self.dsn.user, e)) from e

    def close(self) -> None:
        if self._conn is not None:
            try:
                # HeliosDB's Oracle listener may not answer oracledb's graceful
                # logoff/close handshake, which would otherwise block until the
                # call_timeout (the data is already committed). Cap the close wait
                # short and never let a close-handshake stall fail an
                # otherwise-successful migration.
                try:
                    self._conn.call_timeout = 5_000
                except Exception:  # noqa: BLE001
                    pass
                self._conn.close()
            except Exception:  # noqa: BLE001 -- close stall after committed data is harmless
                pass
            finally:
                self._conn = None

    @property
    def conn(self):  # type: ignore[no-untyped-def]
        if self._conn is None:
            raise TargetConnectionError("driver is not connected; call connect() first")
        return self._conn

    # --- introspection ---------------------------------------------------
    def server_banner(self) -> str:
        for sql in ("SELECT banner FROM v$version WHERE ROWNUM = 1",
                    "SELECT banner_full FROM v$version WHERE ROWNUM = 1"):
            try:
                rows = self.query(sql)
                if rows and rows[0] and rows[0][0]:
                    return str(rows[0][0])
            except Exception:  # noqa: BLE001
                continue
        try:
            return self.conn.version or ""
        except Exception:  # noqa: BLE001
            return ""

    def probe_capabilities(self) -> CapabilityMatrix:
        banner = self.server_banner()
        # On the native path HeliosDB owns the dialect, so capabilities reflect
        # the Oracle surface: no PG COPY, MERGE/RETURNING available, constraints
        # enforced. The PL/SQL rewrite layer is a no-op here.
        self.capabilities = CapabilityMatrix(
            edition=detect_edition(banner), server_version=banner, raw_banner=banner,
            copy_from_stdin=False, copy_binary=False, returning=True, on_conflict=False,
            merge=True, enforces_check=True, enforces_fk=True)
        return self.capabilities

    # --- statement execution --------------------------------------------
    def execute(self, sql: str, params: Optional[Sequence[object]] = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql, list(params) if params else [])

    def query(self, sql: str, params: Optional[Sequence[object]] = None) -> List[Tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, list(params) if params else [])
            return list(cur.fetchall())

    def describe_columns(self, target_table: str) -> List[str]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM {} WHERE ROWNUM < 1".format(_oq(target_table)))
            return [d[0] for d in (cur.description or [])]

    def select_keys(self, target_table: str, key_cols: Sequence[str]) -> List[Tuple]:
        """Return every row's key tuple — for CDC delete reconciliation."""
        cols = ", ".join(oracle_ident(c) for c in key_cols)
        with self.conn.cursor() as cur:
            cur.execute("SELECT {} FROM {}".format(cols, _oq(target_table)))
            return [tuple(r) for r in cur.fetchall()]

    def begin(self) -> None:
        self.conn.autocommit = False

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    @contextmanager
    def _atomic(self) -> Iterator[None]:
        """Run a DELETE+INSERT critical section as one all-or-nothing transaction.

        connect() opens the connection with ``autocommit = True``, which makes a
        bare DELETE commit the instant it executes. A DELETE-then-INSERT pair
        (load_range / upsert) therefore loses data if the INSERT fails after the
        DELETE has already auto-committed: the range is emptied but never
        repopulated. Wrap the pair so autocommit is disabled for the duration,
        the work is committed only once both statements succeed, and any failure
        rolls the DELETE back. The connection's previous autocommit state is
        always restored.
        """
        conn = self.conn
        prev_autocommit = getattr(conn, "autocommit", True)
        conn.autocommit = False
        try:
            yield
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 -- surface the original failure below
                pass
            raise
        finally:
            conn.autocommit = prev_autocommit

    # --- bulk load -------------------------------------------------------
    def copy_rows(
        self, target_table: str, columns: Sequence[str], rows: Iterable[Sequence[object]]
    ) -> int:
        raise NotImplementedError(
            "the native Oracle path has no COPY FROM STDIN; use insert_rows/load_range")

    def _insert_sql(self, target_table: str, columns: Sequence[str]) -> str:
        cols = ", ".join(oracle_ident(c) for c in columns)
        binds = ", ".join(":{}".format(i + 1) for i in range(len(columns)))
        return "INSERT INTO {} ({}) VALUES ({})".format(_oq(target_table), cols, binds)

    def _bind_timestamps(self, cur, columns, rows) -> None:  # type: ignore[no-untyped-def]
        """Force datetime binds to TIMESTAMP so fractional seconds survive.

        python-oracledb defaults a ``datetime`` bind to ``DB_TYPE_DATE`` (the
        7-byte Oracle date, no fractional seconds), which silently truncates
        sub-second precision *client-side* before it ever reaches HeliosDB. The
        type map sends both Oracle DATE and TIMESTAMP to a TIMESTAMP column, so
        bind every datetime position as ``DB_TYPE_TIMESTAMP``; other positions
        keep oracledb's own inference (``None``).
        """
        import datetime as _dt

        # Find datetime positions first (pure Python): a rowset with no datetime
        # never imports oracledb, which keeps the hermetic mock tests (and any
        # oracledb-free environment) working. oracledb is always present on the
        # real connect() path, so the guarded import below only no-ops in tests.
        ts_cols = []
        for i in range(len(columns)):
            for r in rows:
                v = r[i]
                if v is not None:
                    if isinstance(v, _dt.datetime):
                        ts_cols.append(i)
                    break
        if not ts_cols:
            return
        try:
            import oracledb
        except ImportError:  # pragma: no cover - the native path always has oracledb
            return
        sizes = [oracledb.DB_TYPE_TIMESTAMP if i in ts_cols else None
                 for i in range(len(columns))]
        cur.setinputsizes(*sizes)

    def insert_rows(
        self,
        target_table: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
        on_conflict_do_nothing: bool = False,
    ) -> int:
        materialized = [tuple(r) for r in rows]
        if materialized:
            with self.conn.cursor() as cur:
                self._bind_timestamps(cur, columns, materialized)
                cur.executemany(self._insert_sql(target_table, columns), materialized)
            self.conn.commit()
        return len(materialized)

    def load_range(
        self,
        target_table: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
        where: Optional[str] = None,
        use_copy: bool = True,  # ignored: Oracle path always uses INSERT
    ) -> int:
        materialized = [tuple(r) for r in rows]
        # DELETE + INSERT must be one atomic unit: under the connection's default
        # autocommit the DELETE would commit immediately, so a failing INSERT
        # would leave the range emptied (silent data loss). See _atomic().
        with self._atomic():
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM {}{}".format(
                    _oq(target_table), " WHERE {}".format(where) if where else ""))
                if materialized:
                    self._bind_timestamps(cur, columns, materialized)
                    cur.executemany(self._insert_sql(target_table, columns), materialized)
        return len(materialized)

    # --- CDC apply (idempotent) -----------------------------------------
    def upsert(
        self,
        target_table: str,
        key_cols: Sequence[str],
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
    ) -> int:
        columns = list(columns)
        key_idx = [columns.index(k) for k in key_cols]
        by_key: dict = {}
        for r in rows:
            t = tuple(r)
            by_key[tuple(t[i] for i in key_idx)] = t
        if not by_key:
            return 0
        where = " AND ".join("{} = :{}".format(oracle_ident(k), i + 1) for i, k in enumerate(key_cols))
        delete = "DELETE FROM {} WHERE {}".format(_oq(target_table), where)
        # Atomic DELETE + INSERT for the same reason as load_range(): a failed
        # re-insert after the per-key DELETE must not drop those rows.
        with self._atomic():
            with self.conn.cursor() as cur:
                cur.executemany(delete, list(by_key.keys()))
                values = list(by_key.values())
                self._bind_timestamps(cur, columns, values)
                cur.executemany(self._insert_sql(target_table, columns), values)
        return len(by_key)

    def delete_keys(
        self, target_table: str, key_cols: Sequence[str], keys: Iterable[Sequence[object]]
    ) -> int:
        materialized = [tuple(k) for k in keys]
        if not materialized:
            return 0
        where = " AND ".join("{} = :{}".format(oracle_ident(k), i + 1) for i, k in enumerate(key_cols))
        with self.conn.cursor() as cur:
            cur.executemany("DELETE FROM {} WHERE {}".format(_oq(target_table), where), materialized)
        self.conn.commit()
        return len(materialized)

    def truncate(self, target_table: str) -> None:
        self.execute("TRUNCATE TABLE {}".format(_oq(target_table)))
