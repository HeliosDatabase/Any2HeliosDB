"""H2 — tables created after the first extract.

The capture set is PINNED to the registry: a table that appears in the source
after registration is reported (a warning each cycle) but NOT captured, until an
explicit ``a2h extract NAME --refresh-tables`` snapshot-loads its current rows and
adopts it (so its CDC events flow from the next cycle). Hermetic — a fake adapter
supplies a mutable schema + snapshot rows, and a fake PG source returns no change
events so the tests isolate table adoption.
"""
from __future__ import annotations

import pytest

from any2heliosdb.cdc.engine import _registry_path, _trail_dir, run_extract
from any2heliosdb.cdc.registry import CdcRegistry
from any2heliosdb.cdc.trail import Trail
from any2heliosdb.config.model import Options, ProjectConfig, SourceConfig, TargetConfig
from any2heliosdb.constants import SourceDialect, TargetDriverKind
from any2heliosdb.core.catalog_model import Column, DataType, PrimaryKey, Schema, Table


def _t(name, pk=True):
    return Table(name=name, schema="public",
                 columns=[Column("ID", DataType.decimal(10, 0)), Column("V", DataType.varchar(20))],
                 primary_key=PrimaryKey(columns=["ID"]) if pk else None)


class _FakeAdapter:
    """Schema is mutable across cycles; stream_rows serves snapshot rows."""
    tables = [_t("t1")]
    rows = {"t2": [(1, "a"), (2, "b"), (3, "c")], "t1": [(9, "x")]}

    def connect(self):
        pass

    def introspect_schema(self, schema):
        return Schema(name="public", tables=list(type(self).tables))

    def stream_rows(self, table, cols, where=None, arraysize=1000):
        for row in type(self).rows.get(table.name, []):
            yield row

    def close(self):
        pass


class _FakePgSource:
    """No change events — isolates the new-table / snapshot behaviour."""
    def __init__(self, adapter, schema, tables, name):
        self.tables = tables

    def epoch_identity(self):
        return None

    def capture(self, limit=0):
        return [], "0/200", []

    def advance(self, lsn):
        pass


def _cfg(tmp_path):
    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.POSTGRESQL, host="h", port=5432,
                            database="hr", schema="public", user="u", password="p"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG),
        options=Options(output_dir=str(tmp_path)))


def _wire(monkeypatch):
    from any2heliosdb.config import store
    from any2heliosdb.cdc.sources import postgres_logical
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())
    monkeypatch.setattr(postgres_logical, "PostgresLogicalSource", _FakePgSource)


def test_new_table_reported_not_captured_until_refresh(tmp_path, monkeypatch):
    _FakeAdapter.tables = [_t("t1")]
    _wire(monkeypatch)
    cfg = _cfg(tmp_path)
    name = "e1"

    # 1) First extract registers t1; nothing is "new".
    r1 = run_extract(cfg, name)
    assert r1["new_tables"] == [] and r1["snapshotted"] == 0
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name).tables == ["t1"]
    reg.close()

    # 2) t2 appears in the source. A routine extract REPORTS it but does NOT
    #    capture it or adopt it (registry stays pinned to [t1]).
    _FakeAdapter.tables = [_t("t1"), _t("t2")]
    r2 = run_extract(cfg, name)
    assert r2["new_tables"] == ["t2"] and r2["snapshotted"] == 0
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name).tables == ["t1"]      # still pinned — not silently absorbed
    reg.close()

    # 3) --refresh-tables snapshot-loads t2's current rows AND adopts it.
    r3 = run_extract(cfg, name, refresh_tables=True)
    assert r3["snapshotted"] == 3              # t2's three current rows
    reg = CdcRegistry(_registry_path(cfg))
    assert sorted(reg.get(name).tables) == ["t1", "t2"]   # adopted
    reg.close()
    # The snapshot rows are in the trail as INSERT records for t2.
    out, _ = Trail(_trail_dir(cfg, name)).read(0)
    t2_rows = [rec for rec in out if rec.table == "t2"]
    assert [rec.key["ID"] for rec in t2_rows] == [1, 2, 3]
    assert all(rec.op == "I" for rec in t2_rows)

    # 4) Now that t2 is adopted, a routine extract no longer flags it as new.
    r4 = run_extract(cfg, name)
    assert r4["new_tables"] == []


