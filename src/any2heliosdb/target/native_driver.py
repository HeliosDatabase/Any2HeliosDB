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

from typing import Iterable, List, Optional, Sequence, Tuple

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
        with self.conn.cursor() as cur:
            cur.execute("DELETE FROM {}{}".format(
                _oq(target_table), " WHERE {}".format(where) if where else ""))
            if materialized:
                cur.executemany(self._insert_sql(target_table, columns), materialized)
        self.conn.commit()
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
        with self.conn.cursor() as cur:
            cur.executemany(delete, list(by_key.keys()))
            cur.executemany(self._insert_sql(target_table, columns), list(by_key.values()))
        self.conn.commit()
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
