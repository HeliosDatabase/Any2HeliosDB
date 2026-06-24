"""Unit tests for the CDC spine: change-record codec, trail cursor, registry."""
import datetime
from decimal import Decimal

from any2heliosdb.cdc.registry import CdcRegistry
from any2heliosdb.cdc.trail import Trail
from any2heliosdb.core.change_record import UPDATE, ChangeRecord


def test_change_record_roundtrip_preserves_types():
    r = ChangeRecord(
        op=UPDATE, schema="HR", table="EMP",
        key={"ID": Decimal("5")},
        after={"ID": Decimal("5"), "NAME": "x",
               "HIRED": datetime.datetime(2020, 1, 2, 3, 4, 5),
               "PHOTO": b"\x00\x01\xff", "BAL": Decimal("123.45"), "NIL": None},
        scn=999)
    r2 = ChangeRecord.from_json(r.to_json())
    assert r2.op == "U" and r2.table == "EMP" and r2.scn == 999
    assert isinstance(r2.key["ID"], Decimal) and r2.key["ID"] == Decimal("5")
    assert r2.after["HIRED"] == datetime.datetime(2020, 1, 2, 3, 4, 5)
    assert r2.after["PHOTO"] == b"\x00\x01\xff"
    assert r2.after["BAL"] == Decimal("123.45")
    assert r2.after["NIL"] is None


def test_trail_append_and_incremental_cursor(tmp_path):
    t = Trail(str(tmp_path))
    recs = [ChangeRecord(op=UPDATE, schema="S", table="T", key={"id": i}, after={"id": i})
            for i in range(3)]
    assert t.append(recs) == 3
    out, cur = t.read(0)
    assert len(out) == 3 and cur == 3
    # reading again from the advanced cursor yields nothing
    out2, cur2 = t.read(cur)
    assert out2 == [] and cur2 == 3
    # appended records are visible from the old cursor only
    t.append([ChangeRecord(op=UPDATE, schema="S", table="T", key={"id": 9}, after={"id": 9})])
    out3, cur3 = t.read(cur)
    assert len(out3) == 1 and out3[0].key["id"] == 9 and cur3 == 4


def test_registry_tracks_watermark_and_cursor(tmp_path):
    reg = CdcRegistry(str(tmp_path / "cdc.db"))
    try:
        reg.register("e1", "HR", ["A", "B"])
        e = reg.get("e1")
        assert e is not None and e.watermark == 0 and e.apply_cursor == 0 and e.tables == ["A", "B"]
        reg.set_watermark("e1", 12345)
        reg.set_apply_cursor("e1", 7)
        e = reg.get("e1")
        assert e.watermark == 12345 and e.apply_cursor == 7
        assert [x.name for x in reg.list()] == ["e1"]
    finally:
        reg.close()


def test_version_tuple_parses_helios_banner():
    from any2heliosdb.cdc.engine import _version_tuple
    assert _version_tuple("HeliosDB-Nano 3.58.2") == (3, 58, 2)
    assert _version_tuple("3.58.1") == (3, 58, 1)
    assert _version_tuple("v3.60.0-rc1") == (3, 60, 0)
    assert _version_tuple("nano-no-version") is None
    assert _version_tuple("") is None
    assert _version_tuple(None) is None


def test_nano_cdc_apply_gate_threshold():
    # The replicat gate allows Nano only from the #34 fix (v3.58.2) onward; older
    # or unparseable versions are refused so a pre-fix Nano can't silently corrupt
    # keyed upserts (ON CONFLICT DO UPDATE quoted SET target).
    from any2heliosdb.cdc.engine import _NANO_MIN_CDC_VERSION, _version_tuple

    def allowed(v):
        t = _version_tuple(v)
        return t is not None and t >= _NANO_MIN_CDC_VERSION

    assert allowed("3.58.3") and allowed("3.59.0") and allowed("4.0.0")
    assert not allowed("3.58.2") and not allowed("3.58.1") and not allowed("3.0.0")
    assert not allowed("unknown")


