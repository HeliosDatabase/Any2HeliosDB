"""Hermetic unit tests for MySQL binlog capture — proves a PK-changing UPDATE
emits ``before_key`` (BLOCKER 3c) without a live MySQL server.

``MySqlBinlogSource.capture`` imports ``pymysqlreplication`` lazily inside the
method, so we inject a fake module exposing the row-event classes and a fake
stream reader that yields synthetic events. The event instances are created from
the very classes the fake module exposes, so capture's ``isinstance`` dispatch
works exactly as against the real library.
"""
from __future__ import annotations

import sys
import types

from any2heliosdb.cdc.sources.mysql_binlog import MySqlBinlogSource, binlog_pos_to_int
from any2heliosdb.core.catalog_model import Column, DataType, PrimaryKey, Table
from any2heliosdb.core.change_record import DELETE, INSERT, UPDATE


class _FakeEvent:
    def __init__(self, table, rows):
        self.table = table
        self.rows = rows


class WriteRowsEvent(_FakeEvent):
    pass


class UpdateRowsEvent(_FakeEvent):
    pass


class DeleteRowsEvent(_FakeEvent):
    pass


class _Dsn:
    host = "127.0.0.1"
    port = 3306
    user = "cdc"
    password = ""


def _install_fake_reader(monkeypatch, events, log_file="mysql-bin.000009", log_pos=4242):
    class _FakeStream:
        def __init__(self, **kwargs):
            self.log_file = log_file
            self.log_pos = log_pos

        def __iter__(self):
            return iter(events)

        def close(self):
            pass

    root = types.ModuleType("pymysqlreplication")
    root.BinLogStreamReader = _FakeStream
    row_event = types.ModuleType("pymysqlreplication.row_event")
    row_event.WriteRowsEvent = WriteRowsEvent
    row_event.UpdateRowsEvent = UpdateRowsEvent
    row_event.DeleteRowsEvent = DeleteRowsEvent
    monkeypatch.setitem(sys.modules, "pymysqlreplication", root)
    monkeypatch.setitem(sys.modules, "pymysqlreplication.row_event", row_event)


def _orders_table():
    return Table(
        name="ORDERS", schema="db",
        columns=[Column("ID", DataType.decimal(10, 0)), Column("V", DataType.varchar(20))],
        primary_key=PrimaryKey(columns=["ID"]),
    )


def _source():
    return MySqlBinlogSource(_Dsn(), "db", [_orders_table()])


def test_capture_pk_changing_update_emits_before_key(monkeypatch):
    ev = UpdateRowsEvent("ORDERS", [{
        "before_values": {"ID": 1, "V": "old"},
        "after_values": {"ID": 2, "V": "new"},
    }])
    _install_fake_reader(monkeypatch, [ev])
    records, new_pos = _source().capture("mysql-bin.000001:4")
    assert new_pos == "mysql-bin.000009:4242"
    assert len(records) == 1
    r = records[0]
    assert r.op == UPDATE
    assert r.key == {"ID": 2}                 # new identity
    assert r.before_key == {"ID": 1}          # old identity -> replicat deletes it
    assert r.after == {"ID": 2, "V": "new"}


def test_capture_update_without_before_image_fails_closed(monkeypatch):
    # S4(2): a library-yielded UPDATE that omits before_values would manufacture an
    # all-None before_key (a bogus key-move that leaks the old row). Fail closed
    # with an actionable binlog_row_image=FULL message instead.
    import pytest

    from any2heliosdb.errors import Any2HeliosError
    ev = UpdateRowsEvent("ORDERS", [{"after_values": {"ID": 2, "V": "new"}}])  # no before_values
    _install_fake_reader(monkeypatch, [ev])
    with pytest.raises(Any2HeliosError) as ei:
        _source().capture("mysql-bin.000001:4")
    msg = str(ei.value).lower()
    assert "before-image" in msg and "binlog_row_image=full" in msg


def test_capture_update_with_empty_before_image_fails_closed(monkeypatch):
    # An empty before dict is equally unusable (all-None old key) -> fail closed.
    import pytest

    from any2heliosdb.errors import Any2HeliosError
    ev = UpdateRowsEvent("ORDERS", [{"before_values": {}, "after_values": {"ID": 2, "V": "n"}}])
    _install_fake_reader(monkeypatch, [ev])
    with pytest.raises(Any2HeliosError):
        _source().capture("mysql-bin.000001:4")


def test_capture_non_key_update_has_no_before_key(monkeypatch):
    ev = UpdateRowsEvent("ORDERS", [{
        "before_values": {"ID": 1, "V": "old"},
        "after_values": {"ID": 1, "V": "new"},
    }])
    _install_fake_reader(monkeypatch, [ev])
    records, _ = _source().capture("mysql-bin.000001:4")
    assert len(records) == 1 and records[0].before_key == {}
    assert records[0].key == {"ID": 1}


