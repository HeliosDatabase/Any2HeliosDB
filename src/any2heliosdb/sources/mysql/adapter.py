"""MySQL source adapter (PyMySQL).

Introspects ``information_schema`` into the canonical IR and streams table data
for the loader. The connection runs in ANSI_QUOTES mode so double-quoted
identifiers (used by the chunker's range predicates) are interpreted as
identifiers, not string literals. Columns carry the verbatim ``source_type``
(``COLUMN_TYPE`` minus ``unsigned``/``zerofill``) so the emit layer can
re-resolve with user overrides.
"""
from __future__ import annotations

import re
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
from ...typemap.defaults import map_mysql_type
from ..base import SourceAdapter, SourceDsn

_SYS_SCHEMAS = frozenset({"mysql", "information_schema", "performance_schema", "sys"})
_NUM_LITERAL = re.compile(r"^-?\d+(\.\d+)?$")


def quote_mysql(db: str, name: str) -> str:
    return "`{}`.`{}`".format(db, name)


def _clean_coltype(column_type: str) -> str:
    # COLUMN_TYPE carries length/precision (e.g. 'varchar(100)', 'decimal(10,2)',
    # 'tinyint(1)', 'int unsigned'); strip the unsigned/zerofill attributes so the
    # type map matches, but keep the parenthesised size.
    return column_type.replace(" unsigned", "").replace(" zerofill", "").strip()


def _default(raw: object) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if s.upper() in ("CURRENT_TIMESTAMP", "NOW()", "CURRENT_TIMESTAMP()"):
        return "CURRENT_TIMESTAMP"
    if _NUM_LITERAL.match(s):
        return s
    # Skip string/expression defaults: the data carries the values, and replaying
    # a MySQL default expression verbatim risks cross-dialect quoting errors.
    return None


