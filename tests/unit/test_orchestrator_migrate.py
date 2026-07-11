"""core/orchestrator.migrate() end-to-end with fakes at the adapter/driver seams.

Hermetic: a fake SourceAdapter serves the IR + row streams, and a fake
TargetDriver records every DDL/execute and load call — no live server. The tests
pin the documented contract of migrate():

* schema DDL emission -> data load -> FK-after-data -> views, in that order;
* the exit-state contract: ``stats.failed_chunks = chunks_total - chunks_loaded``
  (a partial load surfaces as > 0, not a silent success), the drop_existing reset
  threading (``fresh=drop_existing``), and the row-count map keyed by source name;
* ``batch_size`` / ``parallelism`` / ``preserve_case`` threading into the loader
  and into ``stream_rows``;
* the native-Oracle sequential fallback (inline INSERT, NO resumable loader) vs
  the resumable/parallel default on a PG-wire target.
"""
from __future__ import annotations

import types

import pytest

from any2heliosdb.constants import Edition
from any2heliosdb.core.catalog_model import (
    Column,
    DataType,
    ForeignKey,
    Index,
    IndexColumn,
    PrimaryKey,
    Schema,
    Sequence,
    Table,
    View,
)
from any2heliosdb.core.orchestrator import migrate
from any2heliosdb.target.base import CapabilityMatrix


# --- fakes -------------------------------------------------------------------
class FakeSource:
    """Serves introspect_schema + stream_rows; records stream arraysizes."""

    def __init__(self, schema, rows_by_table=None):
        self._schema = schema
        self._rows = rows_by_table or {}
        self.stream_arraysizes = []
        self.introspect_calls = []

    def introspect_schema(self, schema=None):
        self.introspect_calls.append(schema)
        return self._schema

    def stream_rows(self, table, columns, where=None, arraysize=1000):
        self.stream_arraysizes.append(arraysize)
        return iter(self._rows.get(table.name, []))


class FakeTarget:
    dialect = "postgres"

    def __init__(self, caps, copy_raises=False):
        self.capabilities = caps
        self.executed = []            # every execute() SQL, in order
        self.copies = []              # (table, cols, rowcount)
        self.inserts = []             # (table, cols, rowcount)
        self.reconnects = 0
        self.probe_called = False
        self._copy_raises = copy_raises

    def probe_capabilities(self):
        self.probe_called = True
        return self.capabilities

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def copy_rows(self, target_table, columns, rows):
        if self._copy_raises:
            # consume nothing; simulate a mid-COPY protocol error
            raise RuntimeError("boom mid-COPY")
        materialized = list(rows)
        self.copies.append((target_table, list(columns), len(materialized)))
        return len(materialized)

    def insert_rows(self, target_table, columns, rows, on_conflict_do_nothing=False):
        materialized = list(rows)
        self.inserts.append((target_table, list(columns), len(materialized)))
        return len(materialized)

    def close(self):
        pass

    def connect(self):
        self.reconnects += 1


class OracleFakeTarget(FakeTarget):
    dialect = "oracle"


def _caps(edition=Edition.POSTGRES, copy=True, concurrent=True):
    return CapabilityMatrix(edition=edition, copy_from_stdin=copy,
                            concurrent_writes=concurrent, server_version="x")


def _schema():
    dept = Table(
        name="dept", schema="hr",
        columns=[Column("id", DataType.decimal(10, 0), nullable=False),
                 Column("name", DataType.varchar(50))],
        primary_key=PrimaryKey(columns=["id"]))
    emp = Table(
        name="emp", schema="hr",
        columns=[Column("id", DataType.decimal(10, 0), nullable=False),
                 Column("dept_id", DataType.decimal(10, 0))],
        primary_key=PrimaryKey(columns=["id"]),
        foreign_keys=[ForeignKey(columns=["dept_id"], references_table="dept",
                                 references_columns=["id"], name="emp_dept_fk")],
        indexes=[Index(name="emp_dept_ix", columns=[IndexColumn("dept_id")])])
    return Schema("hr", tables=[dept, emp],
                  sequences=[Sequence(name="emp_seq", start=1)],
                  views=[View(name="emp_v", definition="SELECT id FROM emp")])


