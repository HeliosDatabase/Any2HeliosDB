"""MySQL-dialect DDL emission for the MySQL **target** path.

The MySQL target driver moves data *out* of HeliosDB (or any source) into a
MySQL 8 server, so the tool renders MySQL-dialect DDL from the canonical IR:
identifiers are backtick-quoted, and the dialect-neutral :class:`DataType` is
mapped to its MySQL spelling (``INTEGER`` -> ``INT``, ``BYTEA`` -> ``LONGBLOB``,
``TIMESTAMP`` -> ``DATETIME``, ``BOOLEAN`` -> ``TINYINT(1)``, ``JSONB`` -> ``JSON``
…). This mirrors :mod:`any2heliosdb.emit.oracle_ddl` (Oracle target) and is the
counterpart of :mod:`any2heliosdb.emit.ddl` (PostgreSQL/HeliosDB target).

Identifiers keep their (lowercased, by the orchestrator) shape; only the
*rendering* — quoting + type spelling — is MySQL-specific. Foreign keys are
emitted separately so data can be loaded before they are enforced.
"""
from __future__ import annotations

from typing import List, Optional

from ..core.catalog_model import (
    ConstraintKind,
    DataType,
    DataTypeKind as K,
    Index,
    Table,
)
from ..typemap.registry import TypeRegistry


def mysql_ident(name: str) -> str:
    """Backtick-quote a MySQL identifier (escaping embedded backticks)."""
    return "`{}`".format(name.replace("`", "``"))


def _ident(name: str, preserve_case: bool) -> str:
    """Case-fold (PRESERVE_CASE-off lowercases) then backtick-quote.

    The orchestrator loads into MySQL with lowercased identifiers by default (it
    matches the PG path), so the emitted DDL must lowercase too — otherwise a
    case-sensitive MySQL (``lower_case_table_names=0``, the Linux default)
    creates ``EMPLOYEES`` while the loader inserts into ``employees``."""
    return mysql_ident(name if preserve_case else name.lower())


def mysql_type(dt: DataType) -> str:
    """Render the MySQL DDL fragment for a resolved :class:`DataType`.

    The inverse of the source type maps: it takes the dialect-neutral IR type
    and spells it the way MySQL 8 expects.
    """
    k = dt.kind
    simple = {
        K.SMALLINT: "SMALLINT",
        K.INTEGER: "INT",
        K.BIGINT: "BIGINT",
        K.REAL: "FLOAT",
        K.DOUBLE_PRECISION: "DOUBLE",
        K.SERIAL: "INT",
        K.BIGSERIAL: "BIGINT",
        K.TEXT: "LONGTEXT",
        K.BYTEA: "LONGBLOB",
        K.DATE: "DATE",
        K.TIME: "TIME",
        # MySQL DATETIME has no zone; TIMESTAMP is UTC-normalized with a zone,
        # so map TIMESTAMP -> DATETIME and TIMESTAMPTZ -> TIMESTAMP.
        K.TIMESTAMP: "DATETIME",
        K.TIMESTAMPTZ: "TIMESTAMP",
        K.BOOLEAN: "TINYINT(1)",
        # MySQL has no native INTERVAL/UUID type; keep the data as text.
        K.INTERVAL: "VARCHAR(64)",
        K.UUID: "CHAR(36)",
        K.JSON: "JSON",
        K.JSONB: "JSON",
    }
    if k in simple:
        return simple[k]
    if k in (K.DECIMAL, K.NUMERIC):
        if dt.precision is None:
            # Unconstrained numeric (the source exposed no precision/scale): use
            # MySQL's maximum DECIMAL so no fractional digits are ever truncated.
            return "DECIMAL(65, 30)"
        p = min(dt.precision, 65)
        s = dt.scale if dt.scale is not None else 0
        s = min(s, 30, p)
        return "DECIMAL({}, {})".format(p, s)
    if k is K.CHAR:
        return "CHAR({})".format(dt.length if dt.length is not None else 1)
    if k is K.VARCHAR:
        if dt.length is None:
            # Unbounded source varchar (e.g. HeliosDB exposes no length): a fixed
            # VARCHAR(n) could truncate, so store it as LONGTEXT.
            return "LONGTEXT"
        # MySQL VARCHAR is bounded; a very large width is better expressed as TEXT.
        if dt.length > 16383:
            return "LONGTEXT"
        return "VARCHAR({})".format(dt.length)
    if k is K.ARRAY:
        # MySQL has no array type; store as JSON.
        return "JSON"
    if k is K.CUSTOM:
        # Unmapped source type — fall back to text rather than emit an unknown name.
        return "LONGTEXT"
    return "LONGTEXT"


