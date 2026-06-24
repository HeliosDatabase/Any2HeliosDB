"""Result model for the VALIDATION layer (Ora2Pg TEST / TEST_COUNT / TEST_DATA).

These are the in-memory shapes the three validators produce. They are deliberately
small dataclasses so a run manifest / report can serialize them without custom
encoders (severities reuse the cross-cutting :class:`Severity` enum).

Note: :class:`ValidationError` here is a *finding record* (severity + table +
message), distinct from :class:`any2heliosdb.errors.ValidationError`, which is the
raised exception type. A result "passes" when it carries no BLOCKER findings;
DEGRADED/COSMETIC findings are reported but not fatal.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List

from ..constants import Severity


class ValidationType(str, Enum):
    """Which Ora2Pg-style check produced a result."""

    TEST = "TEST"
    TEST_COUNT = "TEST_COUNT"
    TEST_DATA = "TEST_DATA"
    TEST_INDEX = "TEST_INDEX"


@dataclass
class ValidationError:
    """A single validation finding (not an exception)."""

    severity: Severity
    table: str
    message: str


@dataclass
class ValidationResult:
    """The outcome of one validator over a set of tables.

    ``errors`` accumulates findings; ``metrics`` carries free-form counters
    (rows compared, tables checked, mismatches, ...) for the report. ``passed``
    is *derived*: a result passes iff it holds no BLOCKER findings.
    """

    validation_type: ValidationType
    errors: List[ValidationError] = field(default_factory=list)
    metrics: Dict[str, object] = field(default_factory=dict)

    def add_error(self, severity: Severity, table: str, message: str) -> ValidationError:
        """Append a finding and return it (for fluent use / inspection)."""
        err = ValidationError(severity=severity, table=table, message=message)
        self.errors.append(err)
        return err

    @property
    def passed(self) -> bool:
        """True when no BLOCKER findings are present."""
        return not any(e.severity is Severity.BLOCKER for e in self.errors)
