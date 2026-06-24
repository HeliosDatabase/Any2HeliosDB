"""Data-driven, overridable type registry.

Reproduces Ora2Pg's two override knobs on top of the default mappers:

* ``DATA_TYPE`` — global source-type-name → target-type remap
  (e.g. ``{"NUMBER": "bigint"}``).
* ``MODIFY_TYPE`` — per-column override keyed ``schema.table.column`` (or
  ``table.column``) → target-type (e.g. ``{"hr.emp.salary": "numeric(12,2)"}``).

``resolve`` returns the target :class:`DataType` plus *provenance* so the
assessment report can show exactly what the user overrode versus the default.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from ..constants import SourceDialect
from ..core.catalog_model import DataType
from .defaults import MAPPERS, parse_target_type


class Provenance(str, Enum):
    DEFAULT = "default"
    DATA_TYPE = "data_type_override"
    MODIFY_TYPE = "modify_type_override"


@dataclass
class ResolvedType:
    data_type: DataType
    provenance: Provenance
    source_type: str


class TypeRegistry:
    def __init__(self, dialect: SourceDialect) -> None:
        self.dialect = dialect
        self._mapper = MAPPERS[dialect.value]
        self._data_type: Dict[str, str] = {}
        self._modify_type: Dict[str, str] = {}

    def apply_data_type(self, overrides: Dict[str, str]) -> None:
        """Register DATA_TYPE overrides (source type name → target type)."""
        for k, v in overrides.items():
            self._data_type[k.upper().strip()] = v

    def apply_modify_type(self, overrides: Dict[str, str]) -> None:
        """Register MODIFY_TYPE overrides (qualified column → target type)."""
        for k, v in overrides.items():
            self._modify_type[k.lower().strip()] = v

    def _column_key(self, table: Optional[str], column: Optional[str], schema: Optional[str]) -> Optional[str]:
        if not column or not table:
            return None
        if schema:
            return "{}.{}.{}".format(schema.lower(), table.lower(), column.lower())
        return "{}.{}".format(table.lower(), column.lower())

    def resolve(
        self,
        source_type: str,
        table: Optional[str] = None,
        column: Optional[str] = None,
        schema: Optional[str] = None,
    ) -> ResolvedType:
        # 1) per-column MODIFY_TYPE (highest precedence)
        ckey = self._column_key(table, column, schema)
        if ckey and ckey in self._modify_type:
            return ResolvedType(
                parse_target_type(self._modify_type[ckey]), Provenance.MODIFY_TYPE, source_type
            )
        # 2) global DATA_TYPE remap by source type name (match on base name)
        base = source_type.upper().strip()
        base_name = base.split("(")[0].strip()
        for key in (base, base_name):
            if key in self._data_type:
                return ResolvedType(
                    parse_target_type(self._data_type[key]), Provenance.DATA_TYPE, source_type
                )
        # 3) default dialect mapping
        return ResolvedType(self._mapper(source_type), Provenance.DEFAULT, source_type)
