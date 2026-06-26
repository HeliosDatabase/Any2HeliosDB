"""Schema object inventory — the SHOW_SCHEMA / SHOW_TABLE / SHOW_COLUMN counts.

Walks the canonical IR (:class:`~any2heliosdb.core.catalog_model.Schema`) and
returns a plain, JSON-serializable ``dict`` of object counts plus a per-table
column listing. Pure function, no I/O — the assessment report and the renderers
consume this directly.
"""
from __future__ import annotations

from typing import Dict, List

from ..core.catalog_model import Schema


def _column_entry(column) -> Dict[str, object]:
    """One column's assessment view: name, verbatim source type, target SQL."""
    source_type = column.source_type or column.data_type.sql()
    return {
        "name": column.name,
        "source_type": source_type,
        "target_sql": column.data_type.sql(),
        "nullable": bool(column.nullable),
    }


def schema_inventory(schema: Schema) -> Dict[str, object]:
    """Return object/column counts plus a per-table column list for *schema*.

    The returned dict is deliberately flat and JSON-friendly::

        {
          "schema": "HR",
          "counts": {"tables": 2, "columns": 5, "views": 0, "sequences": 1,
                     "routines": 0, "triggers": 0, "indexes": 1,
                     "foreign_keys": 1, "types": 0},
          "tables": [
             {"name": "EMPLOYEES", "schema": "HR", "column_count": 3,
              "columns": [ {column entries...} ]},
             ...
          ],
        }
    """
    tables: List[Dict[str, object]] = []
    total_columns = 0
    total_indexes = 0
    total_foreign_keys = 0
    total_partitioned = 0

    for table in schema.tables:
        cols = [_column_entry(c) for c in table.columns]
        total_columns += len(cols)
        total_indexes += len(table.indexes)
        total_foreign_keys += len(table.foreign_keys)
        if getattr(table.options, "partition", None) is not None:
            total_partitioned += 1
        tables.append(
            {
                "name": table.name,
                "schema": table.schema,
                "column_count": len(cols),
                "columns": cols,
            }
        )

    counts: Dict[str, int] = {
        "tables": len(schema.tables),
        "columns": total_columns,
        "views": len(schema.views),
        "sequences": len(schema.sequences),
        "routines": len(schema.routines),
        "triggers": len(schema.triggers),
        "materialized_views": len(schema.mviews),
        "partitioned_tables": total_partitioned,
        "indexes": total_indexes,
        "foreign_keys": total_foreign_keys,
        "types": len(schema.types),
    }

    return {
        "schema": schema.name,
        "counts": counts,
        "tables": tables,
    }
