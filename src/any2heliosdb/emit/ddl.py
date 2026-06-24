"""Render HeliosDB DDL from the canonical IR.

DDL is generated from the IR structs (not by regex over source DDL). Identifiers
are lowercased by default (Ora2Pg's PRESERVE_CASE-off behavior) and quoted only
when necessary (reserved word, mixed case under preserve_case, or special
characters) via the shared ``core.identifiers`` renderer, so the DDL, the data
loader, and the validators always agree on every name. Foreign keys are emitted
separately so data can be loaded before they are enforced.

When a :class:`TypeRegistry` is supplied, column types are re-resolved from the
verbatim ``source_type`` so user DATA_TYPE/MODIFY_TYPE overrides apply; otherwise
the column's already-resolved ``data_type`` is used.
"""
from __future__ import annotations

from typing import List, Optional

from ..core.catalog_model import (
    ConstraintKind,
    DataType,
    DataTypeKind,
    Index,
    Sequence,
    Table,
    View,
)
from ..core.identifiers import render_ident as ident
from ..typemap.registry import TypeRegistry


def _col_type(table: Table, column, registry: Optional[TypeRegistry]) -> str:
    if registry is not None and column.source_type:
        return registry.resolve(
            column.source_type, table=table.name, column=column.name, schema=table.schema
        ).data_type.sql()
    return column.data_type.sql()


def render_create_table(
    table: Table, registry: Optional[TypeRegistry] = None, preserve_case: bool = False
) -> str:
    lines: List[str] = []
    for col in table.columns:
        piece = "    {} {}".format(ident(col.name, preserve_case), _col_type(table, col, registry))
        if not col.nullable:
            piece += " NOT NULL"
        if col.default is not None:
            piece += " DEFAULT {}".format(_translate_default(col.default, col.data_type))
        lines.append(piece)
    if table.primary_key and table.primary_key.columns:
        pk = ", ".join(ident(c, preserve_case) for c in table.primary_key.columns)
        lines.append("    PRIMARY KEY ({})".format(pk))
    for c in table.constraints:
        if c.constraint_type is ConstraintKind.CHECK and c.expression:
            name = " CONSTRAINT {}".format(ident(c.name, preserve_case)) if c.name else ""
            lines.append("    {}CHECK ({})".format(name.strip() + " " if name else "", c.expression))
    body = ",\n".join(lines)
    return "CREATE TABLE {} (\n{}\n);".format(ident(table.name, preserve_case), body)


def _translate_default(default: str, data_type: Optional[DataType] = None) -> str:
    d = default.strip()
    up = d.upper()
    # The few always-safe Oracle->PG default rewrites.
    if up in ("SYSDATE", "SYSTIMESTAMP", "CURRENT_TIMESTAMP"):
        return "CURRENT_TIMESTAMP"
    if up in ("SYS_GUID()", "SYS_GUID"):
        return "gen_random_uuid()"
    # A BOOLEAN column's numeric/string default (MySQL TINYINT(1) DEFAULT 1/0,
    # or '1'/'0', b'1') must be a boolean literal — strict PostgreSQL rejects
    # `boolean DEFAULT 1` (HeliosDB accepts it, which masked this).
    if data_type is not None and data_type.kind is DataTypeKind.BOOLEAN:
        lit = up.strip("()").strip("'\"").lstrip("B").strip("'\"")
        if lit in ("1", "TRUE", "T", "Y", "YES"):
            return "true"
        if lit in ("0", "FALSE", "F", "N", "NO"):
            return "false"
    return d


def render_sequence(seq: Sequence, preserve_case: bool = False) -> str:
    parts = ["CREATE SEQUENCE {}".format(ident(seq.name, preserve_case))]
    parts.append("START WITH {}".format(seq.start))
    parts.append("INCREMENT BY {}".format(seq.increment))
    if seq.min_value is not None:
        parts.append("MINVALUE {}".format(seq.min_value))
    if seq.max_value is not None and seq.max_value < 10**27:
        parts.append("MAXVALUE {}".format(seq.max_value))
    if seq.cycle:
        parts.append("CYCLE")
    return " ".join(parts) + ";"


def render_index(table: Table, index: Index, preserve_case: bool = False) -> Optional[str]:
    # Skip the index that merely backs the primary key.
    if table.primary_key:
        idx_cols = [c.name for c in index.columns]
        if index.unique and idx_cols == table.primary_key.columns:
            return None
    cols = ", ".join(ident(c.name, preserve_case) for c in index.columns)
    unique = "UNIQUE " if index.unique else ""
    stmt = "CREATE {}INDEX {} ON {} ({})".format(
        unique, ident(index.name, preserve_case), ident(table.name, preserve_case), cols
    )
    if index.condition:
        stmt += " WHERE {}".format(index.condition)
    return stmt + ";"


def render_foreign_keys(table: Table, preserve_case: bool = False) -> List[str]:
    out: List[str] = []
    for fk in table.foreign_keys:
        local = ", ".join(ident(c, preserve_case) for c in fk.columns)
        ref = ", ".join(ident(c, preserve_case) for c in fk.references_columns)
        name = ident(fk.name, preserve_case) if fk.name else "{}_fk".format(table.name.lower())
        out.append(
            "ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({});".format(
                ident(table.name, preserve_case), name, local,
                ident(fk.references_table, preserve_case), ref,
            )
        )
    return out


def render_drop_foreign_keys(table: Table, preserve_case: bool = False) -> List[str]:
    """DROP each FK (IF EXISTS) — used before a resumable/chunked data load so
    per-chunk range deletes can't trip enforced FKs; they are re-added after."""
    out: List[str] = []
    for fk in table.foreign_keys:
        name = ident(fk.name, preserve_case) if fk.name else "{}_fk".format(table.name.lower())
        out.append("ALTER TABLE {} DROP CONSTRAINT IF EXISTS {};".format(
            ident(table.name, preserve_case), name))
    return out


def render_view(view: View, preserve_case: bool = False) -> str:
    return "CREATE VIEW {} AS {};".format(ident(view.name, preserve_case), view.definition.rstrip(";"))