def _first_idx(target, needle):
    for i, s in enumerate(target.executed):
        if needle in s:
            return i
    raise AssertionError("{!r} not found in {}".format(needle, target.executed))


# --- sequential (cfg=None) reference path ------------------------------------
def test_sequential_migrate_emits_ddl_load_fk_view_in_order():
    src = FakeSource(_schema(), {"dept": [(1, "eng"), (2, "ops")], "emp": [(10, 1)]})
    tgt = FakeTarget(_caps())
    stats = migrate(src, tgt, schema="hr", batch_size=250)

    assert tgt.probe_called is False          # edition already known -> no probe
    assert stats.tables == 2
    assert stats.load_mode == "copy"          # copy_from_stdin True + prefer_copy
    # rows keyed by SOURCE table name; counts from the streamed rows
    assert stats.rows == {"dept": 2, "emp": 1}
    assert stats.failed_chunks == 0

    # DROP happens child-before-parent (reversed): emp before dept, with CASCADE.
    assert _first_idx(tgt, "DROP TABLE IF EXISTS emp CASCADE") < \
        _first_idx(tgt, "DROP TABLE IF EXISTS dept CASCADE")
    # Sequence created before the tables; FK added after the tables; view last.
    assert _first_idx(tgt, "CREATE SEQUENCE emp_seq") < _first_idx(tgt, "CREATE TABLE dept")
    assert _first_idx(tgt, "CREATE TABLE emp") < _first_idx(tgt, "ADD CONSTRAINT")
    assert "FOREIGN KEY" in tgt.executed[_first_idx(tgt, "ADD CONSTRAINT")]
    assert _first_idx(tgt, "CREATE INDEX emp_dept_ix") > _first_idx(tgt, "CREATE TABLE emp")
    assert _first_idx(tgt, "CREATE VIEW emp_v") > _first_idx(tgt, "ADD CONSTRAINT")

    # COPY fast path used with lowercased table names; batch_size -> arraysize.
    assert [t for (t, _c, _n) in tgt.copies] == ["dept", "emp"]
    assert tgt.inserts == []
    assert src.stream_arraysizes == [250, 250]


def test_insert_mode_when_copy_unsupported():
    src = FakeSource(_schema(), {"dept": [(1, "x")], "emp": []})
    tgt = FakeTarget(_caps(copy=False))
    stats = migrate(src, tgt, schema="hr")
    assert stats.load_mode == "insert"
    assert tgt.copies == []
    assert [t for (t, _c, _n) in tgt.inserts] == ["dept", "emp"]


def test_prefer_copy_false_forces_insert_even_when_supported():
    src = FakeSource(_schema(), {"dept": [(1, "x")], "emp": []})
    tgt = FakeTarget(_caps(copy=True))
    stats = migrate(src, tgt, schema="hr", prefer_copy=False)
    assert stats.load_mode == "insert" and tgt.copies == []


def test_copy_failure_falls_back_to_insert_with_reconnect_and_warning():
    src = FakeSource(_schema(), {"dept": [(1, "x")], "emp": [(2, 1)]})
    tgt = FakeTarget(_caps(copy=True), copy_raises=True)
    stats = migrate(src, tgt, schema="hr")
    # every table's COPY failed -> retried via INSERT after a reconnect
    assert tgt.copies == []
    assert [t for (t, _c, _n) in tgt.inserts] == ["dept", "emp"]
    assert tgt.reconnects == 2
    assert sum("retrying via INSERT" in w for w in stats.warnings) == 2


