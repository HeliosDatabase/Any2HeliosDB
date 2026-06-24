"""MySQL target driver (PyMySQL).

Moves data *out* of HeliosDB (or any source) into a MySQL 8 server — the
heterogeneous / migrate-back direction. MySQL is not PG-wire, so this driver
parallels :mod:`any2heliosdb.target.native_driver` (the Oracle path) rather than
the psycopg path:

* No ``COPY FROM STDIN``; bulk load is array INSERT (``executemany`` with ``%s``
  binds), and ``load_range`` is a DELETE-range + INSERT in one transaction.
* Identifiers are backtick-quoted.
* Idempotent upsert uses ``INSERT ... ON DUPLICATE KEY UPDATE``.

PyMySQL adapts ``datetime``/``bytes``/``Decimal`` natively, so the apply path
uses ordinary binds (no literal-SQL/text-coercion workarounds the psycopg path
needs for HeliosDB-Lite).
"""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence, Tuple

from ..errors import TargetConnectionError
from .base import CapabilityMatrix, TargetDriver, TargetDsn, detect_edition


def mysql_ident(name: str) -> str:
    """Backtick-quote a MySQL identifier (escaping embedded backticks)."""
    return "`{}`".format(name.replace("`", "``"))


def _mq(table: str) -> str:
    """Quote a possibly schema-qualified table name in MySQL dialect."""
    return ".".join(mysql_ident(p) for p in table.split("."))