# --- Replicat ordered-apply (BLOCKER 1) ---------------------------------------

from any2heliosdb.cdc.replicat import Replicat  # noqa: E402
from any2heliosdb.core.catalog_model import (  # noqa: E402
    Column,
    DataType,
    PrimaryKey,
    Schema,
    Table,
)
from any2heliosdb.core.change_record import DELETE, INSERT  # noqa: E402


class _FakeTarget:
    """In-memory apply seam that mirrors row state and logs the call order.

    Keyed by (table, key-tuple) so we can assert final presence/absence after a
    mixed I/U/D stream, and ``calls`` records the op-class sequence to prove the
    replicat didn't reorder upserts ahead of deletes.
    """

    def __init__(self):
        self.rows = {}
        self.calls = []

    def upsert(self, target_table, key_cols, columns, rows):
        rows = list(rows)
        self.calls.append(("upsert", target_table, len(rows)))
        n = 0
        for r in rows:
            row = dict(zip(columns, r))
            key = tuple(row[k] for k in key_cols)
            self.rows[(target_table, key)] = row
            n += 1
        return n

    def delete_keys(self, target_table, key_cols, keys):
        keys = list(keys)
        self.calls.append(("delete", target_table, len(keys)))
        n = 0
        for k in keys:
            self.rows.pop((target_table, tuple(k)), None)
            n += 1
        return n


def _schema(*tables):
    return Schema(name="HR", tables=list(tables))


def _t(name, key_col="ID"):
    return Table(
        name=name,
        schema="HR",
        columns=[Column(key_col, DataType.decimal(10, 0)), Column("V", DataType.varchar(10))],
        primary_key=PrimaryKey(columns=[key_col]),
    )


def _rec(op, table, idval, v=None):
    after = {} if op == DELETE else {"ID": idval, "V": v}
    return ChangeRecord(op=op, schema="HR", table=table, key={"ID": idval}, after=after)


def test_apply_delete_then_insert_leaves_row_present():
    # D id=7 then I id=7 must end PRESENT (the original split-by-op apply upserted
    # first then deleted, wrongly dropping the row).
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T")))
    applied, warnings = rep.apply([_rec(DELETE, "T", 7), _rec(INSERT, "T", 7, "new")])
    assert warnings == []
    assert applied == 2
    assert ("t", (7,)) in tgt.rows and tgt.rows[("t", (7,))]["v"] == "new"
    # And the delete batch must have run before the upsert batch.
    assert [c[0] for c in tgt.calls] == ["delete", "upsert"]


def test_apply_insert_delete_insert_leaves_row_present():
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T")))
    rep.apply([_rec(INSERT, "T", 7, "a"), _rec(DELETE, "T", 7), _rec(INSERT, "T", 7, "b")])
    assert tgt.rows[("t", (7,))]["v"] == "b"
    assert [c[0] for c in tgt.calls] == ["upsert", "delete", "upsert"]


def test_apply_insert_then_delete_leaves_row_absent():
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T")))
    rep.apply([_rec(INSERT, "T", 7, "a"), _rec(DELETE, "T", 7)])
    assert ("t", (7,)) not in tgt.rows
    assert [c[0] for c in tgt.calls] == ["upsert", "delete"]


def test_apply_contiguous_same_class_records_batch_into_one_call():
    # Ordering is preserved without sacrificing batching: a run of upserts is a
    # single driver round-trip, likewise a run of deletes.
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T")))
    rep.apply([
        _rec(INSERT, "T", 1, "a"), _rec(INSERT, "T", 2, "b"),
        _rec(DELETE, "T", 1), _rec(DELETE, "T", 2),
        _rec(INSERT, "T", 3, "c"),
    ])
    assert tgt.calls == [("upsert", "t", 2), ("delete", "t", 2), ("upsert", "t", 1)]
    assert ("t", (1,)) not in tgt.rows and ("t", (2,)) not in tgt.rows
    assert tgt.rows[("t", (3,))]["v"] == "c"


