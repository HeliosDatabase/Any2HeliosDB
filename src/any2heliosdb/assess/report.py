"""The assessment report — Ora2Pg ``SHOW_REPORT --estimate_cost`` analogue.

:func:`build_report` combines:

* the schema **inventory** (object/column counts, per-table columns),
* **type provenance** — for every table column, what the :class:`TypeRegistry`
  resolved the verbatim source type to, and whether that came from a default
  mapping or a user ``DATA_TYPE`` / ``MODIFY_TYPE`` override, and
* a coarse **migration-cost** estimate in person-days.

The real PL/SQL translation cost is produced by the ``plsql`` module and arrives
as ``gap_report``; here we apply only a deliberately simple placeholder
heuristic (routines + triggers) so the report is useful before that lands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..constants import Edition
from ..core.catalog_model import Schema
from ..typemap.registry import TypeRegistry
from .inventory import schema_inventory

# Placeholder cost weights (person-days). Real PL/SQL cost replaces these once
# the plsql module's gap report is wired in.
_COST_PER_ROUTINE = 0.25
_COST_PER_TRIGGER = 0.1


@dataclass
class AssessmentReport:
    """Structured result of assessing one schema against a target edition."""

    source_dialect: str
    edition: Edition
    inventory: Dict[str, Any]
    type_provenance: List[Dict[str, Any]] = field(default_factory=list)
    cost_person_days: float = 0.0
    gaps: List[Dict[str, Any]] = field(default_factory=list)


def _gaps_to_list(gap_report: Optional[Any]) -> List[Dict[str, Any]]:
    """Coerce an optional gap report into a JSON-serializable list of dicts.

    Tolerant of shapes because the producing ``plsql`` module is developed in
    parallel: accepts ``None``, an object exposing a ``.gaps`` iterable, or a
    bare iterable. Each item is normalized to a dict; items already dict-like or
    dataclass-like are passed through, others are stringified.
    """
    if gap_report is None:
        return []
    items = getattr(gap_report, "gaps", gap_report)
    out: List[Dict[str, Any]] = []
    try:
        iterator = iter(items)
    except TypeError:
        return out
    for item in iterator:
        out.append(_gap_item_to_dict(item))
    return out


def _gap_item_to_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    as_dict = getattr(item, "__dict__", None)
    if as_dict:
        result: Dict[str, Any] = {}
        for key, value in as_dict.items():
            # Unwrap str-Enums (e.g. Severity) to their plain string value.
            result[key] = getattr(value, "value", value)
        return result
    return {"detail": str(item)}


def build_report(
    schema: Schema,
    registry: TypeRegistry,
    edition: Edition = Edition.UNKNOWN,
    gap_report: Optional[Any] = None,
) -> AssessmentReport:
    """Build an :class:`AssessmentReport` for *schema* against *edition*.

    For every table column, ``registry.resolve`` is consulted (keyed by the
    verbatim ``source_type`` when present, else the column's resolved target
    SQL) and the resulting (source type -> target SQL + provenance) mapping is
    recorded in ``type_provenance``.
    """
    inventory = schema_inventory(schema)

    type_provenance: List[Dict[str, Any]] = []
    for table in schema.tables:
        for column in table.columns:
            source_type = column.source_type or column.data_type.sql()
            resolved = registry.resolve(
                source_type,
                table=table.name,
                column=column.name,
                schema=table.schema,
            )
            type_provenance.append(
                {
                    "table": table.fqn,
                    "column": column.name,
                    "source_type": source_type,
                    "target_sql": resolved.data_type.sql(),
                    "provenance": resolved.provenance.value,
                }
            )

    cost_person_days = round(
        _COST_PER_ROUTINE * len(schema.routines)
        + _COST_PER_TRIGGER * len(schema.triggers),
        2,
    )

    return AssessmentReport(
        source_dialect=registry.dialect.value,
        edition=edition,
        inventory=inventory,
        type_provenance=type_provenance,
        cost_person_days=cost_person_days,
        gaps=_gaps_to_list(gap_report),
    )
