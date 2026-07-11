"""H1 — bounded memory: capture caps, batched apply, chunked reconcile.

Every knob bounds a resource the CDC pipeline would otherwise let grow without
limit (the reason tier-2 hardening followed the host OOM). These tests prove the
caps are honoured AND that a bounded run reaches the SAME final state / cursor as
an unbounded one (composition with the keymove barrier included).
"""
from __future__ import annotations

import sys
import types

from any2heliosdb.cdc.engine import run_replicat
from any2heliosdb.cdc.registry import CdcRegistry
from any2heliosdb.cdc.engine import _registry_path, _trail_dir
from any2heliosdb.cdc.replicat import Replicat
from any2heliosdb.cdc.trail import Trail
from any2heliosdb.config.model import CdcConfig, Options, ProjectConfig, SourceConfig, TargetConfig
from any2heliosdb.constants import Edition, SourceDialect, TargetDriverKind
from any2heliosdb.core.catalog_model import Column, DataType, PrimaryKey, Schema, Table
from any2heliosdb.core.change_record import INSERT, UPDATE, ChangeRecord
from any2heliosdb.target.base import CapabilityMatrix


# --- PG capture LIMIT ---------------------------------------------------------


class _RecordingPgAdapter:
    """Fake PG adapter capturing the peek SQL so we can assert the LIMIT."""

    def __init__(self):
        self.qall_sql = []

    def _q1(self, sql, *p):
        return (1,) if "pg_replication_slots" in sql else None

    def _qall(self, sql, *p):
        self.qall_sql.append(sql)
        return []


def _pg_table():
    return Table(name="actor", schema="public",
                 columns=[Column("actor_id", DataType.decimal(10, 0))],
                 primary_key=PrimaryKey(columns=["actor_id"]))


def test_pg_capture_passes_limit_as_upto_nchanges():
    from any2heliosdb.cdc.sources.postgres_logical import PostgresLogicalSource
    ad = _RecordingPgAdapter()
    src = PostgresLogicalSource(ad, "public", [_pg_table()], "e1")
    src.capture(limit=250)
    assert "pg_logical_slot_peek_changes(%s, NULL, 250)" in ad.qall_sql[-1]


def test_pg_capture_zero_limit_is_unbounded_null():
    from any2heliosdb.cdc.sources.postgres_logical import PostgresLogicalSource
    ad = _RecordingPgAdapter()
    PostgresLogicalSource(ad, "public", [_pg_table()], "e1").capture(limit=0)
    assert "pg_logical_slot_peek_changes(%s, NULL, NULL)" in ad.qall_sql[-1]


# --- MySQL capture stops after the cap ----------------------------------------


class _Ev:
    def __init__(self, table, rows):
        self.table = table
        self.rows = rows


class WriteRowsEvent(_Ev):
    pass


class UpdateRowsEvent(_Ev):
    pass


class DeleteRowsEvent(_Ev):
    pass


class _Dsn:
    host = "127.0.0.1"
    port = 3306
    user = "cdc"
    password = ""


def _install_reader(monkeypatch, events_with_pos, log_file="mysql-bin.000003"):
    class _Stream:
        def __init__(self, **kw):
            self.log_file = log_file
            self.log_pos = 0

        def __iter__(self):
            for ev, pos in events_with_pos:
                self.log_pos = pos
                yield ev

        def close(self):
            pass

    root = types.ModuleType("pymysqlreplication")
    root.BinLogStreamReader = _Stream
    row_event = types.ModuleType("pymysqlreplication.row_event")
    row_event.WriteRowsEvent = WriteRowsEvent
    row_event.UpdateRowsEvent = UpdateRowsEvent
    row_event.DeleteRowsEvent = DeleteRowsEvent
    monkeypatch.setitem(sys.modules, "pymysqlreplication", root)
    monkeypatch.setitem(sys.modules, "pymysqlreplication.row_event", row_event)


def _orders():
    return Table(name="ORDERS", schema="db",
                 columns=[Column("ID", DataType.decimal(10, 0)), Column("V", DataType.varchar(20))],
                 primary_key=PrimaryKey(columns=["ID"]))