def _col_type(table: Table, column, registry: Optional[TypeRegistry]) -> str:
    if registry is not None and column.source_type:
        resolved = registry.resolve(
            column.source_type, table=table.name, column=column.name, schema=table.schema
        ).data_type
        return mysql_type(resolved)
    return mysql_type(column.data_type)


def _translate_default(default: str) -> Optional[str]:
    d = default.strip()
    up = d.upper()
    if up in ("CURRENT_TIMESTAMP", "SYSDATE", "SYSTIMESTAMP", "NOW()"):
        return "CURRENT_TIMESTAMP"
    # Numeric literals replay verbatim; string/expression defaults are dropped
    # (the data carries the values, and a cross-dialect default expression is a
    # quoting/semantics hazard).
    try:
        float(d)
        return d
    except ValueError:
        return None


def render_create_table(
    table: Table, registry: Optional[TypeRegistry] = None, preserve_case: bool = False
) -> str:
    lines: List[str] = []
    for col in table.columns:
        coltype = _col_type(table, col, registry)
        piece = "  {} {}".format(_ident(col.name, preserve_case), coltype)
        if not col.nullable:
            piece += " NOT NULL"
        # Skip a PG nextval() default: MySQL uses AUTO_INCREMENT, not nextval().
        if col.default is not None and not col.default.strip().upper().startswith("NEXTVAL("):
            d = _translate_default(col.default)
            # CURRENT_TIMESTAMP is only valid as a default on DATETIME/TIMESTAMP.
            if d is not None and not (
                d == "CURRENT_TIMESTAMP" and not coltype.upper().startswith(("DATETIME", "TIMESTAMP"))
            ):
                piece += " DEFAULT {}".format(d)
        lines.append(piece)
    if table.primary_key and table.primary_key.columns:
        pk = ", ".join(_ident(c, preserve_case) for c in table.primary_key.columns)
        lines.append("  PRIMARY KEY ({})".format(pk))
    for c in table.constraints:
        if c.constraint_type is ConstraintKind.CHECK and c.expression:
            name = "CONSTRAINT {} ".format(_ident(c.name, preserve_case)) if c.name else ""
            lines.append("  {}CHECK ({})".format(name, c.expression))
    body = ",\n".join(lines)
    return "CREATE TABLE {} (\n{}\n)".format(_ident(table.name, preserve_case), body)


def render_index(table: Table, index: Index, preserve_case: bool = False) -> Optional[str]:
    # Skip the index that merely backs the primary key (MySQL creates it).
    if table.primary_key:
        idx_cols = [c.name for c in index.columns]
        if index.unique and idx_cols == table.primary_key.columns:
            return None
    cols = ", ".join(_ident(c.name, preserve_case) for c in index.columns)
    unique = "UNIQUE " if index.unique else ""
    return "CREATE {}INDEX {} ON {} ({})".format(
        unique, _ident(index.name, preserve_case), _ident(table.name, preserve_case), cols)


def render_foreign_keys(table: Table, preserve_case: bool = False) -> List[str]:
    out: List[str] = []
    for fk in table.foreign_keys:
        local = ", ".join(_ident(c, preserve_case) for c in fk.columns)
        ref = ", ".join(_ident(c, preserve_case) for c in fk.references_columns)
        name = (_ident(fk.name, preserve_case) if fk.name
                else _ident("{}_fk".format(table.name), preserve_case))
        out.append(
            "ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({})".format(
                _ident(table.name, preserve_case), name, local,
                _ident(fk.references_table, preserve_case), ref))
    return out


def render_drop_foreign_keys(table: Table, preserve_case: bool = False) -> List[str]:
    """DROP each FK — used before a resumable/chunked data load so per-chunk
    range deletes can't trip enforced FKs; they are re-added after.

    MySQL has no ``DROP FOREIGN KEY IF EXISTS``, so the orchestrator runs these
    best-effort (a missing FK on a fresh migrate just errors harmlessly)."""
    out: List[str] = []
    for fk in table.foreign_keys:
        name = (_ident(fk.name, preserve_case) if fk.name
                else _ident("{}_fk".format(table.name), preserve_case))
        out.append("ALTER TABLE {} DROP FOREIGN KEY {}".format(
            _ident(table.name, preserve_case), name))
    return out
