"""SQL Server (MSSQL) source adapter (pyodbc).

Introspects the SQL Server catalog (``sys.*`` + ``INFORMATION_SCHEMA``) into the
canonical IR and streams table data for the loader. Identifiers are quoted with
``[brackets]`` (the SQL Server delimited-identifier form), so reserved words and
the chunker's range predicates round-trip regardless of the session's
``QUOTED_IDENTIFIER`` setting.

A SQL Server instance nests *database* → *schema* (``dbo`` by default) → table.
The connection binds to a single database (``SourceDsn.database``); the IR
``schema`` is the SQL Server schema name. Columns carry the verbatim
``source_type`` rebuilt with length/precision/scale (``NVARCHAR(100)``,
``DECIMAL(10,2)``, ``VARBINARY(MAX)`` …) so :func:`map_mssql_type` matches and
the emit layer can re-resolve with user DATA_TYPE/MODIFY_TYPE overrides.
"""
from __future__ import annotations

from typing import Iterator, List, Optional, Sequence, Tuple

from ...constants import SourceDialect
from ...core.catalog_model import (
    Column,
    ForeignKey,
    Index,
    IndexColumn,
    PrimaryKey,
    Schema,
    Table,
    View,
)
from ...errors import IntrospectionError, SourceConnectionError
from ...typemap.defaults import map_mssql_type
from ..base import SourceAdapter, SourceDsn

# Schemas SQL Server ships built-in; never migrated.
_SYS_SCHEMAS = frozenset(
    {
        "sys",
        "INFORMATION_SCHEMA",
        "guest",
        "db_owner",
        "db_accessadmin",
        "db_securityadmin",
        "db_ddladmin",
        "db_backupoperator",
        "db_datareader",
        "db_datawriter",
        "db_denydatareader",
        "db_denydatawriter",
    }
)

