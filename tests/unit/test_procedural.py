"""Unit tests for procedural/advanced-object surfacing — Option A (no DB).

Covers build_procedural_gaps (routines/triggers/mviews/partitions -> DEGRADED
gaps), render_review (verbatim source + headers + v2.0.0 note), and the
inventory counts for materialized views and partitioned tables.
"""
from any2heliosdb.core.catalog_model import (
    Column, DataType, Partition, PartitionInfo, PartitionType, Routine, RoutineKind,
    Schema, Table, TableOptions, Trigger, View,
)
from any2heliosdb.assess.inventory import schema_inventory
from any2heliosdb.plsql.procedural import build_procedural_gaps, render_review


def _rich_schema() -> Schema:
    part_tbl = Table(
        name="SALES", schema="HR",
        columns=[Column("ID", DataType.decimal(10, 0))],
        options=TableOptions(partition=PartitionInfo(
            partition_type=PartitionType.RANGE, columns=["SOLD"],
            partitions=[Partition(name="P1", value="")])),
    )
    plain_tbl = Table(name="DEPT", schema="HR", columns=[Column("ID", DataType.decimal(10, 0))])
    return Schema(
        name="HR",
        tables=[plain_tbl, part_tbl],
        routines=[
            Routine(name="ADD_FN", kind=RoutineKind.FUNCTION, body="FUNCTION add ... END;"),
            Routine(name="LOG_PROC", kind=RoutineKind.PROCEDURE, body="PROCEDURE log ... END;"),
        ],
        triggers=[Trigger(name="AUD_TRG", table="EMP", timing="AFTER", events=["INSERT"],
                          body="BEGIN NULL; END;")],
        mviews=[View(name="SUMMARY_MV", definition="SELECT 1", materialized=True)],
    )


def test_build_procedural_gaps_covers_all_kinds():
    gaps = build_procedural_gaps(_rich_schema()).gaps
    feats = {g.feature for g in gaps}
    assert {"PL/SQL function", "PL/SQL procedure", "trigger",
            "materialized view", "partitioned table"} <= feats
    # All DEGRADED (the data tier still migrates) — never BLOCKER.
    assert all(g.severity.value == "degraded" for g in gaps)
    # The partitioned-table gap names its key column.
    part = next(g for g in gaps if g.feature == "partitioned table")
    assert "SOLD" in part.recommendation


def test_build_procedural_gaps_empty_schema():
    assert len(build_procedural_gaps(Schema(name="X")).gaps) == 0


def test_render_review_includes_bodies_and_headers():
    out = render_review(_rich_schema())
    assert "PL/SQL FUNCTION: ADD_FN" in out
    assert "TRIGGER: AUD_TRG ON EMP" in out
    assert "MATERIALIZED VIEW: SUMMARY_MV" in out
    assert "FUNCTION add ... END;" in out      # verbatim body preserved
    assert "v2.0.0" in out                      # roadmap note present


def test_render_review_empty_when_no_procedural_objects():
    assert render_review(Schema(name="X", tables=[Table(name="T", columns=[])])) == ""


def test_inventory_counts_mviews_and_partitions():
    counts = schema_inventory(_rich_schema())["counts"]
    assert counts["tables"] == 2
    assert counts["routines"] == 2
    assert counts["triggers"] == 1
    assert counts["materialized_views"] == 1
    assert counts["partitioned_tables"] == 1
