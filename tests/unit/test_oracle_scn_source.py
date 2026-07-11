"""Oracle SCN-watermark capture (cdc/sources/oracle_scn.py) — cheap, hermetic.

A fake adapter supplies ``current_scn()`` and ``stream_rows`` (recording the WHERE
it was handed). Pins the watermark advance/anchor logic: the new watermark is the
SCN captured BEFORE the scan (so commits during the scan are picked up next cycle,
never skipped), the WHERE filters on the OLD watermark, the first cycle is a full
snapshot, and a PK-less table is skipped (not captured) because a watermark upsert
needs a key.
"""
from __future__ import annotations

from any2heliosdb.cdc.sources.oracle_scn import OracleScnSource
from any2heliosdb.core.catalog_model import Column, DataType, PrimaryKey, Table
from any2heliosdb.core.change_record import UPDATE


class FakeAdapter:
    def __init__(self, scn, rows_by_table=None):
        self._scn = scn
        self._rows = rows_by_table or {}
        self.where_seen = {}

    def current_scn(self):
        return self._scn

    def stream_rows(self, table, columns, where=None):
        self.where_seen[table.name] = where
        return iter(self._rows.get(table.name, []))


def _emp():
    return Table(
        name="EMP", schema="HR",
        columns=[Column("ID", DataType.decimal(10, 0), nullable=False),
                 Column("NAME", DataType.varchar(50))],
        primary_key=PrimaryKey(columns=["ID"]))


def _pkless():
    return Table(name="LOG", schema="HR",
                 columns=[Column("MSG", DataType.varchar(50))])


def _src(adapter, tables):
    return OracleScnSource(adapter, "HR", tables)


def test_first_cycle_is_full_snapshot_and_advances_watermark():
    adapter = FakeAdapter(1500, {"EMP": [(1, "Al"), (2, "Bo")]})
    records, watermark, skipped = _src(adapter, [_emp()]).capture(since_scn=0)
    # since_scn <= 0 -> no ORA_ROWSCN filter (full snapshot)
    assert adapter.where_seen["EMP"] is None
    assert watermark == 1500                       # anchored to current_scn()
    assert skipped == []
    assert len(records) == 2
    r = records[0]
    assert r.op == UPDATE and r.schema == "HR" and r.table == "EMP"
    assert r.key == {"ID": 1}
    assert r.after == {"ID": 1, "NAME": "Al"}
    assert r.scn == 1500


def test_incremental_cycle_filters_on_old_watermark():
    adapter = FakeAdapter(2000, {"EMP": [(3, "Cy")]})
    records, watermark, _ = _src(adapter, [_emp()]).capture(since_scn=1500)
    # WHERE filters on the OLD watermark (1500), not the freshly-anchored 2000
    assert adapter.where_seen["EMP"] == "ORA_ROWSCN > 1500"
    assert watermark == 2000
    assert records[0].scn == 2000                  # stamped with the new anchor
    assert records[0].key == {"ID": 3}


def test_watermark_anchored_before_scan_never_skips_concurrent_commits():
    # The anchor is current_scn() taken BEFORE streaming; a commit that lands
    # during the scan keeps a higher SCN and is caught next cycle, not dropped.
    adapter = FakeAdapter(2000, {"EMP": []})
    records, watermark, _ = _src(adapter, [_emp()]).capture(since_scn=1000)
    assert records == []
    assert watermark == 2000                        # advances even with no rows


def test_pkless_table_is_skipped_not_captured():
    adapter = FakeAdapter(2000, {"LOG": [("hi",)], "EMP": [(1, "Al")]})
    records, _watermark, skipped = _src(adapter, [_pkless(), _emp()]).capture(since_scn=0)
    assert skipped == ["LOG"]                        # no PK -> no keyed upsert
    assert "LOG" not in adapter.where_seen           # never even scanned
    assert [r.table for r in records] == ["EMP"]


def test_zero_current_scn_keeps_prior_watermark():
    # Adapter can't advance the SCN (returns 0): keep the caller's watermark so a
    # later real SCN doesn't regress.
    adapter = FakeAdapter(0, {"EMP": []})
    _records, watermark, _ = _src(adapter, [_emp()]).capture(since_scn=1234)
    assert watermark == 1234