def test_preserve_case_threads_into_ddl_and_load():
    t = Table(name="Emp", schema="HR",
              columns=[Column("Id", DataType.decimal(10, 0), nullable=False)],
              primary_key=PrimaryKey(columns=["Id"]))
    src = FakeSource(Schema("HR", tables=[t]), {"Emp": [(1,)]})
    tgt = FakeTarget(_caps())
    migrate(src, tgt, schema="HR", preserve_case=True)
    # mixed-case identifiers kept + quoted; load target keeps source case.
    assert '"Emp"' in tgt.executed[_first_idx(tgt, "CREATE TABLE")]
    assert tgt.copies[0][0] == "Emp"


def test_do_schema_false_skips_ddl_but_still_loads():
    src = FakeSource(_schema(), {"dept": [(1, "x")], "emp": []})
    tgt = FakeTarget(_caps())
    stats = migrate(src, tgt, schema="hr", do_schema=False)
    assert stats.tables == 2                       # counted from the source IR
    assert not any(s.startswith("CREATE TABLE") for s in tgt.executed)
    assert not any(s.startswith("DROP TABLE") for s in tgt.executed)
    assert [t for (t, _c, _n) in tgt.copies] == ["dept", "emp"]


def test_drop_existing_false_emits_no_drops():
    src = FakeSource(_schema(), {"dept": [], "emp": []})
    tgt = FakeTarget(_caps())
    migrate(src, tgt, schema="hr", drop_existing=False)
    assert not any(s.startswith("DROP TABLE") for s in tgt.executed)
    assert not any(s.startswith("DROP SEQUENCE") for s in tgt.executed)
    assert any(s.startswith("CREATE TABLE") for s in tgt.executed)   # still created


# --- resumable / parallel default path ---------------------------------------
def _fake_loader_factory(captured, ls):
    class _FakeLoader:
        def __init__(self, cfg, schema, manifest_path, run_id, **kw):
            captured["kw"] = kw
            captured["run_id"] = run_id

        def run(self):
            return ls

    return _FakeLoader


def test_resumable_path_threads_knobs_and_reports_partial_load(monkeypatch):
    from any2heliosdb.core import loader as loader_mod

    schema = _schema()
    src = FakeSource(schema)
    tgt = FakeTarget(_caps(concurrent=False))
    captured = {}
    ls = types.SimpleNamespace(
        rows={"hr.dept": 5, "hr.emp": 3}, warnings=["a warning"],
        chunks_total=5, chunks_loaded=3)
    monkeypatch.setattr(loader_mod, "ResumableLoader",
                        _fake_loader_factory(captured, ls))

    stats = migrate(src, tgt, schema="hr", cfg=object(),
                    manifest_path="/tmp/m.db", run_id="run7",
                    parallelism=4, batch_size=321, drop_existing=False)

    kw = captured["kw"]
    assert kw["parallelism"] == 4
    assert kw["batch_size"] == 321
    assert kw["use_copy"] is True
    assert kw["preserve_case"] is False
    assert kw["fresh"] is False                       # fresh == drop_existing
    assert kw["concurrent_writes"] is False           # threaded from caps
    # rows re-keyed from fqn back to the source table name
    assert stats.rows == {"dept": 5, "emp": 3}
    # partial-load exit state: 5 planned, 3 loaded -> 2 failed chunks
    assert stats.failed_chunks == 2
    assert "a warning" in stats.warnings
    # FKs dropped BEFORE the chunked load (so per-chunk range deletes are FK-safe)
    assert any("DROP CONSTRAINT IF EXISTS" in s for s in tgt.executed)


def test_resumable_path_clean_load_has_zero_failed_chunks(monkeypatch):
    from any2heliosdb.core import loader as loader_mod
    src = FakeSource(_schema())
    tgt = FakeTarget(_caps())
    captured = {}
    ls = types.SimpleNamespace(rows={"hr.dept": 1, "hr.emp": 1}, warnings=[],
                               chunks_total=4, chunks_loaded=4)
    monkeypatch.setattr(loader_mod, "ResumableLoader",
                        _fake_loader_factory(captured, ls))
    stats = migrate(src, tgt, schema="hr", cfg=object(),
                    manifest_path="/tmp/m.db", run_id="r")
    assert stats.failed_chunks == 0
    assert captured["kw"]["fresh"] is True            # default drop_existing=True


