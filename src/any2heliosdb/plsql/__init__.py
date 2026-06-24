"""PL/SQL rewrite, target-gap reporting, and effort-cost scoring.

A thin, capability-gated translation layer that turns common Oracle SQL/PL-SQL
constructs into portable HeliosDB SQL, records what it could not handle as
:class:`~any2heliosdb.plsql.gap.TargetGap` items, and estimates routine effort.
"""
from __future__ import annotations

from .cost import score_routine, to_person_days
from .gap import GapReport, TargetGap
from .rewrite import rewrite_sql

__all__ = [
    "GapReport",
    "TargetGap",
    "rewrite_sql",
    "score_routine",
    "to_person_days",
]