class MySQLTargetDriver(TargetDriver):
    """Target over the MySQL wire protocol via PyMySQL."""

    dialect = "mysql"

    def __init__(self, dsn: TargetDsn) -> None:
        super().__init__(dsn)
        self._conn = None  # type: ignore[assignment]

    # --- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        import pymysql  # lazy

        try:
            self._conn = pymysql.connect(
                host=self.dsn.host, port=self.dsn.port, user=self.dsn.user,
                password=self.dsn.password or "", database=self.dsn.dbname or None,
                charset="utf8mb4", autocommit=True,
                connect_timeout=self.dsn.connect_timeout)
        except Exception as e:  # noqa: BLE001
            raise TargetConnectionError(
                "could not connect to MySQL at {}:{} as {}: {}".format(
                    self.dsn.host, self.dsn.port, self.dsn.user, e)) from e

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def conn(self):  # type: ignore[no-untyped-def]
        if self._conn is None:
            raise TargetConnectionError("driver is not connected; call connect() first")
        return self._conn

    # --- introspection ---------------------------------------------------
    def server_banner(self) -> str:
        for sql in ("SELECT VERSION()",):
            try:
                rows = self.query(sql)
                if rows and rows[0] and rows[0][0]:
                    return str(rows[0][0])
            except Exception:  # noqa: BLE001
                continue
        try:
            return str(getattr(self.conn, "get_server_info", lambda: "")() or "")
        except Exception:  # noqa: BLE001
            return ""

    def probe_capabilities(self) -> CapabilityMatrix:
        banner = self.server_banner()
        # MySQL owns the dialect on this path: no PG COPY, upsert via ON DUPLICATE
        # KEY (not ON CONFLICT), no PG MERGE/RETURNING, constraints enforced (FK
        # under InnoDB; CHECK from MySQL 8.0.16). Edition stays UNKNOWN — this is
        # not a HeliosDB target, but the orchestrator only reads copy_from_stdin.
        self.capabilities = CapabilityMatrix(
            edition=detect_edition(banner), server_version=banner, raw_banner=banner,
            copy_from_stdin=False, copy_binary=False, returning=False, on_conflict=True,
            merge=False, enforces_check=True, enforces_fk=True)
        self.capabilities.accepts = {
            "copy_from_stdin": False, "on_conflict": True, "merge": False,
            "returning": False, "enforces_check": True, "enforces_fk": True,
        }
        return self.capabilities

    # --- statement execution --------------------------------------------
    def execute(self, sql: str, params: Optional[Sequence[object]] = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql, list(params) if params else None)

    def query(self, sql: str, params: Optional[Sequence[object]] = None) -> List[Tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, list(params) if params else None)
            return [tuple(r) for r in cur.fetchall()]

    def describe_columns(self, target_table: str) -> List[str]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM {} LIMIT 0".format(_mq(target_table)))
            return [d[0] for d in (cur.description or [])]

    def select_keys(self, target_table: str, key_cols: Sequence[str]) -> List[Tuple]:
        """Return every row's key tuple — for CDC delete reconciliation."""
        cols = ", ".join(mysql_ident(c) for c in key_cols)
        with self.conn.cursor() as cur:
            cur.execute("SELECT {} FROM {}".format(cols, _mq(target_table)))
            return [tuple(r) for r in cur.fetchall()]

    def begin(self) -> None:
        self.conn.begin()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # --- bulk load -------------------------------------------------------
    def copy_rows(
        self, target_table: str, columns: Sequence[str], rows: Iterable[Sequence[object]]
    ) -> int:
        raise NotImplementedError(
            "the MySQL target has no COPY FROM STDIN; use insert_rows/load_range")

    def _insert_sql(self, target_table: str, columns: Sequence[str]) -> str:
        cols = ", ".join(mysql_ident(c) for c in columns)
        binds = ", ".join(["%s"] * len(columns))
        return "INSERT INTO {} ({}) VALUES ({})".format(_mq(target_table), cols, binds)

    def insert_rows(
        self,
        target_table: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
        on_conflict_do_nothing: bool = False,
    ) -> int:
        materialized = [tuple(r) for r in rows]
        if not materialized:
            return 0
        if on_conflict_do_nothing:
            # INSERT IGNORE skips rows that would violate a unique/PK constraint.
            sql = self._insert_sql(target_table, columns).replace(
                "INSERT INTO", "INSERT IGNORE INTO", 1)
        else:
            sql = self._insert_sql(target_table, columns)
        with self.conn.cursor() as cur:
            cur.executemany(sql, materialized)
        self.conn.commit()
        return len(materialized)

    def load_range(
        self,
        target_table: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
        where: Optional[str] = None,
        use_copy: bool = True,  # ignored: MySQL path always uses INSERT
    ) -> int:
        """Idempotently (re)load one chunk: DELETE its range then array-INSERT, in
        a single transaction. Atomic so a crashed/retried chunk never duplicates.
        """
        materialized = [tuple(r) for r in rows]
        self.conn.begin()
        try:
            with self.conn.cursor() as cur:
                cur.execute("DELETE FROM {}{}".format(
                    _mq(target_table), " WHERE {}".format(where) if where else ""))
                if materialized:
                    cur.executemany(self._insert_sql(target_table, columns), materialized)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(materialized)

    # --- CDC apply (idempotent) -----------------------------------------
    def upsert(
        self,
        target_table: str,
        key_cols: Sequence[str],
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
    ) -> int:
        """Idempotently apply full-row change records via
        ``INSERT ... ON DUPLICATE KEY UPDATE`` (the MySQL upsert). CDC records
        carry the full after-image, so every non-key column is overwritten on a
        key collision; within a batch the last record per key wins.
        """
        columns = list(columns)
        key_set = set(key_cols)
        key_idx = [columns.index(k) for k in key_cols]
        by_key: dict = {}
        for r in rows:
            t = tuple(r)
            by_key[tuple(t[i] for i in key_idx)] = t
        if not by_key:
            return 0
        non_key = [c for c in columns if c not in key_set]
        insert = self._insert_sql(target_table, columns)
        if non_key:
            sets = ", ".join("{0} = VALUES({0})".format(mysql_ident(c)) for c in non_key)
            sql = "{} ON DUPLICATE KEY UPDATE {}".format(insert, sets)
        else:
            # All columns are key columns: nothing to update, just ignore dups.
            sql = insert.replace("INSERT INTO", "INSERT IGNORE INTO", 1)
        with self.conn.cursor() as cur:
            cur.executemany(sql, list(by_key.values()))
        self.conn.commit()
        return len(by_key)

    def delete_keys(
        self, target_table: str, key_cols: Sequence[str], keys: Iterable[Sequence[object]]
    ) -> int:
        materialized = [tuple(k) for k in keys]
        if not materialized:
            return 0
        where = " AND ".join("{} = %s".format(mysql_ident(k)) for k in key_cols)
        with self.conn.cursor() as cur:
            cur.executemany("DELETE FROM {} WHERE {}".format(_mq(target_table), where), materialized)
        self.conn.commit()
        return len(materialized)

    def truncate(self, target_table: str) -> None:
        self.execute("TRUNCATE TABLE {}".format(_mq(target_table)))
