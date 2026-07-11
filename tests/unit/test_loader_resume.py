"""Resume must replay the RECORDED chunk plan, never recompute from live bounds.

Regression for the silent PK-range skip: ``ResumableLoader.plan()`` used to call
``compute_chunks`` on every plan — including resume — so a source whose PK bounds
drifted (rows inserted/deleted at the key edges) between the original run and the
resume produced a NEW in-memory plan while the manifest kept the OLD predicates +
LOADED states. The two diverged and PK ranges fell through the gap. These tests
pin the fix: on resume the ledger is the single source of truth for the plan.

Hermetic — a fake source adapter (no DB server) is injected by monkeypatching the
config store's ``build_source_adapter``, exactly the seam ``plan()`` imports."""
import re
import types

import pytest

from any2heliosdb.core import manifest as M
from any2heliosdb.core.catalog_model import (Column, DataType, PrimaryKey, Schema,
                                             Table)
from any2heliosdb.core.loader import ResumableLoader, ResumeDriftError
from any2heliosdb.core.manifest import Manifest


class FakeSource:
    """Minimal source adapter for ``plan()``: reports per-table integer PK bounds
    and records every ``numeric_pk_bounds`` call so a test can prove a resume did
    NOT re-probe (recompute) a table it should have replayed from the ledger."""

    def __init__(self, bounds_by_fqn, row_count=100):
        self.bounds_by_fqn = bounds_by_fqn  # fqn -> (lo, hi) | None
        self.row_count = row_count
        self.numeric_calls = []

    def connect(self):
        pass

    def close(self):
        pass

    def capture_snapshot(self):
        return None

    def use_snapshot(self, token):
        pass

    def exact_row_count(self, table):
        return self.row_count

    def numeric_pk_bounds(self, table, col):
        self.numeric_calls.append(table.fqn)
        return self.bounds_by_fqn.get(table.fqn)


def _cfg(tmp_path, backend="sqlite"):
    options = types.SimpleNamespace(
        preserve_case=False, manifest_backend=backend,
        output_dir=str(tmp_path), parallelism=2)
    source = types.SimpleNamespace(
        dialect="oracle", host="h", port=1521, database="db", schema="HR", user="hr")
    target = types.SimpleNamespace(
        driver="native", host="th", port=1521, dbname="tdb", user="tu")
    return types.SimpleNamespace(source=source, target=target, options=options)


def _table(name):
    return Table(
        name=name, schema="HR",
        columns=[Column("ID", DataType.decimal(10, 0), nullable=False),
                 Column("NAME", DataType.varchar(50))],
        primary_key=PrimaryKey(columns=["ID"]))


def _inject(monkeypatch, source):
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: source)


def _ranges(loader, prefix):
    return sorted((ch.lo, ch.hi) for cid, ch in loader._chunks.items()
                  if cid.startswith(prefix))


def _hi_from_predicate(pred):
    return int(re.search(r"< (\d+)", pred).group(1))