def test_mysql_capture_stops_at_cap_and_reports_stop_coordinate(monkeypatch):
    from any2heliosdb.cdc.sources.mysql_binlog import MySqlBinlogSource
    events = [(WriteRowsEvent("ORDERS", [{"values": {"ID": i, "V": "a"}}]), 100 + i * 10)
              for i in range(1, 6)]
    _install_reader(monkeypatch, events)
    src = MySqlBinlogSource(_Dsn(), "db", [_orders()])
    records, new_pos = src.capture("mysql-bin.000003:4", limit=3)
    assert len(records) == 3                       # stopped after 3 events
    assert new_pos == "mysql-bin.000003:130"        # the 3rd event's END coordinate


def test_mysql_capture_zero_limit_reads_all(monkeypatch):
    from any2heliosdb.cdc.sources.mysql_binlog import MySqlBinlogSource
    events = [(WriteRowsEvent("ORDERS", [{"values": {"ID": i, "V": "a"}}]), 100 + i * 10)
              for i in range(1, 6)]
    _install_reader(monkeypatch, events)
    records, _ = MySqlBinlogSource(_Dsn(), "db", [_orders()]).capture("mysql-bin.000003:4", limit=0)
    assert len(records) == 5


# --- batched apply == unbatched final state (engine level) --------------------


def _table():
    return Table(name="t", schema="hr",
                 columns=[Column("ID", DataType.decimal(10, 0)), Column("V", DataType.varchar(10)),
                          Column("BODY", DataType.varchar(4000))],
                 primary_key=PrimaryKey(columns=["ID"]))


class _FakeAdapter:
    def connect(self):
        pass

    def introspect_schema(self, schema):
        return Schema(name="hr", tables=[_table()])

    def close(self):
        pass


class _FakeTarget:
    def __init__(self):
        self.rows = {}

    def connect(self):
        pass

    def close(self):
        pass

    def probe_capabilities(self):
        return CapabilityMatrix(edition=Edition.FULL, server_version="14.0 (HeliosDB)")

    def upsert(self, tt, key_cols, columns, rows):
        n = 0
        for r in rows:
            prov = dict(zip(columns, r))
            key = tuple(prov[k] for k in key_cols)
            merged = dict(self.rows.get((tt, key), {}))
            merged.update(prov)
            self.rows[(tt, key)] = merged
            n += 1
        return n

    def update_columns(self, tt, key_cols, set_cols, rows):
        nset = len(set_cols)
        matched = 0
        for r in rows:
            setvals, keyvals = r[:nset], r[nset:]
            key = tuple(keyvals)
            existing = self.rows.get((tt, key))
            if existing is None:
                continue
            existing = dict(existing)
            existing.update(dict(zip(set_cols, setvals)))
            newkey = tuple(existing[k] for k in key_cols)
            if newkey != key:
                self.rows.pop((tt, key), None)
            self.rows[(tt, newkey)] = existing
            matched += 1
        return matched

    def delete_keys(self, tt, key_cols, keys):
        n = 0
        for k in keys:
            self.rows.pop((tt, tuple(k)), None)
            n += 1
        return n


def _cfg(tmp_path, apply_batch):
    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.POSTGRESQL, host="h", port=5432,
                            database="hr", schema="hr", user="u", password="p"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG),
        options=Options(output_dir=str(tmp_path)),
        cdc=CdcConfig(apply_batch=apply_batch, poison_retries=0))


def _wire(monkeypatch, target):
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())
    monkeypatch.setattr(store, "build_target_driver", lambda cfg: target)


def _ins(idv, v, body):
    return ChangeRecord(op=INSERT, schema="hr", table="t", key={"ID": idv},
                        after={"ID": idv, "V": v, "BODY": body})


def _keymove(new_id, old_id, v):
    return ChangeRecord(op=UPDATE, schema="hr", table="t", key={"ID": new_id},
                        after={"ID": new_id, "V": v}, before_key={"ID": old_id})


_SLICE = [_ins(1, "a", "B1"), _ins(2, "b", "B2"), _keymove(3, 1, "moved"),
          _ins(4, "d", "B4"), _keymove(5, 2, "moved2"), _ins(6, "f", "B6")]