def test_capture_insert_and_delete_unaffected(monkeypatch):
    ins = WriteRowsEvent("ORDERS", [{"values": {"ID": 5, "V": "a"}}])
    dele = DeleteRowsEvent("ORDERS", [{"values": {"ID": 5, "V": "a"}}])
    _install_fake_reader(monkeypatch, [ins, dele])
    records, _ = _source().capture("mysql-bin.000001:4")
    assert [r.op for r in records] == [INSERT, DELETE]
    assert all(r.before_key == {} for r in records)


def test_capture_first_cycle_anchors_without_records(monkeypatch):
    # Empty position -> first cycle. capture() must NOT touch pymysqlreplication;
    # it anchors via current_position(). Stub that so no server is needed.
    src = _source()
    monkeypatch.setattr(src, "current_position", lambda: "mysql-bin.000001:4")
    records, pos = src.capture("")
    assert records == [] and pos == "mysql-bin.000001:4"


def _install_advancing_reader(monkeypatch, events_with_pos, log_file="mysql-bin.000003"):
    """Fake reader whose log_pos advances to each event's end as it is yielded
    (like the real BinLogStreamReader), so capture tags per-event source_pos."""
    class _FakeStream:
        def __init__(self, **kwargs):
            self.log_file = log_file
            self.log_pos = 0

        def __iter__(self):
            for ev, pos in events_with_pos:
                self.log_pos = pos
                yield ev

        def close(self):
            pass

    root = types.ModuleType("pymysqlreplication")
    root.BinLogStreamReader = _FakeStream
    row_event = types.ModuleType("pymysqlreplication.row_event")
    row_event.WriteRowsEvent = WriteRowsEvent
    row_event.UpdateRowsEvent = UpdateRowsEvent
    row_event.DeleteRowsEvent = DeleteRowsEvent
    monkeypatch.setitem(sys.modules, "pymysqlreplication", root)
    monkeypatch.setitem(sys.modules, "pymysqlreplication.row_event", row_event)


def test_capture_tags_each_record_with_monotonic_source_pos(monkeypatch):
    ins = WriteRowsEvent("ORDERS", [{"values": {"ID": 1, "V": "a"}}])
    upd = UpdateRowsEvent("ORDERS", [{
        "before_values": {"ID": 1, "V": "a"}, "after_values": {"ID": 1, "V": "b"}}])
    _install_advancing_reader(monkeypatch, [(ins, 120), (upd, 240)])
    records, new_pos = _source().capture("mysql-bin.000003:4")
    assert [r.source_pos for r in records] == [
        binlog_pos_to_int("mysql-bin.000003", 120),
        binlog_pos_to_int("mysql-bin.000003", 240),
    ]
    assert records[0].source_pos < records[1].source_pos   # monotonic for dedup
    assert new_pos == "mysql-bin.000003:240"


def test_capture_multi_row_event_shares_base_but_orders_rows(monkeypatch):
    # S2: all rows of one event share the event-end BASE coordinate, but each row
    # carries its ordinal as a compound [base, seq] so the RECORD stream is totally
    # ordered — a prefix crash can then re-append only the never-trailed tail rows
    # (the old all-share-one-int scheme dropped them forever).
    ins = WriteRowsEvent("ORDERS", [
        {"values": {"ID": 1, "V": "a"}}, {"values": {"ID": 2, "V": "b"}},
        {"values": {"ID": 3, "V": "c"}}])
    _install_advancing_reader(monkeypatch, [(ins, 500)])
    records, _ = _source().capture("mysql-bin.000003:4")
    base = binlog_pos_to_int("mysql-bin.000003", 500)
    assert len(records) == 3
    assert [r.source_pos for r in records] == [[base, 0], [base, 1], [base, 2]]
    from any2heliosdb.core.change_record import source_pos_key
    keys = [source_pos_key(r.source_pos) for r in records]
    assert keys == sorted(keys) and len(set(keys)) == 3   # strict total order


def test_capture_single_row_event_keeps_bare_int_pos(monkeypatch):
    # A singleton event stays wire-compatible: a plain int, which orders identically
    # to [base, 0] under source_pos_key.
    from any2heliosdb.core.change_record import source_pos_key
    ins = WriteRowsEvent("ORDERS", [{"values": {"ID": 9, "V": "z"}}])
    _install_advancing_reader(monkeypatch, [(ins, 720)])
    records, _ = _source().capture("mysql-bin.000003:4")
    base = binlog_pos_to_int("mysql-bin.000003", 720)
    assert records[0].source_pos == base                      # bare int, not [base, 0]
    assert source_pos_key(base) == source_pos_key([base, 0])  # but orders the same
