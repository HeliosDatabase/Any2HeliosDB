"""Assessment module — the SHOW_* / SHOW_REPORT surface.

Mirrors Ora2Pg's ``SHOW_VERSION`` / ``SHOW_SCHEMA`` / ``SHOW_TABLE`` /
``SHOW_COLUMN`` inspection and ``SHOW_REPORT --estimate_cost`` migration-cost
estimate, computed against the canonical IR
(:mod:`any2heliosdb.core.catalog_model`) instead of a live catalog so the same
report can be produced offline from an introspected schema.

Public surface:

* :func:`~any2heliosdb.assess.inventory.schema_inventory` — object/column counts.
* :class:`~any2heliosdb.assess.report.AssessmentReport` + :func:`build_report`.
* :mod:`~any2heliosdb.assess.render` — text / JSON / HTML renderers.
"""
from __future__ import annotations

from .inventory import schema_inventory
from .report import AssessmentReport, build_report
from .render import render_html, render_json, render_text

__all__ = [
    "schema_inventory",
    "AssessmentReport",
    "build_report",
    "render_text",
    "render_json",
    "render_html",
]