def test_resumable_path_threads_non_default_preserve_case_and_use_copy(monkeypatch):
    # Non-default knob values must thread through (the defaults-only assertions
    # above cannot catch an accidentally hardcoded kwarg): preserve_case=True,
    # and a no-COPY capability so use_copy resolves False (prefer_copy True AND
    # copy_from_stdin False).
    from any2heliosdb.core import loader as loader_mod
    src = FakeSource(_schema())
    tgt = FakeTarget(_caps(copy=False))               # copy_from_stdin False
    captured = {}
    ls = types.SimpleNamespace(rows={}, warnings=[], chunks_total=0, chunks_loaded=0)
    monkeypatch.setattr(loader_mod, "ResumableLoader",
                        _fake_loader_factory(captured, ls))
    stats = migrate(src, tgt, schema="hr", cfg=object(), manifest_path="/tmp/m.db",
                    run_id="r", preserve_case=True)
    assert captured["kw"]["preserve_case"] is True    # non-default value threaded
    assert captured["kw"]["use_copy"] is False        # derived from capability
    assert stats.load_mode == "insert"


# --- native-Oracle sequential fallback vs resumable default ------------------
def test_native_oracle_uses_inline_insert_not_resumable_loader(monkeypatch):
    from any2heliosdb.core import loader as loader_mod

    # Guard: constructing the resumable loader on the Oracle path is a bug.
    def _boom(*a, **k):
        raise AssertionError("native Oracle path must NOT build ResumableLoader")

    monkeypatch.setattr(loader_mod, "ResumableLoader", _boom)

    src = FakeSource(_schema(), {"dept": [(1, "x")], "emp": [(2, 1)]})
    tgt = OracleFakeTarget(_caps(edition=Edition.LITE, copy=False))
    stats = migrate(src, tgt, schema="hr", cfg=object(),
                    manifest_path="/tmp/m.db", run_id="r")
    # inline INSERT load (no COPY on the Oracle wire), keyed by source name
    assert stats.load_mode == "insert"
    assert stats.rows == {"dept": 1, "emp": 1}
    assert [t for (t, _c, _n) in tgt.inserts] == ["dept", "emp"]
    # Oracle keeps the source (upper) case for the loaded table names.
    # (dept/emp are already lowercase here; the point is the inline path ran.)
    assert tgt.copies == []


def test_native_oracle_keeps_source_case_on_inline_load(monkeypatch):
    from any2heliosdb.core import loader as loader_mod
    monkeypatch.setattr(loader_mod, "ResumableLoader",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no loader")))
    t = Table(name="EMP", schema="HR",
              columns=[Column("ID", DataType.decimal(10, 0), nullable=False)],
              primary_key=PrimaryKey(columns=["ID"]))
    src = FakeSource(Schema("HR", tables=[t]), {"EMP": [(1,)]})
    tgt = OracleFakeTarget(_caps(edition=Edition.LITE, copy=False))
    migrate(src, tgt, schema="HR", cfg=object(), manifest_path="/tmp/m.db", run_id="r")
    # keep_source_case = is_oracle -> target table name stays upper-case.
    assert tgt.inserts[0][0] == "EMP"


def test_migrate_probes_when_edition_unknown():
    src = FakeSource(_schema(), {"dept": [], "emp": []})
    tgt = FakeTarget(_caps(edition=Edition.UNKNOWN))
    migrate(src, tgt, schema="hr")
    assert tgt.probe_called is True


@pytest.mark.parametrize("bs", [100, 4096])
def test_batch_size_reaches_stream_rows_on_sequential_path(bs):
    src = FakeSource(_schema(), {"dept": [(1, "x")], "emp": []})
    tgt = FakeTarget(_caps())
    migrate(src, tgt, schema="hr", batch_size=bs)
    assert src.stream_arraysizes and all(a == bs for a in src.stream_arraysizes)