def test_snapshot_records_carry_source_pos_for_dedup(tmp_path, monkeypatch):
    # Snapshot records are tagged at the advance coordinate so the trail tail keeps
    # a comparable position — extract-start dedup stays enabled on the next cycle.
    _FakeAdapter.tables = [_t("t1")]
    _wire(monkeypatch)
    cfg = _cfg(tmp_path)
    name = "e2"
    run_extract(cfg, name)
    _FakeAdapter.tables = [_t("t1"), _t("t2")]
    run_extract(cfg, name, refresh_tables=True)
    tail = Trail(_trail_dir(cfg, name)).last_source_pos()
    assert tail is not None                    # not None -> dedup stays enabled


def test_adopt_deferred_until_snapshot_lands(tmp_path, monkeypatch):
    # B3: reg.register(adopt) must NOT persist before the snapshot is durably in
    # the trail. If the snapshot read fails mid-refresh, the tables stay "new"
    # (unadopted) so the next --refresh-tables re-snapshots them.
    _FakeAdapter.tables = [_t("t1")]
    _wire(monkeypatch)
    cfg = _cfg(tmp_path)
    name = "b3"
    run_extract(cfg, name)                         # register t1
    _FakeAdapter.tables = [_t("t1"), _t("t2")]

    orig_stream = _FakeAdapter.stream_rows

    def _boom(self, table, cols, where=None, arraysize=1000):
        raise RuntimeError("snapshot read failed")
        yield  # pragma: no cover

    monkeypatch.setattr(_FakeAdapter, "stream_rows", _boom)
    with pytest.raises(RuntimeError):
        run_extract(cfg, name, refresh_tables=True)
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name).tables == ["t1"]          # NOT adopted (snapshot never landed)
    reg.close()

    # The new-table warning still fires on a routine cycle.
    r = run_extract(cfg, name)
    assert r["new_tables"] == ["t2"]

    # A working re-run snapshots t2 fully and adopts it.
    monkeypatch.setattr(_FakeAdapter, "stream_rows", orig_stream)
    r3 = run_extract(cfg, name, refresh_tables=True)
    assert r3["snapshotted"] == 3
    reg = CdcRegistry(_registry_path(cfg))
    assert sorted(reg.get(name).tables) == ["t1", "t2"]
    reg.close()


def test_refresh_skips_pk_less_new_table(tmp_path, monkeypatch):
    _FakeAdapter.tables = [_t("t1")]
    _wire(monkeypatch)
    cfg = _cfg(tmp_path)
    name = "e3"
    run_extract(cfg, name)
    _FakeAdapter.tables = [_t("t1"), _t("nopk", pk=False)]
    r = run_extract(cfg, name, refresh_tables=True)
    assert r["snapshotted"] == 0               # PK-less table can't be keyed
    assert "nopk" in r["skipped"]
    # It is still adopted into the registry (so it stops being flagged "new").
    reg = CdcRegistry(_registry_path(cfg))
    assert sorted(reg.get(name).tables) == ["nopk", "t1"]
    reg.close()


def test_comma_table_name_fails_closed_at_cycle_start(tmp_path, monkeypatch):
    # Minor residual: the comma-name rejection must fire BEFORE any capture or
    # snapshot work — previously it fired inside the post-snapshot adopt, so
    # every --refresh-tables retry appended a full snapshot then raised.
    import os

    from any2heliosdb.errors import Any2HeliosError

    cfg = _cfg(tmp_path)

    class _CommaTable:
        name = "T,BAD"
        primary_key = None

    class _Schema:
        name = "hr"
        tables = [_CommaTable()]

    class _Adapter:
        def connect(self):
            pass

        def close(self):
            pass

        def introspect_schema(self, schema):
            return _Schema()

    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda c: _Adapter())
    with pytest.raises(Any2HeliosError, match="comma"):
        run_extract(cfg, "ct1")
    # Nothing was captured or snapshotted before the raise.
    assert not os.path.exists(os.path.join(str(tmp_path), "trail", "ct1", "trail.jsonl"))