class MySQLAdapter(SourceAdapter):
    """Oracle-parity source adapter for MySQL/MariaDB."""

    dialect = SourceDialect.MYSQL

    def __init__(self, dsn: SourceDsn) -> None:
        super().__init__(dsn)
        self._conn = None  # type: ignore[assignment]

    # --- lifecycle -------------------------------------------------------
    def _db(self) -> str:
        return self.dsn.schema or self.dsn.database or ""

    def connect(self) -> None:
        import pymysql  # lazy

        try:
            self._conn = pymysql.connect(
                host=self.dsn.host, port=self.dsn.port, user=self.dsn.user,
                password=self.dsn.password, database=self._db() or None,
                charset="utf8mb4", autocommit=True)
            with self._conn.cursor() as cur:
                cur.execute("SET SESSION sql_mode=CONCAT(@@sql_mode, ',ANSI_QUOTES')")
        except Exception as e:  # noqa: BLE001
            raise SourceConnectionError(
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
            raise SourceConnectionError("adapter is not connected; call connect() first")
        return self._conn

    def _q1(self, sql: str, *params: object) -> Optional[Tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _qall(self, sql: str, *params: object) -> List[Tuple]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    # --- metadata --------------------------------------------------------
    def server_version(self) -> str:
        row = self._q1("SELECT VERSION()")
        return str(row[0]) if row else ""

    def default_schema(self) -> str:
        db = self._db()
        if db:
            return db
        row = self._q1("SELECT DATABASE()")
        return str(row[0]) if row and row[0] else ""

    def list_schemas(self) -> List[str]:
        return [r[0] for r in self._qall(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME")
            if r[0] not in _SYS_SCHEMAS]

    # --- introspection ---------------------------------------------------
    def introspect_schema(self, schema: Optional[str] = None) -> Schema:
        db = schema or self.default_schema()
        try:
            tables = [self._table(db, name) for (name,) in self._qall(
                "SELECT TABLE_NAME FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME", db)]
            views = self._views(db)
        except Exception as e:  # noqa: BLE001
            raise IntrospectionError("MySQL introspection failed for {}: {}".format(db, e)) from e
        return Schema(name=db, tables=tables, sequences=[], views=views)

    def _table(self, db: str, name: str) -> Table:
        cols: List[Column] = []
        for (col, coltype, nullable, default) in self._qall(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s "
            "ORDER BY ORDINAL_POSITION", db, name):
            src = _clean_coltype(coltype)
            cols.append(Column(name=col, data_type=map_mysql_type(src),
                               nullable=(nullable == "YES"), default=_default(default),
                               source_type=src))

        pk_cols = [r[0] for r in self._qall(
            "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_NAME='PRIMARY' "
            "ORDER BY ORDINAL_POSITION", db, name)]
        primary_key = PrimaryKey(columns=pk_cols) if pk_cols else None

        fks: List[ForeignKey] = []
        fk_rows = self._qall(
            "SELECT CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND REFERENCED_TABLE_NAME IS NOT NULL "
            "ORDER BY CONSTRAINT_NAME, ORDINAL_POSITION", db, name)
        by_name: dict = {}
        for cname, lcol, rtable, rcol in fk_rows:
            fk = by_name.get(cname)
            if fk is None:
                fk = by_name[cname] = ForeignKey(name=cname, columns=[],
                                                 references_table=rtable, references_columns=[])
            fk.columns.append(lcol)
            fk.references_columns.append(rcol)
        fks = list(by_name.values())

        indexes: List[Index] = []
        idx_by_name: dict = {}
        for iname, icol, non_unique in self._qall(
            "SELECT INDEX_NAME, COLUMN_NAME, NON_UNIQUE FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME<>'PRIMARY' "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX", db, name):
            idx = idx_by_name.get(iname)
            if idx is None:
                idx = idx_by_name[iname] = Index(name=iname, columns=[], unique=(int(non_unique) == 0))
            idx.columns.append(IndexColumn(name=icol))
        indexes = list(idx_by_name.values())

        return Table(name=name, schema=db, columns=cols, primary_key=primary_key,
                     foreign_keys=fks, indexes=indexes)

    def _views(self, db: str) -> List[View]:
        out: List[View] = []
        for vname, vdef in self._qall(
            "SELECT TABLE_NAME, VIEW_DEFINITION FROM information_schema.VIEWS "
            "WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME", db):
            # MySQL emits view bodies with backtick-quoted, schema-qualified
            # identifiers (`hr`.`employees`.`col`). Strip backticks and the schema
            # prefix so the body is portable SQL the target accepts. Best-effort;
            # complex views may still need manual review (the orchestrator warns).
            body = str(vdef).replace("`", "").replace("{}.".format(db), "")
            out.append(View(name=vname, definition=body))
        return out

    # --- data ------------------------------------------------------------
    def exact_row_count(self, table: Table) -> int:
        db = table.schema or self.default_schema()
        row = self._q1("SELECT COUNT(*) FROM {}".format(quote_mysql(db, table.name)))
        return int(row[0]) if row else 0

    def numeric_pk_bounds(self, table: Table, pk_col: str):  # type: ignore[no-untyped-def]
        db = table.schema or self.default_schema()
        row = self._q1("SELECT MIN(`{0}`), MAX(`{0}`) FROM {1}".format(
            pk_col, quote_mysql(db, table.name)))
        if row and row[0] is not None and isinstance(row[0], int):
            return (int(row[0]), int(row[1]))
        return None

    def stream_rows(
        self,
        table: Table,
        columns: Sequence[str],
        where: Optional[str] = None,
        arraysize: int = 1000,
    ) -> Iterator[Tuple]:
        import pymysql.cursors  # lazy

        db = table.schema or self.default_schema()
        col_list = ", ".join("`{}`".format(c) for c in columns)
        sql = "SELECT {} FROM {}".format(col_list, quote_mysql(db, table.name))
        if where:
            sql += " WHERE {}".format(where)
        cur = self.conn.cursor(pymysql.cursors.SSCursor)  # server-side streaming
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
