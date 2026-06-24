"""Cheap, heuristic effort scoring for PL/SQL routine bodies.

Mirrors Ora2Pg's assessment-cost idea: count weighted constructs in a routine's
source text to produce a rough "cost units" number, then convert to person-days
with a tunable factor. This is intentionally a regex word-count — it is an
estimate for triage/reporting, not a parser.

All matching is case-insensitive and counts *every* occurrence of each construct
(so a body with three cursors scores three times the cursor weight).
"""
from __future__ import annotations

import re
from typing import List, Tuple

# (compiled pattern, weight). \b word boundaries keep these from matching inside
# unrelated identifiers; ``dbms_`` deliberately has no trailing boundary so it
# catches the whole DBMS_* package family (DBMS_OUTPUT, DBMS_LOB, ...).
_CONSTRUCTS: List[Tuple["re.Pattern[str]", int]] = [
    (re.compile(r"\bcursor\b", re.IGNORECASE), 2),
    (re.compile(r"\bexception\b", re.IGNORECASE), 2),
    (re.compile(r"\bdbms_", re.IGNORECASE), 3),
    (re.compile(r"\bconnect\s+by\b", re.IGNORECASE), 5),
    (re.compile(r"\bbulk\s+collect\b", re.IGNORECASE), 4),
    (re.compile(r"\bforall\b", re.IGNORECASE), 4),
    (re.compile(r"\bpragma\s+autonomous_transaction\b", re.IGNORECASE), 5),
    (re.compile(r"\bexecute\s+immediate\b", re.IGNORECASE), 3),
]


def score_routine(body: str) -> int:
    """Return weighted cost units for a routine body.

    Each construct contributes ``weight * occurrences``. An empty/None body
    scores 0.
    """
    if not body:
        return 0
    total = 0
    for pattern, weight in _CONSTRUCTS:
        total += weight * len(pattern.findall(body))
    return total


def to_person_days(units: int, factor: float = 0.05) -> float:
    """Convert cost units to estimated person-days, rounded to 2 decimals."""
    return round(units * factor, 2)
