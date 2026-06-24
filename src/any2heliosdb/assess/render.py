"""Renderers for an :class:`~any2heliosdb.assess.report.AssessmentReport`.

Three surfaces, mirroring Ora2Pg's report outputs:

* :func:`render_text` — a compact plain-text summary for the terminal.
* :func:`render_json` — ``json.dumps`` of the report (machine-readable).
* :func:`render_html` — a standalone HTML page via a small Jinja2 template.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Dict

from jinja2 import Environment

from .report import AssessmentReport


def _as_dict(report: AssessmentReport) -> Dict[str, Any]:
    """Report as a plain dict. ``str``-Enums (Edition) serialize as their value."""
    data = asdict(report)
    # ``asdict`` keeps the Enum instance; normalize to its plain string value so
    # both JSON and the HTML template see a string.
    data["edition"] = getattr(report.edition, "value", report.edition)
    return data


def render_json(report: AssessmentReport) -> str:
    """Serialize the full report to indented JSON."""
    return json.dumps(_as_dict(report), indent=2, sort_keys=True)


def render_text(report: AssessmentReport) -> str:
    """Render a compact, human-readable plain-text summary."""
    counts = report.inventory.get("counts", {})
    lines = []
    lines.append("=" * 60)
    lines.append("HeliosDB Migration Assessment")
    lines.append("=" * 60)
    lines.append("Source dialect : {}".format(report.source_dialect))
    lines.append("Target edition : {}".format(getattr(report.edition, "value", report.edition)))
    lines.append("Schema         : {}".format(report.inventory.get("schema", "")))
    lines.append("")
    lines.append("Object inventory")
    lines.append("-" * 60)
    for key in (
        "tables",
        "columns",
        "views",
        "sequences",
        "routines",
        "triggers",
        "indexes",
        "foreign_keys",
        "types",
    ):
        if key in counts:
            lines.append("  {:<14}: {}".format(key, counts[key]))
    lines.append("")
    lines.append("Tables")
    lines.append("-" * 60)
    for table in report.inventory.get("tables", []):
        lines.append(
            "  {} ({} columns)".format(table.get("name"), table.get("column_count", 0))
        )
        for col in table.get("columns", []):
            lines.append(
                "      {:<24} {} -> {}".format(
                    col.get("name", ""),
                    col.get("source_type", ""),
                    col.get("target_sql", ""),
                )
            )
    lines.append("")
    lines.append("Estimated migration cost: {} person-days".format(report.cost_person_days))
    if report.gaps:
        lines.append("")
        lines.append("Gaps ({})".format(len(report.gaps)))
        lines.append("-" * 60)
        for gap in report.gaps:
            lines.append("  - {}".format(gap))
    lines.append("=" * 60)
    return "\n".join(lines)


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>HeliosDB Migration Assessment - {{ schema }}</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem; }
  h1 { font-size: 1.5rem; }
  table { border-collapse: collapse; margin-bottom: 1.5rem; }
  th, td { border: 1px solid #ccc; padding: 4px 10px; text-align: left; }
  th { background: #f4f4f4; }
  .meta td:first-child { font-weight: bold; }
  caption { font-weight: bold; text-align: left; margin-bottom: 4px; }
</style>
</head>
<body>
<h1>HeliosDB Migration Assessment</h1>
<table class="meta">
  <tr><td>Source dialect</td><td>{{ source_dialect }}</td></tr>
  <tr><td>Target edition</td><td>{{ edition }}</td></tr>
  <tr><td>Schema</td><td>{{ schema }}</td></tr>
  <tr><td>Estimated cost</td><td>{{ cost_person_days }} person-days</td></tr>
</table>

<table>
  <caption>Object inventory</caption>
  <tr><th>Object</th><th>Count</th></tr>
  {% for key, value in counts.items() %}
  <tr><td>{{ key }}</td><td>{{ value }}</td></tr>
  {% endfor %}
</table>

{% for table in tables %}
<table>
  <caption>{{ table.name }} ({{ table.column_count }} columns)</caption>
  <tr><th>Column</th><th>Source type</th><th>Target SQL</th><th>Nullable</th></tr>
  {% for col in table.columns %}
  <tr>
    <td>{{ col.name }}</td>
    <td>{{ col.source_type }}</td>
    <td>{{ col.target_sql }}</td>
    <td>{{ col.nullable }}</td>
  </tr>
  {% endfor %}
</table>
{% endfor %}

{% if gaps %}
<table>
  <caption>Gaps ({{ gaps|length }})</caption>
  <tr><th>Detail</th></tr>
  {% for gap in gaps %}
  <tr><td>{{ gap }}</td></tr>
  {% endfor %}
</table>
{% endif %}
</body>
</html>
"""


def render_html(report: AssessmentReport) -> str:
    """Render the report as a standalone HTML page via Jinja2."""
    env = Environment(autoescape=True)
    template = env.from_string(_HTML_TEMPLATE)
    inventory = report.inventory
    return template.render(
        source_dialect=report.source_dialect,
        edition=getattr(report.edition, "value", report.edition),
        schema=inventory.get("schema", ""),
        cost_person_days=report.cost_person_days,
        counts=inventory.get("counts", {}),
        tables=inventory.get("tables", []),
        gaps=report.gaps,
    )