@pytest.mark.parametrize("resume_bounds", [(1, 200), (50, 100), (1, 50)])
def test_resume_replays_recorded_predicates_not_drifted_bounds(tmp_path, monkeypatch, resume_bounds):
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    rid = "run1"

    # Initial plan against bounds [1,100] -> 4 contiguous chunks (step 25).
    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 100)}))
    loader1 = ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                              parallelism=2)
    loader1.plan()
    recorded = {cid: ch.source_where() for cid, ch in loader1._chunks.items()}
    assert len(recorded) == 4
    assert set(recorded) == {"EMP:0", "EMP:1", "EMP:2", "EMP:3"}

    # First two chunks complete, then a "crash".
    man = Manifest(manifest_path)
    man.set_chunk_state(rid, "HR.EMP", "EMP:0", M.LOADED, rows_loaded=25)
    man.set_chunk_state(rid, "HR.EMP", "EMP:1", M.LOADED, rows_loaded=25)
    man.close()

    # Resume with DRIFTED source bounds. A recompute would repartition [1,resume_hi]
    # and diverge from the recorded LOADED bookkeeping; replay must ignore the live
    # bounds entirely.
    src2 = FakeSource({"HR.EMP": resume_bounds})
    _inject(monkeypatch, src2)
    loader2 = ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                              parallelism=2)
    loader2.plan()

    replayed = {cid: ch.source_where() for cid, ch in loader2._chunks.items()}
    # 1. predicates replayed byte-identical to what was recorded
    assert replayed == recorded
    # 2. a recorded table is NEVER re-probed for live bounds on resume
    assert src2.numeric_calls == []
    # 3. no replayed predicate carries a recomputed upper bound (all from the
    #    original [1,100] plan: 26/51/76/101 — never the drifted 200/50/...)
    assert {_hi_from_predicate(p) for p in replayed.values()} == {26, 51, 76, 101}
    # 4. every original range still covered exactly once, contiguous [1,101)
    ranges = _ranges(loader2, "EMP:")
    assert ranges[0][0] == 1 and ranges[-1][1] == 101
    for i in range(len(ranges) - 1):
        assert ranges[i][1] == ranges[i + 1][0]
    # 5. total_chunks bookkeeping unchanged by replay (still the recorded 4)
    ro = Manifest.open_readonly(manifest_path)
    emp = next(t for t in ro.progress_snapshot(rid)["tables"] if t["table_fqn"] == "HR.EMP")
    ro.close()
    assert emp["chunks_total"] == 4 == len(ranges)


def test_resume_plans_new_source_table_fresh_alongside_replay(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    rid = "run1"

    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 100)}))
    ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                    parallelism=2).plan()

    # DEPT appears in the source on resume; EMP bounds also drifted.
    src2 = FakeSource({"HR.EMP": (1, 300), "HR.DEPT": (1, 10)})
    _inject(monkeypatch, src2)
    loader2 = ResumableLoader(cfg, Schema("HR", tables=[_table("EMP"), _table("DEPT")]),
                              manifest_path, rid, parallelism=2)
    loader2.plan()

    # EMP replayed from the ledger (never re-probed); DEPT planned fresh (probed once).
    assert src2.numeric_calls == ["HR.DEPT"]
    assert {cid for cid in loader2._chunks if cid.startswith("DEPT:")}
    emp_ranges = _ranges(loader2, "EMP:")
    assert emp_ranges[-1][1] == 101  # still the recorded [1,100] plan, not [1,300]

    # DEPT is now recorded in the manifest so a later resume replays it too.
    ro = Manifest.open_readonly(manifest_path)
    assert ro.get_chunks(rid, "HR.DEPT")
    ro.close()


def test_resume_fails_closed_when_recorded_table_vanished(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    rid = "run1"

    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 100), "HR.DEPT": (1, 10)}))
    ResumableLoader(cfg, Schema("HR", tables=[_table("EMP"), _table("DEPT")]),
                    manifest_path, rid, parallelism=2).plan()

    # DEPT has vanished from the source on resume -> fail closed, don't drop it.
    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 200)}))
    loader2 = ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]),
                              manifest_path, rid, parallelism=2)
    with pytest.raises(ResumeDriftError) as ei:
        loader2.plan()
    assert "HR.DEPT" in str(ei.value)


def test_fresh_reset_recomputes_from_live_bounds(tmp_path, monkeypatch):
    """The reset path (fresh=True) is UNCHANGED: it clears the ledger and re-plans
    from the CURRENT source, so drifted bounds are reflected (contrast: resume)."""
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    rid = "run1"

    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 100)}))
    ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                    parallelism=2).plan()

    # A drop_existing re-migrate (fresh=True) must repartition against live bounds.
    src2 = FakeSource({"HR.EMP": (1, 200)})
    _inject(monkeypatch, src2)
    loader2 = ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                              parallelism=2, fresh=True)
    loader2.plan()
    assert src2.numeric_calls == ["HR.EMP"]  # recomputed
    assert _ranges(loader2, "EMP:")[-1][1] == 201  # [1,200] plan, step 50


