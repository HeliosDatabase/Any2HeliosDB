"""PostgreSQL-wire source adapter (psycopg v3).

Reads a PG-wire server — **HeliosDB** (the migrate-back / GoldenGate-reverse
direction) or stock PostgreSQL — and populates the canonical IR. This is the
counterpart of the psycopg *target* driver: here HeliosDB is the *source*, so
data can flow back out of it to any target (MySQL, Oracle, another PG).

Introspection prefers ``information_schema`` (portable, present on PostgreSQL and
HeliosDB-Full). HeliosDB-Lite historically ships a thin/empty
``information_schema``; when the table list comes back empty there, the adapter
falls back to ``pg_catalog`` (``pg_class``/``pg_attribute``/``pg_constraint``).
Columns carry the verbatim ``source_type`` so the emit layer can re-resolve with
user DATA_TYPE/MODIFY_TYPE overrides; types map via ``map_postgresql_type``.

Extraction streams rows through a psycopg server-side (named) cursor so large
tables don't materialize client-side.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ...constants import SourceDialect
from ...core.identifiers import quote_ident as _shared_quote_ident
from ...core.catalog_model import (
    Column,
    ForeignKey,
    Index,
    IndexColumn,
    PrimaryKey,
    Schema,
    Sequence as SequenceModel,
    Table,
    View,
)
from ...errors import IntrospectionError, SourceConnectionError
from ...typemap.defaults import map_postgresql_type
from ..base import SourceAdapter, SourceDsn

_SYS_SCHEMAS = frozenset({"pg_catalog", "information_schema", "pg_toast"})
def quote_ident(name: str) -> str:
    """Quote an identifier only when necessary, via the shared reserved-aware
    quoter (``core.identifiers``).

    Lowercased simple identifiers stay bare — which matters for HeliosDB-as-
    source: its data path resolves a bare ``employees`` / ``emp_id`` but returns
    0 rows for a *quoted* one in MIN/MAX. Reserved words (a stock-PostgreSQL
    ``order`` / ``user`` table) and mixed-case/special names are double-quoted.
    The previous local check had no reserved-word set, so a bare reserved
    identifier produced a syntax error in data reads.
    """
    return _shared_quote_ident(name)


def table_ref(schema: str, name: str) -> str:
    """A table reference for *data* queries.

    Deliberately **unqualified** (no ``schema.`` prefix): HeliosDB-Full returns
    0 rows for a schema-qualified reference but resolves the bare table name, and
    the migration always targets the source's single working schema anyway.
    """
    return quote_ident(name)


def _reconstruct_source_type(
    data_type: str,
    char_len: Optional[int],
    num_precision: Optional[int],
    num_scale: Optional[int],
) -> str:
    """Rebuild a verbatim type string from information_schema columns.

    ``data_type`` is the base name (``character varying``, ``numeric`` …);
    information_schema splits the length/precision into separate columns, so we
    fold them back so ``map_postgresql_type`` and DATA_TYPE overrides see the
    parameterised form (``VARCHAR(120)``, ``NUMERIC(10,2)`` …).
    """
    dt = (data_type or "").upper().strip()
    # HeliosDB-Full returns '' (empty string) — not NULL — for size columns it
    # doesn't track; coerce those to None so we emit bare, unconstrained types.
    char_len = _as_int(char_len)
    num_precision = _as_int(num_precision)
    num_scale = _as_int(num_scale)
    if dt in ("CHARACTER VARYING", "VARCHAR") and char_len:
        return "VARCHAR({})".format(char_len)
    if dt in ("CHARACTER VARYING", "VARCHAR"):
        return "VARCHAR"  # unbounded: emitter picks a safe wide text type
    if dt in ("CHARACTER", "CHAR", "BPCHAR") and char_len:
        return "CHAR({})".format(char_len)
    if dt in ("NUMERIC", "DECIMAL") and num_precision is not None:
        if num_scale:
            return "NUMERIC({},{})".format(num_precision, num_scale)
        return "NUMERIC({})".format(num_precision)
    if dt in ("NUMERIC", "DECIMAL"):
        # Unconstrained numeric (no precision/scale exposed): keep it bare so the
        # emitter maps it to a high-fidelity type that NEVER truncates the scale.
        return "NUMERIC"
    # timestamp/date/integer/text/bytea/etc. are self-describing.
    return dt


def _as_int(v: object) -> Optional[int]:
    """Coerce a size field to int, mapping ''/None/non-numeric to None."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


