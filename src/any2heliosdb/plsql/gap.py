"""Target-gap modelling for the PL/SQL rewrite layer.

A :class:`TargetGap` records one thing this HeliosDB target could not accept (a
missing function, an unsupported construct, a partial rewrite) together with a
human-actionable recommendation. The rewrite layer (:mod:`.rewrite`) emits these
as it translates; :class:`GapReport` collects and de-duplicates them so the
assessment surface shows each gap once with a summed occurrence count.

Severity is the shared :class:`~any2heliosdb.constants.Severity` enum (BLOCKER /
DEGRADED / COSMETIC) — never redefined here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..constants import Edition, Severity


@dataclass(frozen=True)
class TargetGap:
    """One capability/translation gap against a specific HeliosDB target.

    ``object_ref`` is an optional source object the gap was found in (e.g. a
    routine or view name) so the report can point the user at it; ``occurrences``
    counts how many times the same (feature, object_ref) gap was seen.
    """

    feature: str
    edition: Edition
    object_ref: Optional[str]
    occurrences: int
    severity: Severity
    workaround: Optional[str]
    recommendation: str

    @property
    def key(self) -> Tuple[str, Optional[str]]:
        """De-dup identity: the same feature in the same object is one gap."""
        return (self.feature, self.object_ref)


class GapReport:
    """An ordered, de-duplicated collection of :class:`TargetGap` items."""

    def __init__(self) -> None:
        # Keyed by (feature, object_ref); insertion order is preserved (py3.7+),
        # so render order matches discovery order.
        self._gaps: Dict[Tuple[str, Optional[str]], TargetGap] = {}

    def add(self, gap: TargetGap) -> None:
        """Add a gap, merging into an existing one with the same key.

        Occurrences are summed. The first-seen severity/recommendation/workaround
        win (a later duplicate of the same feature does not silently downgrade
        the original finding).
        """
        existing = self._gaps.get(gap.key)
        if existing is None:
            self._gaps[gap.key] = gap
            return
        # frozen dataclass: rebuild with the summed occurrence count.
        self._gaps[gap.key] = TargetGap(
            feature=existing.feature,
            edition=existing.edition,
            object_ref=existing.object_ref,
            occurrences=existing.occurrences + gap.occurrences,
            severity=existing.severity,
            workaround=existing.workaround,
            recommendation=existing.recommendation,
        )

    def extend(self, gaps) -> None:
        """Add many gaps (convenience for rewrite output)."""
        for g in gaps:
            self.add(g)

    @property
    def gaps(self) -> List[TargetGap]:
        return list(self._gaps.values())

    def __len__(self) -> int:
        return len(self._gaps)

    def __bool__(self) -> bool:
        return bool(self._gaps)

    def render_text(self) -> str:
        """A compact, human-readable report (one line per gap)."""
        if not self._gaps:
            return "No target gaps found."
        lines: List[str] = ["Target gaps ({}):".format(len(self._gaps))]
        for g in self._gaps.values():
            where = " in {}".format(g.object_ref) if g.object_ref else ""
            count = " x{}".format(g.occurrences) if g.occurrences != 1 else ""
            lines.append(
                "  [{sev}] {feat}{where}{count} ({ed}) -> {rec}".format(
                    sev=g.severity.value.upper(),
                    feat=g.feature,
                    where=where,
                    count=count,
                    ed=g.edition.value,
                    rec=g.recommendation,
                )
            )
            if g.workaround:
                lines.append("      workaround: {}".format(g.workaround))
        return "\n".join(lines)

    def render_json(self) -> str:
        """A stable JSON array (str-valued enums serialize cleanly)."""
        payload = [
            {
                "feature": g.feature,
                "edition": g.edition.value,
                "object_ref": g.object_ref,
                "occurrences": g.occurrences,
                "severity": g.severity.value,
                "workaround": g.workaround,
                "recommendation": g.recommendation,
            }
            for g in self._gaps.values()
        ]
        return json.dumps(payload, indent=2)