def test_resume_replays_whole_table_chunk_for_pkless_table(tmp_path, monkeypatch):
    """A table with no single-column PK is a single whole-table chunk (NULL
    predicate/bounds). Replay must rebuild it as a whole-table chunk, not choke on
    the NULLs."""
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    rid = "run1"

    def pkless():
        return Table(name="LOG", schema="HR",
                     columns=[Column("MSG", DataType.varchar(50))])

    _inject(monkeypatch, FakeSource({}))  # no bounds -> whole-table chunk
    ResumableLoader(cfg, Schema("HR", tables=[pkless()]), manifest_path, rid,
                    parallelism=2).plan()

    src2 = FakeSource({})
    _inject(monkeypatch, src2)
    loader2 = ResumableLoader(cfg, Schema("HR", tables=[pkless()]), manifest_path, rid,
                              parallelism=2)
    loader2.plan()
    assert set(loader2._chunks) == {"LOG:0"}
    ch = loader2._chunks["LOG:0"]
    assert ch.source_where() is None and ch.target_where() is None
    assert src2.numeric_calls == []


# --- plan-completeness marker: partial plans are never replayed ----------------


def _crashing_add_chunk(monkeypatch, crash_after):
    """Wrap Manifest.add_chunk to raise after *crash_after* successful calls,
    simulating a mid-planning crash (add_chunk commits per row)."""
    real = Manifest.add_chunk
    calls = {"n": 0}

    def wrapper(self, *a, **k):
        if calls["n"] >= crash_after:
            raise RuntimeError("simulated crash mid-planning")
        calls["n"] += 1
        return real(self, *a, **k)

    monkeypatch.setattr(Manifest, "add_chunk", wrapper)
    return calls


def test_partial_plan_crash_replans_fresh_on_resume(tmp_path, monkeypatch):
    # A crash between the first and last add_chunk leaves a syntactically-valid
    # but PARTIAL plan. Replaying it as complete would silently skip the missing
    # tail ranges, so the next run must detect the unset plan-complete marker and
    # re-plan FROM THE LIVE SOURCE instead.
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    rid = "run-crash"

    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 100)}))
    loader1 = ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                              parallelism=2)
    _crashing_add_chunk(monkeypatch, crash_after=2)
    with pytest.raises(RuntimeError, match="simulated crash"):
        loader1.plan()

    man = Manifest(manifest_path)
    assert man.plan_complete(rid) is False            # marker never set
    assert len(man.get_chunks(rid)) == 2              # partial rows persisted
    man.close()

    # Recover the real add_chunk and "restart" with drifted bounds: the run must
    # NOT replay the 2-row partial plan — it re-plans fresh from the live source.
    monkeypatch.undo()
    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 200)}))
    loader2 = ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                              parallelism=2)
    loader2.plan()
    ranges = _ranges(loader2, "EMP:")
    assert ranges[0][0] == 1 and ranges[-1][1] == 201  # full LIVE range covered
    for (_, b), (c, _) in zip(ranges, ranges[1:]):
        assert b == c                                  # contiguous, no gaps
    man = Manifest(manifest_path)
    assert man.plan_complete(rid) is True              # marker set at plan end
    assert len(man.get_chunks(rid)) == len(ranges)     # partial rows replaced
    man.close()


def test_completed_plan_still_replays_after_marker(tmp_path, monkeypatch):
    # Sanity: with the marker set, resume replays (does not re-probe the source).
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    rid = "run-marker"
    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 100)}))
    ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                    parallelism=2).plan()

    drifted = FakeSource({"HR.EMP": (1, 500)})
    _inject(monkeypatch, drifted)
    loader2 = ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                              parallelism=2)
    loader2.plan()
    assert drifted.numeric_calls == []                 # replay: no re-probe
    assert _ranges(loader2, "EMP:")[-1][1] == 101      # recorded plan, not live


# --- changed (not vanished) PK column fails closed -----------------------------


def test_pk_column_change_fails_closed_on_resume(tmp_path, monkeypatch):
    # The recorded predicate reads "ID" verbatim, but the idempotent target
    # DELETE renders from the CURRENT single PK column. If the PK moved to a
    # different column mid-run, read and delete would cover different ranges —
    # fail closed instead.
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    rid = "run-pkmove"
    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 100)}))
    ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                    parallelism=2).plan()

    renamed = Table(
        name="EMP", schema="HR",
        columns=[Column("CODE", DataType.decimal(10, 0), nullable=False),
                 Column("NAME", DataType.varchar(50))],
        primary_key=PrimaryKey(columns=["CODE"]))
    loader2 = ResumableLoader(cfg, Schema("HR", tables=[renamed]), manifest_path, rid,
                              parallelism=2)
    with pytest.raises(ResumeDriftError, match="primary key changed"):
        loader2.plan()