def test_apply_interleaved_keys_across_two_tables():
    # Two tables, interleaved in arrival order; each table's own order is honored.
    # T1: D1 then I1 -> present. T2: I2 then D2 -> absent.
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T1"), _t("T2")))
    rep.apply([
        _rec(DELETE, "T1", 1),
        _rec(INSERT, "T2", 2, "x"),
        _rec(INSERT, "T1", 1, "y"),
        _rec(DELETE, "T2", 2),
    ])
    assert tgt.rows[("t1", (1,))]["v"] == "y"
    assert ("t2", (2,)) not in tgt.rows


def test_apply_skips_table_without_primary_key():
    tgt = _FakeTarget()
    nopk = Table(name="NOPK", schema="HR", columns=[Column("V", DataType.varchar(10))])
    rep = Replicat(tgt, _schema(nopk))
    applied, warnings = rep.apply([_rec(INSERT, "NOPK", 1, "a")])
    assert applied == 0 and tgt.calls == []
    assert len(warnings) == 1 and "no primary key" in warnings[0]


# --- MySQL binlog fail-closed verification (BLOCKER 2) ------------------------

import pytest  # noqa: E402

from any2heliosdb.cdc.sources.mysql_binlog import (  # noqa: E402
    _check_image_columns,
    _require_full_row_image,
)
from any2heliosdb.errors import Any2HeliosError  # noqa: E402


def _full_vars():
    return {"binlog_format": "ROW", "binlog_row_metadata": "FULL", "binlog_row_image": "FULL"}


def test_require_full_row_image_accepts_full_settings():
    # Case-insensitive on both names and values; no raise on a fully-FULL server.
    _require_full_row_image(_full_vars())
    _require_full_row_image({"BINLOG_FORMAT": "row", "binlog_row_metadata": "full",
                             "binlog_row_image": "Full"})


def test_require_full_row_image_rejects_minimal_row_image():
    v = _full_vars()
    v["binlog_row_image"] = "MINIMAL"
    with pytest.raises(Any2HeliosError) as ei:
        _require_full_row_image(v)
    assert "binlog_row_image" in str(ei.value)


def test_require_full_row_image_rejects_statement_format():
    v = _full_vars()
    v["binlog_format"] = "STATEMENT"
    with pytest.raises(Any2HeliosError) as ei:
        _require_full_row_image(v)
    assert "binlog_format" in str(ei.value)


def test_require_full_row_image_rejects_minimal_metadata():
    v = _full_vars()
    v["binlog_row_metadata"] = "MINIMAL"
    with pytest.raises(Any2HeliosError) as ei:
        _require_full_row_image(v)
    assert "binlog_row_metadata" in str(ei.value)


def test_require_full_row_image_rejects_missing_variable():
    # An unreadable variable (denied privilege) must fail closed, not pass.
    v = _full_vars()
    del v["binlog_row_image"]
    with pytest.raises(Any2HeliosError):
        _require_full_row_image(v)


def test_check_image_columns_accepts_full_image():
    _check_image_columns("T", "UPDATE", {"ID", "V", "NAME"}, ["ID", "V", "NAME"])


def test_check_image_columns_rejects_unknown_col_markers():
    with pytest.raises(Any2HeliosError) as ei:
        _check_image_columns("T", "INSERT", {"UNKNOWN_COL0", "UNKNOWN_COL1"}, ["ID", "V"])
    assert "binlog_row_metadata" in str(ei.value)


def test_check_image_columns_rejects_partial_image():
    # MINIMAL UPDATE: after-image omits the unchanged column -> would be NULLed.
    with pytest.raises(Any2HeliosError) as ei:
        _check_image_columns("T", "UPDATE", {"ID"}, ["ID", "V", "NAME"])
    assert "binlog_row_image" in str(ei.value)