class PostgresAdapter(SourceAdapter):
    """Source adapter for a PG-wire server (HeliosDB or PostgreSQL)."""

    dialect = SourceDialect.POSTGRESQL

    def __init__(self, dsn: SourceDsn) -> None:
        super().__init__(dsn)
        self._conn: Any = None

    # --- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        import psycopg  # lazy

        # SourceDsn names the database in either .database or .service_name; map
        # whichever is set onto psycopg's dbname (default 'postgres').
        dbname = self.dsn.database or self.dsn.service_name or "postgres"
        kw: Dict[str, Any] = {
            "host": self.dsn.host, "port": self.dsn.port, "user": self.dsn.user,
            "dbname": dbname, "autocommit": True,
            # Bound the connection-establishment wait (libpq connect_timeout,
            # seconds) so a firewalled source fails fast instead of hanging.
            "connect_timeout": self.dsn.connect_timeout,
        }
        if self.dsn.password:
            kw["password"] = self.dsn.password
        try:
            self._conn = psycopg.connect(**kw)
            # Text-format params keep extraction portable across PG-wire servers.
            self._conn.prepare_threshold = None
            # Pin search_path to the source schema so the deliberately-unqualified
            # data reads (HeliosDB-as-source returns 0 rows for schema-qualified
            # refs) resolve to THIS schema — not a same-named table earlier in the
            # default search_path, which would silently read/validate wrong data.
            # Best-effort: a server that rejects it (or has no schemas) is unharmed.
            if self.dsn.schema:
                try:
                    self._conn.execute(
                        "SET search_path TO {}".format(quote_ident(self.dsn.schema)))
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            raise SourceConnectionError(
                "could not connect to PostgreSQL-wire server at {}:{} as {}: {}".format(
                    self.dsn.host, self.dsn.port, self.dsn.user, e)) from e

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    @property
    def conn(self):
        if self._conn is None:
            raise SourceConnectionError("adapter is not connected; call connect() first")
        return self._conn

    @staticmethod
    def _inline(sql: str, params: Sequence[object]) -> str:
        """Substitute ``%s`` placeholders with safely-escaped SQL **literals**.

        HeliosDB cannot answer a catalog query (``information_schema`` /
        ``pg_catalog``) that carries bind parameters — a parameterized
        ``WHERE table_schema=%s`` fails on the wire (``unexpected field count in
        "D" message``). Literal SQL works, so every catalog query inlines its
        (catalog-derived, hence trusted) string values, single-quote-escaped.
        Stock PostgreSQL accepts this form too, so the path is uniform.
        """
        out = sql
        for p in params:
            lit = "NULL" if p is None else "'{}'".format(str(p).replace("'", "''"))
            out = out.replace("%s", lit, 1)
        return out

    def _q1(self, sql: str, *params: object) -> Optional[Tuple]:
        with self.conn.cursor() as cur:
            cur.execute(self._inline(sql, params) if params else sql)
            return cur.fetchone()

    def _qall(self, sql: str, *params: object) -> List[Tuple]:
        with self.conn.cursor() as cur:
            cur.execute(self._inline(sql, params) if params else sql)
            return [tuple(r) for r in cur.fetchall()]

    def _try_qall(self, sql: str, *params: object) -> List[Tuple]:
        """Like :meth:`_qall` but swallows errors (returns ``[]``).

        Used for the catalog probes that may not exist on a thin HeliosDB-Lite
        information_schema, so the adapter can detect the gap and fall back.
        """
        try:
            return self._qall(sql, *params)
        except Exception:  # noqa: BLE001
            return []

    # --- metadata --------------------------------------------------------
    def server_version(self) -> str:
        # Prefer the startup ParameterStatus (no query needed), so version
        # detection works even when version() is unimplemented on the target.
        try:
            banner = self.conn.info.parameter_status("server_version")
            if banner:
                return banner
        except Exception:  # noqa: BLE001
            pass
        row = self._q1("SELECT version()")
        return str(row[0]) if row and row[0] else ""

    def default_schema(self) -> str:
        if self.dsn.schema:
            return self.dsn.schema
        row = self._q1("SELECT current_schema()")
        if row and row[0]:
            return str(row[0])
        return "public"

    def list_schemas(self) -> List[str]:
        rows = self._try_qall(
            "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name")
        if not rows:
            rows = self._try_qall(
                "SELECT nspname FROM pg_catalog.pg_namespace ORDER BY nspname")
        out = [r[0] for r in rows if r[0] not in _SYS_SCHEMAS and not str(r[0]).startswith("pg_")]
        return out or [self.default_schema()]

    # --- introspection ---------------------------------------------------
    def introspect_schema(self, schema: Optional[str] = None) -> Schema:
        ns = schema or self.default_schema()
        try:
            # Strategy 1 — information_schema.tables (PostgreSQL, HeliosDB-Full).
            names = [r[0] for r in self._try_qall(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name", ns)]
            strategy = "info_schema"
            if not names:
                # Strategy 2 — pg_catalog.pg_class (PostgreSQL when scoped differently).
                names = [r[0] for r in self._try_qall(
                    "SELECT c.relname FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=%s AND c.relkind='r' ORDER BY c.relname", ns)]
                if names:
                    strategy = "pg_catalog"
            if not names:
                # Strategy 3 — HeliosDB-Lite: no information_schema.tables and an
                # empty pg_class, but information_schema.columns lists the columns
                # (with a Rust-style data_type). Derive the table list from it.
                names = sorted({r[0] for r in self._try_qall(
                    "SELECT DISTINCT table_name FROM information_schema.columns "
                    "WHERE table_schema=%s", ns)})
                if names:
                    strategy = "lite_columns"
            # Drop declarative-partition CHILDREN: real PostgreSQL lists them as
            # BASE TABLEs, but the partitioned parent's SELECT already returns all
            # their rows, so migrating both would duplicate the data. No-op on
            # HeliosDB (empty/absent pg_class -> the probe returns []).
            if names:
                parts = {r[0] for r in self._try_qall(
                    "SELECT c.relname FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=%s AND c.relispartition", ns)}
                if parts:
                    names = [n for n in names if n not in parts]
            if strategy == "info_schema":
                tables = [self._table(ns, n) for n in names]
            elif strategy == "pg_catalog":
                tables = [self._table_pg_catalog(ns, n) for n in names]
            else:
                tables = [self._table_lite(ns, n) for n in names]
            views = self._views(ns)
        except Exception as e:  # noqa: BLE001
            raise IntrospectionError(
                "PostgreSQL introspection failed for {}: {}".format(ns, e)) from e
        return Schema(name=ns, tables=tables, sequences=self._sequences(ns), views=views)

    # --- introspection via information_schema (PostgreSQL / HeliosDB-Full) ---
    def _table(self, ns: str, name: str) -> Table:
        cols: List[Column] = []
        for (col, dtype, char_len, num_p, num_s, nullable, default) in self._qall(
            "SELECT column_name, data_type, character_maximum_length, "
            "numeric_precision, numeric_scale, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema=%s AND table_name=%s "
            "ORDER BY ordinal_position", ns, name):
            src = _reconstruct_source_type(dtype, char_len, num_p, num_s)
            cols.append(Column(
                name=col, data_type=map_postgresql_type(src), nullable=(nullable == "YES"),
                default=_clean_default(default), source_type=src))

        pk_cols = [r[0] for r in self._try_qall(
            "SELECT kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema "
            "WHERE tc.table_schema=%s AND tc.table_name=%s AND tc.constraint_type='PRIMARY KEY' "
            "ORDER BY kcu.ordinal_position", ns, name)]
        primary_key = PrimaryKey(columns=pk_cols) if pk_cols else None

        fks = self._foreign_keys(ns, name)
        indexes = self._indexes(ns, name)
        return Table(name=name, schema=ns, columns=cols, primary_key=primary_key,
                     foreign_keys=fks, indexes=indexes)

    def _foreign_keys(self, ns: str, name: str) -> List[ForeignKey]:
        # Pair local and referenced columns by ordinal position from
        # pg_catalog (conkey/confkey arrays). The information_schema join
        # (key_column_usage x constraint_column_usage by constraint name only)
        # makes an N x N cross-product for a COMPOSITE FK, duplicating and
        # scrambling the column lists, and can also drift column order for an FK
        # that targets a UNIQUE key rather than the PK.
        rows = self._try_qall(
            "SELECT con.conname, la.attname, rc.relname, ra.attname, k.ord "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class lc ON lc.oid = con.conrelid "
            "JOIN pg_catalog.pg_namespace ln ON ln.oid = lc.relnamespace "
            "JOIN pg_catalog.pg_class rc ON rc.oid = con.confrelid "
            "JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY "
            "  AS k(lattnum, rattnum, ord) ON true "
            "JOIN pg_catalog.pg_attribute la "
            "  ON la.attrelid = con.conrelid AND la.attnum = k.lattnum "
            "JOIN pg_catalog.pg_attribute ra "
            "  ON ra.attrelid = con.confrelid AND ra.attnum = k.rattnum "
            "WHERE ln.nspname = %s AND lc.relname = %s AND con.contype = 'f' "
            "ORDER BY con.conname, k.ord", ns, name)
        if not rows:
            # Fallback for servers without that pg_catalog surface. Single-column
            # FKs are unaffected by the cross-product, so this stays correct.
            rows = self._try_qall(
                "SELECT tc.constraint_name, kcu.column_name, ccu.table_name, "
                "  ccu.column_name, kcu.ordinal_position "
                "FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name=kcu.constraint_name AND tc.table_schema=kcu.table_schema "
                "JOIN information_schema.constraint_column_usage ccu "
                "  ON tc.constraint_name=ccu.constraint_name AND tc.table_schema=ccu.table_schema "
                "WHERE tc.table_schema=%s AND tc.table_name=%s "
                "  AND tc.constraint_type='FOREIGN KEY' "
                "ORDER BY tc.constraint_name, kcu.ordinal_position", ns, name)
        by_name: dict = {}
        for cname, lcol, rtable, rcol, _ord in rows:
            fk = by_name.get(cname)
            if fk is None:
                fk = by_name[cname] = ForeignKey(
                    name=cname, columns=[], references_table=rtable, references_columns=[])
            fk.columns.append(lcol)
            fk.references_columns.append(rcol)
        return list(by_name.values())

    def _indexes(self, ns: str, name: str) -> List[Index]:
        # Index introspection lives in pg_catalog (information_schema has no index
        # view). Skip the PK-backing index; the emitter would skip it anyway.
        rows = self._try_qall(
            "SELECT i.relname, a.attname, ix.indisunique, ix.indisprimary, "
            "  array_position(ix.indkey, a.attnum) AS pos "
            "FROM pg_catalog.pg_class t "
            "JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace "
            "JOIN pg_catalog.pg_index ix ON ix.indrelid=t.oid "
            "JOIN pg_catalog.pg_class i ON i.oid=ix.indexrelid "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(ix.indkey) "
            "WHERE n.nspname=%s AND t.relname=%s AND ix.indisprimary=false "
            "ORDER BY i.relname, pos", ns, name)
        by_name: dict = {}
        for iname, col, uniq, _isprimary, _pos in rows:
            idx = by_name.get(iname)
            if idx is None:
                idx = by_name[iname] = Index(name=iname, columns=[], unique=bool(uniq))
            idx.columns.append(IndexColumn(name=col))
        return list(by_name.values())

    # --- introspection via pg_catalog (HeliosDB-Lite fallback) ---
    def _table_pg_catalog(self, ns: str, name: str) -> Table:
        cols: List[Column] = []
        rows = self._try_qall(
            "SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod), a.attnotnull "
            "FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_class c ON c.oid=a.attrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s AND a.attnum>0 AND NOT a.attisdropped "
            "ORDER BY a.attnum", ns, name)
        if not rows:
            # Last resort: describe via the wire (SELECT * LIMIT 0) — no catalog at all.
            return self._table_describe(ns, name)
        for col, fmt_type, notnull in rows:
            src = _normalize_format_type(str(fmt_type))
            cols.append(Column(
                name=col, data_type=map_postgresql_type(src), nullable=(not notnull),
                default=None, source_type=src))

        pk_cols = [r[0] for r in self._try_qall(
            "SELECT a.attname FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class c ON c.oid=con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum=ANY(con.conkey) "
            "WHERE n.nspname=%s AND c.relname=%s AND con.contype='p' "
            "ORDER BY array_position(con.conkey, a.attnum)", ns, name)]
        primary_key = PrimaryKey(columns=pk_cols) if pk_cols else None

        fks: List[ForeignKey] = []
        for cname, lcols, rtable, rcols in self._try_qall(
            "SELECT con.conname, "
            "  (SELECT array_agg(att.attname ORDER BY x.ord) FROM unnest(con.conkey) "
            "     WITH ORDINALITY x(attnum, ord) "
            "     JOIN pg_catalog.pg_attribute att ON att.attrelid=con.conrelid AND att.attnum=x.attnum), "
            "  rc.relname, "
            "  (SELECT array_agg(att.attname ORDER BY x.ord) FROM unnest(con.confkey) "
            "     WITH ORDINALITY x(attnum, ord) "
            "     JOIN pg_catalog.pg_attribute att ON att.attrelid=con.confrelid AND att.attnum=x.attnum) "
            "FROM pg_catalog.pg_constraint con "
            "JOIN pg_catalog.pg_class c ON c.oid=con.conrelid "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_catalog.pg_class rc ON rc.oid=con.confrelid "
            "WHERE n.nspname=%s AND c.relname=%s AND con.contype='f'", ns, name):
            fks.append(ForeignKey(
                name=cname, columns=list(lcols or []), references_table=rtable,
                references_columns=list(rcols or [])))

        indexes = self._indexes(ns, name)
        return Table(name=name, schema=ns, columns=cols, primary_key=primary_key,
                     foreign_keys=fks, indexes=indexes)

    # --- introspection via Lite's information_schema.columns ---
    def _table_lite(self, ns: str, name: str) -> Table:
        """Read columns from HeliosDB-Lite's ``information_schema.columns``.

        Lite ships no ``information_schema.tables``/``table_constraints`` and an
        empty ``pg_class``, but ``information_schema.columns`` lists the columns
        with a Rust-Debug ``data_type`` (``Numeric``, ``Varchar(Some(100))``,
        ``Timestamp`` …). No PK/FK/index metadata is exposed, so those are empty
        (TEST_COUNT still validates row parity; chunking falls back to a single
        whole-table chunk). If the columns view is itself unavailable, fall back
        to a zero-row describe.
        """
        rows = self._try_qall(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", ns, name)
        if not rows:
            return self._table_describe(ns, name)
        cols: List[Column] = []
        for col, dtype, nullable in rows:
            src = _normalize_lite_type(str(dtype))
            cols.append(Column(
                name=col, data_type=map_postgresql_type(src),
                nullable=(str(nullable).upper() != "NO"), default=None, source_type=src))
        return Table(name=name, schema=ns, columns=cols)

    def _table_describe(self, ns: str, name: str) -> Table:
        """Final fallback: column names only, via a zero-row SELECT description.

        No PK/FK/index metadata is available this way; the migration still loads
        data and the structure validator (column count) passes. Logged implicitly
        as a thinner table (no constraints) for the caller to notice.
        """
        with self.conn.cursor() as cur:
            cur.execute("SELECT * FROM {} LIMIT 0".format(table_ref(ns, name)))
            names = [d.name for d in (cur.description or [])]
        from ...core.catalog_model import DataType, DataTypeKind
        cols = [Column(name=n, data_type=DataType.of(DataTypeKind.TEXT), nullable=True) for n in names]
        return Table(name=name, schema=ns, columns=cols)

    def _views(self, ns: str) -> List[View]:
        out: List[View] = []
        rows = self._try_qall(
            "SELECT table_name, view_definition FROM information_schema.views "
            "WHERE table_schema=%s ORDER BY table_name", ns)
        if not rows:
            rows = self._try_qall(
                "SELECT c.relname, pg_catalog.pg_get_viewdef(c.oid, true) "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relkind='v' ORDER BY c.relname", ns)
        for vname, vdef in rows:
            body = str(vdef or "").strip()
            if body:
                out.append(View(name=vname, schema=ns, definition=body))
        return out

    def _sequences(self, ns: str) -> List[SequenceModel]:
        """Introspect sequences (incl. SERIAL/IDENTITY-owned) so the target can
        recreate them with the correct resume point.

        Prefers ``pg_sequences`` (PG 10+ / HeliosDB-Nano >= 3.60: carries
        increment/min/max/cycle/cache), then reads each sequence's live
        ``last_value``/``is_called`` so the target START is the *next* value the
        source would hand out — post-migration inserts then can't collide with
        the rows just loaded. Falls back to ``information_schema.sequences``.
        Enumerates by name (never count(*): catalog-view count(*) reads 0 on
        HeliosDB). A server that lists no sequences -> empty list (unchanged).
        """
        rows = self._try_qall(
            "SELECT sequencename, increment_by, min_value, max_value, cycle, "
            "cache_size, start_value FROM pg_sequences WHERE schemaname=%s "
            "ORDER BY sequencename", ns)
        if not rows:
            rows = [(r[0], r[1], r[2], r[3], (str(r[4]).upper() == "YES"), 1, r[5])
                    for r in self._try_qall(
                        "SELECT sequence_name, increment, minimum_value, maximum_value, "
                        "cycle_option, start_value FROM information_schema.sequences "
                        "WHERE sequence_schema=%s ORDER BY sequence_name", ns)]
        out: List[SequenceModel] = []
        for (name, inc, minv, maxv, cycle, cache, start_value) in rows:
            inc_i = _as_int(inc) or 1
            start = _as_int(start_value) or 1
            # Resume at the next value the source would produce.
            cur = self._q1("SELECT last_value, is_called FROM {}".format(quote_ident(name)))
            if cur and cur[0] is not None:
                last = _as_int(cur[0])
                if last is not None:
                    start = (last + inc_i) if bool(cur[1]) else last
            out.append(SequenceModel(
                name=name, start=start, increment=inc_i,
                min_value=_as_int(minv), max_value=_as_int(maxv),
                cache=(_as_int(cache) or 1), cycle=bool(cycle), schema=ns))
        return out

    # --- extraction ------------------------------------------------------
    def exact_row_count(self, table: Table) -> int:
        ns = table.schema or self.default_schema()
        row = self._q1("SELECT count(*) FROM {}".format(table_ref(ns, table.name)))
        return int(row[0]) if row and row[0] is not None else 0

    def numeric_pk_bounds(self, table: Table, pk_col: str):
        ns = table.schema or self.default_schema()
        # Bare (unquoted-when-safe) column + table: HeliosDB returns NULL for a
        # quoted MIN("emp_id"), but resolves the bare form.
        row = self._q1("SELECT MIN({c}), MAX({c}) FROM {t}".format(
            c=quote_ident(pk_col), t=table_ref(ns, table.name)))
        if not row or row[0] is None:
            return None
        try:
            # floor() rather than int() (which truncates toward zero): a negative
            # fractional NUMERIC MIN like -2.75 must floor to -3 so the chunk
            # predicate `pk >= -3` still covers it. int(-2.75) == -2 skips that row.
            return math.floor(row[0]), math.floor(row[1])
        except (TypeError, ValueError):
            return None  # non-numeric PK -> caller falls back to a single chunk

    def stream_rows(
        self,
        table: Table,
        columns: Sequence[str],
        where: Optional[str] = None,
        arraysize: int = 1000,
    ) -> Iterator[Tuple]:
        ns = table.schema or self.default_schema()
        col_list = ", ".join(quote_ident(c) for c in columns)
        sql = "SELECT {} FROM {}".format(col_list, table_ref(ns, table.name))
        if where:
            sql += " WHERE {}".format(where)
        # A server-side (named) cursor would stream in batches, but HeliosDB does
        # not support DECLARE ... CURSOR ("Statement type not supported: Declare"),
        # so use an ordinary cursor and page client-side with fetchmany. psycopg
        # buffers the full result on a plain cursor; arraysize bounds the yield
        # batch, keeping the generator contract (and memory bounded for the row
        # sizes this tool moves). Stock PostgreSQL works the same way.
        with self.conn.cursor() as cur:
            cur.execute(sql)
            while True:
                batch = cur.fetchmany(arraysize)
                if not batch:
                    break
                for row in batch:
                    yield tuple(row)


def _clean_default(raw: object) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.upper() == "NULL":
        return None
    up = s.upper()
    # nextval(...) sequence defaults ARE replayed (normalized to a bare,
    # schema-unqualified nextval('seq')) so a SERIAL/IDENTITY column keeps its
    # auto-increment default on a PG-family target. The companion sequence is
    # introspected by _sequences() and created first by the orchestrator. The
    # Oracle/MySQL emitters skip a nextval default (their dialects spell
    # auto-increment differently), so migrate-back is unaffected.
    if up.startswith("NEXTVAL("):
        return _normalize_nextval(s)
    if up in ("CURRENT_TIMESTAMP", "NOW()"):
        return "CURRENT_TIMESTAMP"
    # Strip a type cast suffix Postgres adds to literals (e.g. 'x'::text, 1::integer).
    if "::" in s:
        s = s.split("::", 1)[0].strip()
    try:
        float(s)
        return s
    except ValueError:
        return None


def _normalize_nextval(default: str) -> Optional[str]:
    """Reduce a PostgreSQL ``nextval`` default to a portable ``nextval('seq')``.

    Input is e.g. ``nextval('public.actor_actor_id_seq'::regclass)``; strip the
    ``::regclass`` cast and any schema qualifier (the migration targets one
    schema and the sequence is created unqualified) so the column default matches
    the ``CREATE SEQUENCE`` name the emitter produces. Returns None if the
    sequence name can't be parsed (caller then drops the default).
    """
    m = re.search(r"nextval\(\s*'([^']+)'", default, re.IGNORECASE)
    if not m:
        return None
    seqname = m.group(1)
    if "." in seqname:
        seqname = seqname.rsplit(".", 1)[-1]
    return "nextval('{}')".format(seqname)


def _normalize_format_type(fmt: str) -> str:
    """Map ``pg_catalog.format_type`` output to the names ``map_postgresql_type``
    expects (it spells e.g. ``character varying(120)``, ``timestamp without time
    zone``, ``timestamp with time zone``)."""
    f = fmt.strip()
    low = f.lower()
    if low.startswith("timestamp with time zone"):
        return "TIMESTAMPTZ"
    if low.startswith("timestamp without time zone") or low.startswith("timestamp"):
        return "TIMESTAMP"
    if low.startswith("time with time zone") or low.startswith("time without time zone"):
        return "TIME"
    return f.upper()


_LITE_LEN = re.compile(r"Some\((\d+)\)")


def _normalize_lite_type(dtype: str) -> str:
    """Map HeliosDB-Lite's Rust-Debug ``data_type`` to a name the PG type map
    understands.

    Lite spells column types as Rust enum debug strings, e.g. ``Numeric``,
    ``Varchar(Some(100))``, ``Char(Some(1))``, ``Timestamp``, ``TimestampTz``,
    ``Text``, ``Bytea``, ``Int``/``BigInt``/``SmallInt``, ``Double``, ``Real``,
    ``Boolean``, ``Json``/``Jsonb``, ``Date``, ``Time``. The length inside
    ``Some(n)`` is preserved; an unparameterised ``Numeric`` stays bare (the
    emitter then maps it to a high-fidelity, non-truncating type)."""
    d = dtype.strip()
    head = d.split("(", 1)[0].strip().lower()
    m = _LITE_LEN.search(d)
    n = m.group(1) if m else None
    if head in ("varchar", "charactervarying"):
        return "VARCHAR({})".format(n) if n else "VARCHAR"
    if head in ("char", "character", "bpchar"):
        return "CHAR({})".format(n) if n else "CHAR"
    if head in ("numeric", "decimal"):
        # Lite does not carry precision/scale -> bare; never assume scale 0.
        return "NUMERIC"
    mapping = {
        "smallint": "SMALLINT", "int": "INTEGER", "integer": "INTEGER", "bigint": "BIGINT",
        "real": "REAL", "float": "REAL", "double": "DOUBLE PRECISION",
        "doubleprecision": "DOUBLE PRECISION", "text": "TEXT", "bytea": "BYTEA",
        "date": "DATE", "time": "TIME", "timestamp": "TIMESTAMP", "timestamptz": "TIMESTAMPTZ",
        "boolean": "BOOLEAN", "bool": "BOOLEAN", "json": "JSON", "jsonb": "JSONB",
        "uuid": "UUID", "interval": "INTERVAL",
    }
    return mapping.get(head, d.upper())
