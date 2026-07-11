"""Portable PG-wire target driver (psycopg v3).

Works against every HeliosDB edition (Nano/Lite/Full). The tool is responsible
for translating the source dialect before handing SQL/data to this driver.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from ..core.identifiers import quote_ident, quote_table  # noqa: F401  (re-export)
from ..errors import TargetConnectionError
from .base import CapabilityMatrix, TargetDriver, TargetDsn
from .capability import probe_capabilities as _probe_caps

# Identifier quoting lives in core.identifiers so the DDL emitter, the loader,
# and the validators all share one reserved-word set and can never disagree on a
# name. quote_ident/quote_table are re-exported above for the driver's own SQL
# (load_range / copy_rows / insert_rows / upsert) and existing import sites.


def _coerce_param(v: object) -> object:
    """Coerce binary-prone Python types to a text representation before binding.

    psycopg picks the BINARY wire format for ``datetime``/``date``/``Decimal``,
    which some HeliosDB editions can't cast from a bind parameter (e.g. binary
    TIMESTAMP). Rendering them as text — which the editions already parse on the
    COPY path — keeps INSERT/upsert/delete params portable. Bytes are left as-is
    (psycopg's bytea text escaping is accepted).
    """
    if isinstance(v, _dt.datetime):
        return v.isoformat(sep=" ")
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return v


class PsycopgDriver(TargetDriver):
    """HeliosDB target over the PostgreSQL wire protocol."""

    def __init__(self, dsn: TargetDsn) -> None:
        super().__init__(dsn)
        self._conn: Any = None

    # --- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        import psycopg  # lazy

        try:
            self._conn = psycopg.connect(autocommit=True, **self.dsn.conninfo_kwargs())
            # Disable client-side prepared statements. Once psycopg auto-prepares
            # (after prepare_threshold executes), it sends params in BINARY, whose
            # encoding is a portability minefield across PG-wire implementations
            # (e.g. binary DATE/TIMESTAMP some HeliosDB editions can't cast). COPY
            # is the bulk fast path regardless; INSERT params then travel as text.
            self._conn.prepare_threshold = None
        except Exception as e:  # noqa: BLE001
            raise TargetConnectionError(
                "could not connect to HeliosDB at {}:{} as {}: {}".format(
                    self.dsn.host, self.dsn.port, self.dsn.user, e
                )
            ) from e

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            raise TargetConnectionError("driver is not connected; call connect() first")
        return self._conn

    # --- introspection ---------------------------------------------------
    def server_banner(self) -> str:
        # Robust: read the startup ParameterStatus (no query needed), so version
        # detection works even when version() is unimplemented on the target.
        banner = self.conn.info.parameter_status("server_version")
        return banner or ""

    def probe_capabilities(self) -> CapabilityMatrix:
        # Probe on a dedicated connection so its throwaway DDL/COPY can never
        # leave residue (open COPY, aborted pipeline) on the data connection.
        import psycopg  # lazy

        banner = self.server_banner()
        probe_conn = psycopg.connect(autocommit=True, **self.dsn.conninfo_kwargs())
        probe_conn.prepare_threshold = None
        try:
            self.capabilities = _probe_caps(probe_conn, banner)
        finally:
            probe_conn.close()
        return self.capabilities

    # --- statement execution --------------------------------------------
    def execute(self, sql: str, params: Optional[Sequence[object]] = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)

    def query(self, sql: str, params: Optional[Sequence[object]] = None) -> List[Tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def ping(self) -> None:
        """Liveness probe for the CDC poison-vs-outage decision (SELECT 1)."""
        self.query("SELECT 1")

    @staticmethod
    def _exec_each(cur, stmt: str, param_sets) -> int:
        """Run ``stmt`` once per parameter set with single ``execute`` calls.

        psycopg's ``executemany`` runs in pipeline mode, which some HeliosDB
        editions cannot consume (the server drops the connection). Per-row
        ``execute`` keeps each statement on the synchronous simple/extended path.
        COPY remains the bulk fast path; this is for CDC apply and INSERT
        fallbacks, which are row-at-a-time or small-batch anyway.
        """
        n = 0
        for p in param_sets:
            cur.execute(stmt, tuple(_coerce_param(v) for v in p))
            n += 1
        return n

    def describe_columns(self, target_table: str) -> List[str]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM {} LIMIT 0".format(quote_table(target_table)))
            return [d.name for d in (cur.description or [])]

    def select_keys(self, target_table: str, key_cols: Sequence[str]) -> List[Tuple]:
        """Return every row's key tuple — for CDC delete reconciliation."""
        cols = ", ".join(quote_ident(c) for c in key_cols)
        with self.conn.cursor() as cur:
            cur.execute("SELECT {} FROM {}".format(cols, quote_table(target_table)))
            return [tuple(r) for r in cur.fetchall()]

    def begin(self) -> None:
        if self.conn.autocommit:
            self.conn.autocommit = False

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # --- bulk load -------------------------------------------------------
    def copy_rows(
        self, target_table: str, columns: Sequence[str], rows: Iterable[Sequence[object]]
    ) -> int:
        cols = ", ".join(quote_ident(c) for c in columns)
        stmt = "COPY {} ({}) FROM STDIN".format(quote_table(target_table), cols)
        n = 0
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                with cur.copy(stmt) as cp:
                    for r in rows:
                        cp.write_row(tuple(r))
                        n += 1
        return n

    def load_range(
        self,
        target_table: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
        where: Optional[str] = None,
        use_copy: bool = True,
    ) -> int:
        """Idempotently (re)load one chunk: DELETE its range then load, in a
        single transaction. Atomic so a crashed/retried chunk never duplicates,
        and one BEGIN/COMMIT avoids autocommit-then-BEGIN transaction conflicts.
        """
        qt = quote_table(target_table)
        cols = ", ".join(quote_ident(c) for c in columns)
        delete = "DELETE FROM {}{}".format(qt, " WHERE {}".format(where) if where else "")
        n = 0
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(delete)
                if use_copy:
                    with cur.copy("COPY {} ({}) FROM STDIN".format(qt, cols)) as cp:
                        for r in rows:
                            cp.write_row(tuple(r))
                            n += 1
                else:
                    placeholders = "(" + ", ".join(["%s"] * len(columns)) + ")"
                    stmt = "INSERT INTO {} ({}) VALUES {}".format(qt, cols, placeholders)
                    n = self._exec_each(cur, stmt, [tuple(r) for r in rows])
        return n

    def insert_rows(
        self,
        target_table: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
        on_conflict_do_nothing: bool = False,
    ) -> int:
        cols = ", ".join(quote_ident(c) for c in columns)
        placeholders = "(" + ", ".join(["%s"] * len(columns)) + ")"
        stmt = "INSERT INTO {} ({}) VALUES {}".format(
            quote_table(target_table), cols, placeholders
        )
        if on_conflict_do_nothing:
            stmt += " ON CONFLICT DO NOTHING"
        materialized = [tuple(r) for r in rows]
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                self._exec_each(cur, stmt, materialized)
        return len(materialized)

    # --- CDC apply (idempotent) -----------------------------------------
    def upsert(
        self,
        target_table: str,
        key_cols: Sequence[str],
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
    ) -> int:
        """Idempotently apply full-row change records keyed by ``key_cols``.

        Implemented as ``INSERT ... ON CONFLICT (key) DO UPDATE`` rendered as
        safely-escaped **literal SQL** (psycopg.sql) rather than bind parameters.
        Two reasons for both choices:

        * Literal SQL: HeliosDB-Lite ignores ON CONFLICT *and* a parameterized
          WHERE when the values are bind params; with literals it behaves
          correctly across editions.
        * ON CONFLICT (vs DELETE+INSERT): updating a row in place is FK-safe.
          Deleting a parent row to re-insert it trips an enforced foreign key on
          targets that check immediately (HeliosDB-Lite).

        CDC records carry the full after-image, so the conflict action overwrites
        every non-key column; within a batch the last record per key wins.
        """
        from psycopg import sql

        columns = list(columns)
        key_set = set(key_cols)
        key_idx = [columns.index(k) for k in key_cols]
        by_key: dict = {}
        for r in rows:
            t = tuple(r)
            by_key[tuple(t[i] for i in key_idx)] = t
        if not by_key:
            return 0
        table = sql.SQL(quote_table(target_table))  # quote_table already escapes
        col_list = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
        conflict = sql.SQL(", ").join(sql.Identifier(c) for c in key_cols)
        non_key = [c for c in columns if c not in key_set]
        action: sql.Composable
        if non_key:
            sets = sql.SQL(", ").join(
                sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(c)) for c in non_key)
            action = sql.SQL("DO UPDATE SET {}").format(sets)
        else:
            action = sql.SQL("DO NOTHING")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                for row in by_key.values():
                    vals = sql.SQL(", ").join(sql.Literal(v) for v in row)
                    cur.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) {}").format(
                        table, col_list, vals, conflict, action))
        return len(by_key)

    def update_columns(
        self,
        target_table: str,
        key_cols: Sequence[str],
        set_cols: Sequence[str],
        rows: Iterable[Sequence[object]],
    ) -> int:
        """Keyed column-subset UPDATE; returns the rows actually matched.

        Rendered as literal SQL for the same reason as :meth:`upsert` /
        :meth:`delete_keys` (parameterized WHERE/SET values match nothing on
        HeliosDB-Lite). Each row is ``(*set-values, *key-values)`` in SQL order.
        Only ``set_cols`` are written, so an omitted (e.g. unchanged-TOAST) column
        keeps its stored value — the key correctness gain over ``upsert`` on the
        DELETE+INSERT drivers.
        """
        from psycopg import sql

        set_cols = list(set_cols)
        materialized = [tuple(r) for r in rows]
        if not materialized or not set_cols:
            return 0
        nset = len(set_cols)
        table = sql.SQL(quote_table(target_table))
        updated = 0
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                for r in materialized:
                    setvals, keyvals = r[:nset], r[nset:]
                    sets = sql.SQL(", ").join(
                        sql.SQL("{} = {}").format(sql.Identifier(c), sql.Literal(v))
                        for c, v in zip(set_cols, setvals))
                    conds = sql.SQL(" AND ").join(
                        sql.SQL("{} = {}").format(sql.Identifier(kc), sql.Literal(v))
                        for kc, v in zip(key_cols, keyvals))
                    cur.execute(sql.SQL("UPDATE {} SET {} WHERE {}").format(table, sets, conds))
                    if cur.rowcount and cur.rowcount > 0:
                        updated += cur.rowcount
        return updated

    def delete_keys(
        self, target_table: str, key_cols: Sequence[str], keys: Iterable[Sequence[object]]
    ) -> int:
        # Literal SQL (not bind params): a parameterized WHERE matches nothing on
        # HeliosDB-Lite today. See upsert() for the rationale.
        from psycopg import sql

        materialized = [tuple(k) for k in keys]
        if not materialized:
            return 0
        # Return rows ACTUALLY deleted (summed cur.rowcount), not the number of
        # keys attempted. A target that silently no-ops a delete (e.g. a build whose
        # DELETE predicate fails to match while SELECT does) then surfaces as a
        # count shortfall the caller/validation can catch, instead of a false
        # "deleted N". rowcount is -1 only when the server reports no tag count.
        deleted = 0
        table = sql.SQL(quote_table(target_table))
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                for k in materialized:
                    conds = sql.SQL(" AND ").join(
                        sql.SQL("{} = {}").format(sql.Identifier(kc), sql.Literal(v))
                        for kc, v in zip(key_cols, k))
                    cur.execute(sql.SQL("DELETE FROM {} WHERE {}").format(table, conds))
                    if cur.rowcount and cur.rowcount > 0:
                        deleted += cur.rowcount
        return deleted

    def truncate(self, target_table: str) -> None:
        self.execute("TRUNCATE TABLE {}".format(quote_table(target_table)))