def test_additive_plan_crash_salvages_without_losing_progress(tmp_path, monkeypatch):
    # Refuter counterexample: a crash while ADDITIVELY planning a table that
    # appeared on resume must not leave a partial plan replayable (the marker is
    # now cleared before every planning session), and the salvage must NOT throw
    # away the already-loaded progress of fully-planned tables.
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    rid = "run-additive"

    # Session 1: EMP planned completely, then partially loaded.
    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 100)}))
    ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                    parallelism=2).plan()
    man = Manifest(manifest_path)
    man.set_chunk_state(rid, "HR.EMP", "EMP:0", M.LOADED, rows_loaded=25)
    assert man.plan_complete(rid) is True
    man.close()

    # Session 2: DEPT appeared; crash after 2 of its add_chunk rows.
    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 100), "HR.DEPT": (1, 10)}))
    loader2 = ResumableLoader(cfg, Schema("HR", tables=[_table("EMP"), _table("DEPT")]),
                              manifest_path, rid, parallelism=2)
    _crashing_add_chunk(monkeypatch, crash_after=2)
    with pytest.raises(RuntimeError, match="simulated crash"):
        loader2.plan()
    man = Manifest(manifest_path)
    assert man.plan_complete(rid) is False             # marker cleared pre-write
    man.close()

    # Session 3: restart. EMP (has progress => provably fully planned) replays;
    # DEPT (no progress; possibly partial) is dropped and re-planned live.
    monkeypatch.undo()
    live = FakeSource({"HR.EMP": (1, 100), "HR.DEPT": (1, 10)})
    _inject(monkeypatch, live)
    loader3 = ResumableLoader(cfg, Schema("HR", tables=[_table("EMP"), _table("DEPT")]),
                              manifest_path, rid, parallelism=2)
    loader3.plan()
    assert "HR.DEPT" in live.numeric_calls            # DEPT re-planned from live
    assert "HR.EMP" not in live.numeric_calls         # EMP replayed, not re-probed
    dept_ranges = _ranges(loader3, "DEPT:")
    assert dept_ranges[0][0] == 1 and dept_ranges[-1][1] == 11  # full live range
    for (_, b), (c, _) in zip(dept_ranges, dept_ranges[1:]):
        assert b == c                                  # contiguous
    man = Manifest(manifest_path)
    assert man.plan_complete(rid) is True
    loaded = [c for c in man.get_chunks(rid, "HR.EMP")]
    assert len(loaded) == 4                            # EMP rows untouched
    # EMP:0's LOADED state survived the salvage (progress preserved).
    row = man._db.execute(
        "SELECT state FROM chunks WHERE run_id=? AND table_fqn=? AND chunk_id=?",
        (rid, "HR.EMP", "EMP:0")).fetchone()
    assert row[0] == M.LOADED
    man.close()


def test_failed_plan_leaves_no_partial_in_memory_plan(tmp_path, monkeypatch):
    # An exception-swallowing library caller must not be able to run() a partial
    # in-memory plan after plan() failed mid-write: _chunks is cleared on the
    # failure path, so run() re-plans (and the ledger marker salvages).
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    rid = "run-swallow"
    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 100)}))
    loader = ResumableLoader(cfg, Schema("HR", tables=[_table("EMP")]), manifest_path, rid,
                             parallelism=2)
    _crashing_add_chunk(monkeypatch, crash_after=2)
    with pytest.raises(RuntimeError, match="simulated crash"):
        loader.plan()
    assert loader._chunks == {} and loader._table_by_fqn == {}
    # Retry on the SAME object succeeds and yields the full plan.
    monkeypatch.undo()
    _inject(monkeypatch, FakeSource({"HR.EMP": (1, 100)}))
    loader.plan()
    assert _ranges(loader, "EMP:")[-1][1] == 101 and len(loader._chunks) == 4