# Character types whose length is a count of characters/bytes (length -1 == MAX).
_LEN_TYPES = frozenset(
    {"VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "BINARY", "VARBINARY"}
)
# Types that carry numeric precision/scale.
_NUMERIC_TYPES = frozenset({"DECIMAL", "NUMERIC"})


def quote_mssql(name: str) -> str:
    """Quote a single SQL Server identifier with brackets (``]`` doubled)."""
    return "[{}]".format(name.replace("]", "]]"))


def quote_mssql_table(schema: str, name: str) -> str:
    return "{}.{}".format(quote_mssql(schema), quote_mssql(name))


def _reconstruct_source_type(
    data_type: str,
    char_max_length: Optional[int],
    numeric_precision: Optional[int],
    numeric_scale: Optional[int],
) -> str:
    """Rebuild a verbatim type string from ``sys.columns`` metadata.

    SQL Server splits the parameters across separate columns; fold them back so
    ``map_mssql_type`` and DATA_TYPE overrides see the parameterised form
    (``VARCHAR(120)``, ``NVARCHAR(MAX)``, ``DECIMAL(10,2)`` …). For ``N``-prefixed
    character types ``sys.columns.max_length`` counts *bytes*, so a halved value
    is the character count (``-1`` is the sentinel for MAX/unbounded).
    """
    dt = (data_type or "").upper().strip()
    if dt in _LEN_TYPES:
        if char_max_length is None:
            return dt
        if char_max_length == -1:
            return "{}(MAX)".format(dt)
        return "{}({})".format(dt, char_max_length)
    if dt in _NUMERIC_TYPES:
        if numeric_precision is None:
            return dt
        scale = numeric_scale if numeric_scale is not None else 0
        return "{}({},{})".format(dt, numeric_precision, scale)
    # int/bigint/bit/datetime2/uniqueidentifier/money/text/xml/etc. are self-describing.
    return dt


def _is_integer_source(source_type: Optional[str]) -> bool:
    """True when a column's source type is an integer family (chunkable PK)."""
    if not source_type:
        return False
    return source_type.upper().split("(", 1)[0].strip() in (
        "TINYINT",
        "SMALLINT",
        "INT",
        "INTEGER",
        "BIGINT",
    )


def _default(raw: object) -> Optional[str]:
    """Normalize a SQL Server column default.

    Definitions arrive wrapped in parentheses (often doubled, e.g. ``((0))`` or
    ``(getdate())``). Keep only simple numeric literals and the portable
    CURRENT_TIMESTAMP; drop string/expression defaults (the data carries the
    values, and replaying a T-SQL expression risks cross-dialect breakage).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    # Strip the balanced wrapping parentheses SQL Server stores around defaults.
    while len(s) >= 2 and s[0] == "(" and s[-1] == ")":
        s = s[1:-1].strip()
    if not s:
        return None
    up = s.upper()
    if up in ("GETDATE()", "SYSDATETIME()", "CURRENT_TIMESTAMP", "GETUTCDATE()", "SYSUTCDATETIME()"):
        return "CURRENT_TIMESTAMP"
    # Numeric literal? (optionally a leading sign / decimal point)
    try:
        float(s)
        return s
    except ValueError:
        return None


class MSSQLAdapter(SourceAdapter):
    """SQL Server source adapter (pyodbc), Oracle/MySQL-parity."""

    dialect = SourceDialect.MSSQL

    def __init__(self, dsn: SourceDsn) -> None:
        super().__init__(dsn)
        self._conn = None  # type: ignore[assignment]

    # --- lifecycle -------------------------------------------------------
    def _database(self) -> str:
        return self.dsn.database or "master"

    def _connection_string(self) -> str:
        """Build an ODBC connection string.

        Picks the newest installed Microsoft ODBC driver, falling back to the
        generic name. ``TrustServerCertificate=yes`` keeps a default-TLS SQL
        Server 2022 reachable without provisioning a CA in the migration host.
        """
        try:
            import pyodbc  # lazy

            drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
        except Exception:  # noqa: BLE001
            drivers = []
        # Prefer the highest-numbered "ODBC Driver NN for SQL Server".
        driver = None
        best = -1
        for d in drivers:
            digits = "".join(ch for ch in d if ch.isdigit())
            n = int(digits) if digits else 0
            if "ODBC Driver" in d and n >= best:
                best, driver = n, d
        if driver is None and drivers:
            driver = drivers[0]
        if driver is None:
            driver = "ODBC Driver 18 for SQL Server"
        parts = [
            "DRIVER={{{}}}".format(driver),
            "SERVER={},{}".format(self.dsn.host, self.dsn.port),
            "DATABASE={}".format(self._database()),
            "UID={}".format(self.dsn.user),
            "PWD={}".format(self.dsn.password),
            "TrustServerCertificate=yes",
            "Encrypt=optional",
        ]
        return ";".join(parts)

    def connect(self) -> None:
        import pyodbc  # lazy

        try:
            self._conn = pyodbc.connect(self._connection_string(), autocommit=True)
        except Exception as e:  # noqa: BLE001
            raise SourceConnectionError(
                "could not connect to SQL Server at {}:{} as {}: {}".format(
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
            raise SourceConnectionError("adapter is not connected; call connect() first")
        return self._conn

    def _q1(self, sql: str, *params: object) -> Optional[Tuple]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql, *params) if params else cur.execute(sql)
            row = cur.fetchone()
            return tuple(row) if row is not None else None
        finally:
            cur.close()

    def _qall(self, sql: str, *params: object) -> List[Tuple]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql, *params) if params else cur.execute(sql)
            return [tuple(r) for r in cur.fetchall()]
        finally:
            cur.close()

    # --- metadata --------------------------------------------------------
    def server_version(self) -> str:
        row = self._q1("SELECT @@VERSION")
        return str(row[0]).strip() if row and row[0] else ""

    def default_schema(self) -> str:
        if self.dsn.schema:
            return self.dsn.schema
        # SCHEMA_NAME() is the caller's default schema; 'dbo' when unset.
        row = self._q1("SELECT SCHEMA_NAME()")
        return str(row[0]) if row and row[0] else "dbo"

    def list_schemas(self) -> List[str]:
        rows = self._qall(
            "SELECT name FROM sys.schemas ORDER BY name")
        return [r[0] for r in rows if r[0] not in _SYS_SCHEMAS and not str(r[0]).startswith("db_")]

    # --- introspection ---------------------------------------------------
    def introspect_schema(self, schema: Optional[str] = None) -> Schema:
        ns = schema or self.default_schema()
        try:
            names = [r[0] for r in self._qall(
                "SELECT t.name FROM sys.tables t "
                "JOIN sys.schemas s ON s.schema_id = t.schema_id "
                "WHERE s.name = ? ORDER BY t.name", ns)]
            tables = [self._table(ns, name) for name in names]
            views = self._views(ns)
        except Exception as e:  # noqa: BLE001
            raise IntrospectionError(
                "SQL Server introspection failed for {}: {}".format(ns, e)) from e
        return Schema(name=ns, tables=tables, sequences=[], views=views)

    def _table(self, ns: str, name: str) -> Table:
        cols: List[Column] = []
        # sys.columns + sys.types: max_length/precision/scale rebuild the source
        # type; ORDER BY column_id preserves declaration order.
        for (col, type_name, max_len, precision, scale, is_nullable, default_def) in self._qall(
            "SELECT c.name, ty.name, c.max_length, c.precision, c.scale, "
            "       c.is_nullable, dc.definition "
            "FROM sys.columns c "
            "JOIN sys.types ty ON ty.user_type_id = c.user_type_id "
            "JOIN sys.tables t ON t.object_id = c.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "LEFT JOIN sys.default_constraints dc ON dc.object_id = c.default_object_id "
            "WHERE s.name = ? AND t.name = ? ORDER BY c.column_id", ns, name):
            char_len = self._char_length(str(type_name), max_len)
            src = _reconstruct_source_type(str(type_name), char_len, precision, scale)
            cols.append(Column(
                name=col, data_type=map_mssql_type(src), nullable=bool(is_nullable),
                default=_default(default_def), source_type=src))

        primary_key = self._primary_key(ns, name)
        fks = self._foreign_keys(ns, name)
        indexes = self._indexes(ns, name)
        return Table(name=name, schema=ns, columns=cols, primary_key=primary_key,
                     foreign_keys=fks, indexes=indexes)

    @staticmethod
    def _char_length(type_name: str, max_length: Optional[int]) -> Optional[int]:
        """Convert ``sys.columns.max_length`` (bytes) to a character count.

        ``-1`` (MAX) passes through unchanged; ``N``-prefixed Unicode types store
        two bytes per character, so the character count is ``max_length / 2``.
        Non-character types report a meaningless length here, but
        :func:`_reconstruct_source_type` only consults it for ``_LEN_TYPES``.
        """
        if max_length is None:
            return None
        n = int(max_length)
        if n == -1:
            return -1
        if type_name.upper() in ("NVARCHAR", "NCHAR"):
            return n // 2
        return n

    def _primary_key(self, ns: str, name: str) -> Optional[PrimaryKey]:
        pk_cols = [r[0] for r in self._qall(
            "SELECT col.name FROM sys.indexes i "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
            "JOIN sys.columns col ON col.object_id = ic.object_id AND col.column_id = ic.column_id "
            "JOIN sys.tables t ON t.object_id = i.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = ? AND t.name = ? AND i.is_primary_key = 1 "
            "ORDER BY ic.key_ordinal", ns, name)]
        return PrimaryKey(columns=pk_cols) if pk_cols else None

    def _foreign_keys(self, ns: str, name: str) -> List[ForeignKey]:
        # sys.foreign_key_columns gives the *actual* referenced columns (not the
        # assumed PK), with referenced schema/table resolved via OBJECT_*.
        rows = self._qall(
            "SELECT fk.name, "
            "       pc.name AS parent_col, "
            "       OBJECT_NAME(fkc.referenced_object_id) AS ref_table, "
            "       rc.name AS ref_col "
            "FROM sys.foreign_keys fk "
            "JOIN sys.foreign_key_columns fkc ON fkc.constraint_object_id = fk.object_id "
            "JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id "
            "  AND pc.column_id = fkc.parent_column_id "
            "JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id "
            "  AND rc.column_id = fkc.referenced_column_id "
            "JOIN sys.tables t ON t.object_id = fk.parent_object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = ? AND t.name = ? "
            "ORDER BY fk.name, fkc.constraint_column_id", ns, name)
        by_name: dict = {}
        for cname, lcol, rtable, rcol in rows:
            fk = by_name.get(cname)
            if fk is None:
                fk = by_name[cname] = ForeignKey(
                    name=cname, columns=[], references_table=rtable, references_columns=[])
            fk.columns.append(lcol)
            fk.references_columns.append(rcol)
        return list(by_name.values())

    def _indexes(self, ns: str, name: str) -> List[Index]:
        # Skip the PK-backing index (the emitter would skip it anyway) and any
        # heap/system rows (index_id 0). is_unique distinguishes UNIQUE indexes.
        rows = self._qall(
            "SELECT i.name, col.name, i.is_unique "
            "FROM sys.indexes i "
            "JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id "
            "JOIN sys.columns col ON col.object_id = ic.object_id AND col.column_id = ic.column_id "
            "JOIN sys.tables t ON t.object_id = i.object_id "
            "JOIN sys.schemas s ON s.schema_id = t.schema_id "
            "WHERE s.name = ? AND t.name = ? AND i.is_primary_key = 0 "
            "  AND i.index_id > 0 AND i.name IS NOT NULL "
            "ORDER BY i.name, ic.key_ordinal", ns, name)
        by_name: dict = {}
        for iname, col, uniq in rows:
            idx = by_name.get(iname)
            if idx is None:
                idx = by_name[iname] = Index(name=iname, columns=[], unique=bool(uniq))
            idx.columns.append(IndexColumn(name=col))
        return list(by_name.values())

    def _views(self, ns: str) -> List[View]:
        out: List[View] = []
        # sys.sql_modules.definition carries the full "CREATE VIEW ..." text.
        for vname, definition in self._qall(
            "SELECT v.name, m.definition FROM sys.views v "
            "JOIN sys.schemas s ON s.schema_id = v.schema_id "
            "LEFT JOIN sys.sql_modules m ON m.object_id = v.object_id "
            "WHERE s.name = ? ORDER BY v.name", ns):
            body = str(definition or "").strip()
            if body:
                out.append(View(name=vname, schema=ns, definition=body))
        return out

    # --- extraction ------------------------------------------------------
    def exact_row_count(self, table: Table) -> int:
        ns = table.schema or self.default_schema()
        row = self._q1("SELECT COUNT_BIG(*) FROM {}".format(quote_mssql_table(ns, table.name)))
        return int(row[0]) if row and row[0] is not None else 0

    def numeric_pk_bounds(self, table: Table, pk_col: str):  # type: ignore[no-untyped-def]
        # Only integer / scale-0 PKs are range-chunkable; map the PK column back
        # to its source type so a DECIMAL/UNIQUEIDENTIFIER PK falls back to a
        # single whole-table chunk rather than a bogus int range.
        src_type = None
        for c in table.columns:
            if c.name == pk_col:
                src_type = c.source_type
                break
        if not _is_integer_source(src_type):
            return None
        ns = table.schema or self.default_schema()
        row = self._q1("SELECT MIN({c}), MAX({c}) FROM {t}".format(
            c=quote_mssql(pk_col), t=quote_mssql_table(ns, table.name)))
        if not row or row[0] is None:
            return None
        try:
            return int(row[0]), int(row[1])
        except (TypeError, ValueError):
            return None

    def stream_rows(
        self,
        table: Table,
        columns: Sequence[str],
        where: Optional[str] = None,
        arraysize: int = 1000,
    ) -> Iterator[Tuple]:
        ns = table.schema or self.default_schema()
        col_list = ", ".join(quote_mssql(c) for c in columns)
        sql = "SELECT {} FROM {}".format(col_list, quote_mssql_table(ns, table.name))
        if where:
            sql += " WHERE {}".format(where)
        cur = self.conn.cursor()
        # pyodbc fetches from the server cursor on demand; arraysize bounds the
        # network round-trip batch so large tables never materialize client-side.
        cur.arraysize = arraysize
        try:
            cur.execute(sql)
            while True:
                batch = cur.fetchmany(arraysize)
                if not batch:
                    break
                for row in batch:
                    yield tuple(row)
        finally:
            cur.close()
