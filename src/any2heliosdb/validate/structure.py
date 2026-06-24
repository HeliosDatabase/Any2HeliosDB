"""TEST: structural parity between the source schema and the HeliosDB target.

Mirrors Ora2Pg's ``-t TEST`` object-existence check, scoped to tables: for each
source :class:`Table`, confirm the target has that table and that it carries the
same number of columns.

Catalog support (information_schema / pg_catalog) varies across HeliosDB
editions, so existence + column count are read via the driver's catalog-free
``describe_columns`` (``SELECT * ... LIMIT 0`` + result description), which works
on any PG-wire target. A missing table or a column-count mismatch is a BLOCKER.
"""
from __future__ import annotations

from typing import List, Optional

from ..constants import Severity
from ..core.catalog_model import Schema, Table
from ..target.base import TargetDriver
from .model import ValidationResult, ValidationType


def _target_columns(target: TargetDriver, table: Table, preserve_case: bool) -> Optional[List[str]]:
    """Target column names, or ``None`` if the table is absent/unreadable."""
    try:
        return target.describe_columns(table.target_name(preserve_case))
    except Exception:  # noqa: BLE001
        return None


def run_test(
    source_schema: Schema, target: TargetDriver, preserve_case: bool = False
) -> ValidationResult:
    """Check every source table exists on the target with the same column count."""
    result = ValidationResult(validation_type=ValidationType.TEST)
    tables_ok = 0
    for table in source_schema.tables:
        tgt_cols = _target_columns(target, table, preserve_case)
        if tgt_cols is None:
            result.add_error(Severity.BLOCKER, table.fqn, "missing table on target")
            continue
        src_cols = len(table.columns)
        if src_cols != len(tgt_cols):
            result.add_error(
                Severity.BLOCKER,
                table.fqn,
                "column count differs (source={}, target={})".format(src_cols, len(tgt_cols)),
            )
        else:
            tables_ok += 1
    result.metrics["tables_checked"] = len(source_schema.tables)
    result.metrics["tables_ok"] = tables_ok
    return result