def _run(tmp_path, monkeypatch, apply_batch, name):
    cfg = _cfg(tmp_path, apply_batch)
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "hr", ["t"])
    reg.close()
    Trail(_trail_dir(cfg, name)).append(list(_SLICE))
    target = _FakeTarget()
    _wire(monkeypatch, target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    return target.rows, res


def test_batched_apply_matches_unbatched_final_state(tmp_path, monkeypatch):
    unbatched, ru = _run(tmp_path, monkeypatch, 0, "u")
    batched, rb = _run(tmp_path, monkeypatch, 2, "b")
    assert batched == unbatched
    assert rb["cursor"] == ru["cursor"] == len(_SLICE)
    assert rb["applied"] == ru["applied"] == len(_SLICE)
    # The keymoves resolved: row 1->3, row 2->5, BODY preserved across the moves.
    assert unbatched[("t", (3,))] == {"id": 3, "v": "moved", "body": "B1"}
    assert unbatched[("t", (5,))] == {"id": 5, "v": "moved2", "body": "B2"}


def test_batched_apply_crash_between_batches_converges(tmp_path, monkeypatch):
    # A crash after chunk K persists the exact per-chunk cursor; resuming replays
    # only the remaining chunks. Final state == a clean single run.
    expected, _ = _run(tmp_path, monkeypatch, 0, "expect")

    cfg = _cfg(tmp_path, 2)
    name = "resume"
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "hr", ["t"])
    reg.close()
    Trail(_trail_dir(cfg, name)).append(list(_SLICE))
    target = _FakeTarget()
    _wire(monkeypatch, target)
    # First pass runs to completion, then we rewind the cursor to simulate a crash
    # that lost the last persisted cursor and re-run — must converge idempotently.
    run_replicat(cfg, name, reconcile_deletes=False)
    for rewind in (0, 2, 4):
        reg = CdcRegistry(_registry_path(cfg))
        reg.set_apply_cursor(name, rewind)
        reg.close()
        run_replicat(cfg, name, reconcile_deletes=False)
        assert target.rows == expected, rewind


# --- reconcile chunking equivalence -------------------------------------------


class _ReconcileTarget:
    def __init__(self, tgt_keys):
        self._keys = list(tgt_keys)
        self.deleted = []
        self.delete_batches = []

    def select_keys(self, target_table, key_cols):
        return list(self._keys)

    def delete_keys(self, target_table, key_cols, keys):
        keys = [tuple(k) for k in keys]
        self.delete_batches.append(len(keys))
        self.deleted += keys
        return len(keys)


class _ReconcileAdapter:
    def __init__(self, src_keys):
        self._src = list(src_keys)

    def stream_rows(self, table, cols, where=None, arraysize=1000):
        for k in self._src:
            yield (k,)


def _recon_schema():
    return Schema(name="hr", tables=[
        Table(name="t", schema="hr", columns=[Column("ID", DataType.decimal(10, 0))],
              primary_key=PrimaryKey(columns=["ID"]))])


def test_reconcile_chunked_equals_unchunked(tmp_path):
    src_keys = [1, 2, 3]
    tgt_keys = [(1,), (2,), (3,), (4,), (5,), (6,), (7,)]  # 4..7 are surplus
    # Unchunked
    t0 = _ReconcileTarget(tgt_keys)
    d0, w0 = Replicat(t0, _recon_schema()).reconcile_deletes(_ReconcileAdapter(src_keys))
    # Chunked (size 2)
    t1 = _ReconcileTarget(tgt_keys)
    d1, w1 = Replicat(t1, _recon_schema()).reconcile_deletes(_ReconcileAdapter(src_keys), chunk_size=2)
    assert w0 == w1 == []
    assert d0 == d1 == 4
    assert sorted(t0.deleted) == sorted(t1.deleted) == [(4,), (5,), (6,), (7,)]
    # Chunking really did split the delete into bounded batches.
    assert t0.delete_batches == [4]
    assert all(b <= 2 for b in t1.delete_batches) and sum(t1.delete_batches) == 4
