"""Procedural & advanced-object surfacing — the v1.0.0 "Option A" bar.

Oracle stored routines (PROCEDURE/FUNCTION/PACKAGE), triggers, materialized
views, and partitioned tables are NOT auto-translated by a2h v1.0.0 — PL/SQL ->
PL/pgSQL translation is the v2.0.0 roadmap. Instead they are made *visible* so a
migration never silently drops them:

* :func:`build_procedural_gaps` turns each into a de-duplicated
  :class:`~any2heliosdb.plsql.gap.TargetGap`, so ``a2h assess`` / SHOW_REPORT
  counts them, scores their cost, and recommends manual porting.
* :func:`render_review` emits their verbatim source into a ``.review.sql``
  companion so a human can port them by hand.

This keeps the tool thin (the governing principle) while surfacing exactly what
needs human attention.
"""
from __future__ import annotations

from typing import List

from ..constants import Edition, Severity
from ..core.catalog_model import Schema
from .gap import GapReport, TargetGap

_V2_NOTE = "auto-translation to PL/pgSQL is on the v2.0.0 roadmap"
_REVIEW_WORKAROUND = "emitted to the .review.sql companion for manual porting"


def build_procedural_gaps(schema: Schema, edition: Edition = Edition.UNKNOWN) -> GapReport:
    """One DEGRADED gap per routine / trigger / materialized view / partitioned
    table — surfaced for review, not auto-migrated. DEGRADED (not BLOCKER): the
    table+data migration still succeeds; only these objects need manual porting.
    """
    report = GapReport()
    for r in schema.routines:
        report.add(TargetGap(
            feature="PL/SQL {}".format(r.kind.value),
            edition=edition, object_ref=r.name, occurrences=1, severity=Severity.DEGRADED,
            workaround=_REVIEW_WORKAROUND,
            recommendation="port the {} body to PL/pgSQL by hand ({})".format(r.kind.value, _V2_NOTE),
        ))
    for t in schema.triggers:
        report.add(TargetGap(
            feature="trigger", edition=edition, object_ref=t.name, occurrences=1,
            severity=Severity.DEGRADED, workaround=_REVIEW_WORKAROUND,
            recommendation="recreate as a PL/pgSQL trigger function + CREATE TRIGGER on {} ({})".format(
                t.table, _V2_NOTE),
        ))
    for m in schema.mviews:
        report.add(TargetGap(
            feature="materialized view", edition=edition, object_ref=m.name, occurrences=1,
            severity=Severity.DEGRADED,
            workaround="defining query emitted to the .review.sql companion",
            recommendation="recreate with CREATE MATERIALIZED VIEW (+ a refresh strategy) on the target",
        ))
    for tbl in schema.tables:
        part = getattr(tbl.options, "partition", None)
        if part is not None:
            cols = ", ".join(part.columns) or "?"
            report.add(TargetGap(
                feature="partitioned table", edition=edition, object_ref=tbl.name, occurrences=1,
                severity=Severity.DEGRADED,
                workaround="data migrates into a single (non-partitioned) target table",
                recommendation="recreate {ptype} partitioning (key: {cols}) on the target if the "
                               "scheme is needed".format(ptype=part.partition_type.value, cols=cols),
            ))
    return report


def render_review(schema: Schema) -> str:
    """Render the verbatim source of every procedural object into a review SQL
    file (header comments mark each as manual-port required). Returns ``''`` when
    there is nothing to review.
    """
    blocks: List[str] = []
    for r in schema.routines:
        if r.body:
            blocks.append(
                "-- ===== PL/SQL {kind}: {name} =====\n"
                "-- REVIEW: not auto-translated by a2h ({note}). Port to PL/pgSQL by hand.\n"
                "-- Original Oracle source:\n/*\n{body}\n*/".format(
                    kind=r.kind.value.upper(), name=r.name, note=_V2_NOTE, body=r.body))
    for t in schema.triggers:
        if t.body:
            blocks.append(
                "-- ===== TRIGGER: {name} ON {table} ({timing} {events}) =====\n"
                "-- REVIEW: not auto-translated ({note}). Recreate as a trigger function + CREATE TRIGGER.\n"
                "/*\n{body}\n*/".format(name=t.name, table=t.table, timing=t.timing,
                                        events=" OR ".join(t.events), note=_V2_NOTE, body=t.body))
    for m in schema.mviews:
        if m.definition:
            blocks.append(
                "-- ===== MATERIALIZED VIEW: {name} =====\n"
                "-- REVIEW: recreate with CREATE MATERIALIZED VIEW + a refresh strategy.\n"
                "-- Defining query:\n/*\n{body}\n*/".format(name=m.name, body=m.definition))
    if not blocks:
        return ""
    header = ("-- a2h review file: procedural & advanced objects NOT auto-migrated.\n"
              "-- Surfaced from the source for MANUAL porting. a2h v1.0.0 migrates\n"
              "-- schema + data + sequences + views; PL/SQL -> PL/pgSQL is v2.0.0.\n\n")
    return header + "\n\n".join(blocks) + "\n"
