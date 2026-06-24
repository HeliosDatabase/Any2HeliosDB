"""Oracle-dialect DDL emission for the ``native`` target path.

The native driver connects to HeliosDB through its **Oracle** wire protocol, so
HeliosDB performs the dialect translation and the tool emits near-passthrough
Oracle DDL: column types are the original ``source_type`` strings captured during
introspection (``NUMBER(10)``, ``VARCHAR2(100)``, ``DATE`` …), identifiers keep
their source (upper) case, and defaults are replayed verbatim. This is the
opposite of :mod:`any2heliosdb.emit.ddl` (which lowercases and maps types to the
PostgreSQL surface for the ``psycopg`` path).
"""
from __future__ import annotations

from typing import List, Optional

from ..core.catalog_model import ConstraintKind, Index, Sequence, Table


def oracle_ident(name: str) -> str:
    """Quote an identifier, preserving the source case (Oracle folds unquoted
    names to upper, and introspection already returns them upper-cased)."""
    return '"{}"'.format(name.replace('"', '""'))


def _col_type(column) -> str:  # type: ignore[no-untyped-def]
    # Near-passthrough: prefer the verbatim Oracle source type.
    return column.source_type or column.data_type.sql()


def render_create_table_oracle(table: Table) -> str:
    lines: List[str] = []
    for col in table.columns:
        piece = "  {} {}".format(oracle_ident(col.name), _col_type(col))
        if not col.nullable:
            piece += " NOT NULL"
        if col.default is not None:
            piece += " DEFAULT {}".format(col.default)
        lines.append(piece)
    if table.primary_key and table.primary_key.columns:
        pk = ", ".join(oracle_ident(c) for c in table.primary_key.columns)
        lines.append("  PRIMARY KEY ({})".format(pk))
    for c in table.constraints:
        if c.constraint_type is ConstraintKind.CHECK and c.expression:
            name = "CONSTRAINT {} ".format(oracle_ident(c.name)) if c.name else ""
            lines.append("  {}CHECK ({})".format(name, c.expression))
    return "CREATE TABLE {} (\n{}\n)".format(oracle_ident(table.name), ",\n".join(lines))


def render_sequence_oracle(seq: Sequence) -> str:
    parts = ["CREATE SEQUENCE {}".format(oracle_ident(seq.name)),
             "START WITH {}".format(seq.start),
             "INCREMENT BY {}".format(seq.increment)]
    if seq.min_value is not None:
        parts.append("MINVALUE {}".format(seq.min_value))
    if seq.max_value is not None and seq.max_value < 10**27:
        parts.append("MAXVALUE {}".format(seq.max_value))
    if seq.cycle:
        parts.append("CYCLE")
    return " ".join(parts)


def render_index_oracle(table: Table, index: Index) -> Optional[str]:
    if table.primary_key:
        idx_cols = [c.name for c in index.columns]
        if index.unique and idx_cols == table.primary_key.columns:
            return None
    cols = ", ".join(oracle_ident(c.name) for c in index.columns)
    unique = "UNIQUE " if index.unique else ""
    return "CREATE {}INDEX {} ON {} ({})".format(
        unique, oracle_ident(index.name), oracle_ident(table.name), cols)


def render_foreign_keys_oracle(table: Table) -> List[str]:
    out: List[str] = []
    for fk in table.foreign_keys:
        local = ", ".join(oracle_ident(c) for c in fk.columns)
        ref = ", ".join(oracle_ident(c) for c in fk.references_columns)
        name = oracle_ident(fk.name) if fk.name else oracle_ident("{}_fk".format(table.name))
        out.append("ALTER TABLE {} ADD CONSTRAINT {} FOREIGN KEY ({}) REFERENCES {} ({})".format(
            oracle_ident(table.name), name, local, oracle_ident(fk.references_table), ref))
    return out
