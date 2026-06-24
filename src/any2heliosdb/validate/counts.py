"""TEST_COUNT: row-count parity between source and HeliosDB target.

Mirrors Ora2Pg's ``-t TEST_COUNT``: for each table, compare the source's exact
row count to ``SELECT count(*)`` on the loaded target table. A per-table
DataMismatch (BLOCKER) finding is recorded for any disagreement; matches are
counted into ``metrics`` for the report.

Identifiers are lowercased on the target side to match the emitter's
PRESERVE_CASE-off convention (see :mod:`any2heliosdb.emit.ddl`).
"""
from __future__ import annotations

from typing import List

from ..constants import Severity
from ..core.catalog_model import Table
from ..core.identifiers import quote_table
from ..sources.base import SourceAdapter
from ..target.base import TargetDriver
from .model import ValidationResult, ValidationType


def _target_count(target: TargetDriver, table: Table, preserve_case: bool) -> int:
    qt = quote_table(table.target_name(preserve_case))
    rows = target.query("SELECT count(*) FROM {}".format(qt))
    if not rows or not rows[0]:
        return 0
    return int(rows[0][0])


def run_test_count(
    source: SourceAdapter,
    target: TargetDriver,
    tables: List[Table],
    preserve_case: bool = False,
) -> ValidationResult:
    """Compare source vs target row counts for every table in *tables*."""
    result = ValidationResult(validation_type=ValidationType.TEST_COUNT)
    matched = 0
    for table in tables:
        src_n = int(source.exact_row_count(table))
        tgt_n = _target_count(target, table, preserve_case)
        if src_n != tgt_n:
            result.add_error(
                Severity.BLOCKER,
                table.fqn,
                "DataMismatch: row count differs (source={}, target={}, delta={})".format(
                    src_n, tgt_n, tgt_n - src_n
                ),
            )
        else:
            matched += 1
    result.metrics["tables_checked"] = len(tables)
    result.metrics["tables_matched"] = matched
    result.metrics["tables_mismatched"] = len(tables) - matched
    return result
