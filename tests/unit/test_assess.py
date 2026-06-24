"""Unit tests for the assessment module (no database required).

Builds a small 2-table :class:`Schema` by hand, an Oracle
:class:`TypeRegistry`, runs :func:`build_report`, and checks the inventory
counts, the type-provenance resolution, the cost heuristic, and that all three
renderers (text / JSON / HTML) produce sensible output.
"""
from __future__ import annotations

import json

from any2heliosdb.assess import (
    AssessmentReport,
    build_report,
    render_html,
    render_json,
    render_text,
    schema_inventory,
)
from any2heliosdb.constants import Edition, SourceDialect
from any2heliosdb.core.catalog_model import (
    Column,
    DataType,
    DataTypeKind as K,
    PrimaryKey,
    Routine,
    RoutineKind,
    Schema,
    Sequence,
    Table,
    Trigger,
)
from any2heliosdb.typemap.registry import Provenance, TypeRegistry


def _two_table_schema() -> Schema:
    employees = Table(
        name="EMPLOYEES",
        schema="HR",
        columns=[
            Column("EMP_ID", DataType.decimal(10, 0), nullable=False, source_type="NUMBER(10,0)"),
            # The required column carrying a verbatim Oracle source type.
            Column("SALARY", DataType.decimal(10, 2), nullable=True, source_type="NUMBER(10,2)"),
            Column("FULL_NAME", DataType.varchar(100), nullable=False, source_type="VARCHAR2(100)"),
        ],
        primary_key=PrimaryKey(columns=["EMP_ID"]),
    )
    departments = Table(
        name="DEPARTMENTS",
        schema="HR",
        columns=[
            Column("DEPT_ID", DataType.decimal(6, 0), nullable=False, source_type="NUMBER(6,0)"),
            # No source_type here -> build_report must fall back to data_type.sql().
            Column("DEPT_NAME", DataType.varchar(50), nullable=False),
        ],
        primary_key=PrimaryKey(columns=["DEPT_ID"]),
    )
    return Schema(
        name="HR",
        tables=[employees, departments],
        sequences=[Sequence(name="EMP_SEQ", start=1)],
        routines=[
            Routine(name="RAISE_SALARY", kind=RoutineKind.PROCEDURE),
            Routine(name="GET_BONUS", kind=RoutineKind.FUNCTION),
        ],
        triggers=[Trigger(name="EMP_AUDIT", table="EMPLOYEES", events=["INSERT"])],
    )


def test_schema_inventory_counts():
    inv = schema_inventory(_two_table_schema())
    counts = inv["counts"]
    assert counts["tables"] == 2
    assert counts["columns"] == 5  # 3 + 2
    assert counts["views"] == 0
    assert counts["sequences"] == 1
    assert counts["routines"] == 2
    assert counts["triggers"] == 1
    # Per-table column list is present and correctly shaped.
    names = {t["name"]: t["column_count"] for t in inv["tables"]}
    assert names == {"EMPLOYEES": 3, "DEPARTMENTS": 2}


def test_build_report_inventory_and_provenance():
    registry = TypeRegistry(SourceDialect.ORACLE)
    report = build_report(_two_table_schema(), registry, edition=Edition.LITE)

    assert isinstance(report, AssessmentReport)
    assert report.source_dialect == "oracle"
    assert report.edition is Edition.LITE
    assert report.inventory["counts"]["tables"] == 2
    assert report.inventory["counts"]["columns"] == 5

    # One provenance entry per column across both tables.
    assert len(report.type_provenance) == 5
    by_col = {(p["table"], p["column"]): p for p in report.type_provenance}
    salary = by_col[("HR.EMPLOYEES", "SALARY")]
    assert salary["source_type"] == "NUMBER(10,2)"
    assert salary["target_sql"] == "DECIMAL(10, 2)"
    assert salary["provenance"] == Provenance.DEFAULT.value
    # The column without a source_type resolves via its already-resolved type.
    dept_name = by_col[("HR.DEPARTMENTS", "DEPT_NAME")]
    assert dept_name["source_type"] == "VARCHAR(50)"


def test_cost_heuristic():
    registry = TypeRegistry(SourceDialect.ORACLE)
    report = build_report(_two_table_schema(), registry)
    # 0.25 * 2 routines + 0.1 * 1 trigger = 0.6 person-days.
    assert report.cost_person_days == 0.6


def test_modify_type_override_provenance_flows_through():
    registry = TypeRegistry(SourceDialect.ORACLE)
    registry.apply_modify_type({"hr.employees.salary": "numeric(12,2)"})
    report = build_report(_two_table_schema(), registry)
    salary = next(
        p for p in report.type_provenance
        if p["table"] == "HR.EMPLOYEES" and p["column"] == "SALARY"
    )
    assert salary["target_sql"] == "NUMERIC(12, 2)"
    assert salary["provenance"] == Provenance.MODIFY_TYPE.value


def test_render_json_is_valid_json():
    registry = TypeRegistry(SourceDialect.ORACLE)
    report = build_report(_two_table_schema(), registry, edition=Edition.LITE)
    payload = render_json(report)
    parsed = json.loads(payload)  # raises if invalid
    assert parsed["source_dialect"] == "oracle"
    assert parsed["edition"] == "lite"
    assert parsed["inventory"]["counts"]["tables"] == 2
    assert parsed["cost_person_days"] == 0.6


def test_render_text_contains_table_names():
    registry = TypeRegistry(SourceDialect.ORACLE)
    report = build_report(_two_table_schema(), registry)
    text = render_text(report)
    assert "EMPLOYEES" in text
    assert "DEPARTMENTS" in text
    assert "person-days" in text


def test_render_html_renders_tables_and_escapes():
    registry = TypeRegistry(SourceDialect.ORACLE)
    report = build_report(_two_table_schema(), registry, edition=Edition.FULL)
    html = render_html(report)
    assert "<table" in html
    assert "EMPLOYEES" in html
    assert "DEPARTMENTS" in html
    assert "full" in html


def test_gap_report_is_normalized_into_report():
    registry = TypeRegistry(SourceDialect.ORACLE)

    # A tiny fake gap report shaped like what the plsql module is expected to
    # produce: an object exposing a ``.gaps`` iterable of dataclass-ish items.
    class _FakeGap:
        def __init__(self, detail, severity):
            self.detail = detail
            self.severity = severity

    class _FakeGapReport:
        gaps = [_FakeGap("CONNECT BY not supported", "blocker")]

    report = build_report(_two_table_schema(), registry, gap_report=_FakeGapReport())
    assert len(report.gaps) == 1
    assert report.gaps[0]["detail"] == "CONNECT BY not supported"
    assert report.gaps[0]["severity"] == "blocker"
    # And it stays JSON-serializable end to end.
    json.loads(render_json(report))
