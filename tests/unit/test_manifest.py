"""Unit tests for the resume manifest (no DB server needed)."""
import os

from any2heliosdb.core import manifest as M
from any2heliosdb.core.manifest import Manifest


def _mk(tmp_path):
    return Manifest(os.path.join(str(tmp_path), "run1", "manifest.db"))


def test_chunk_state_machine_and_resume(tmp_path):
    man = _mk(tmp_path)
    rid = "run1"
    man.start_run(rid)
    man.add_table(rid, "HR.EMPLOYEES", "employees", total_rows_est=5)
    for i in range(3):
        man.add_chunk(rid, "HR.EMPLOYEES", "HR.EMPLOYEES:{}".format(i))

    # One completed, one mid-flight (simulating a crash), one untouched.
    man.set_chunk_state(rid, "HR.EMPLOYEES", "HR.EMPLOYEES:0", M.LOADED, rows_loaded=2)
    man.set_chunk_state(rid, "HR.EMPLOYEES", "HR.EMPLOYEES:1", M.IN_PROGRESS)

    assert man.is_chunk_done(rid, "HR.EMPLOYEES", "HR.EMPLOYEES:0") is True
    assert man.is_chunk_done(rid, "HR.EMPLOYEES", "HR.EMPLOYEES:1") is False

    # Recovery resets the in_progress chunk; completed work is never redone.
    reset = man.recover(rid)
    assert reset == 1
    pending = {c.chunk_id for c in man.pending_chunks(rid)}
    assert pending == {"HR.EMPLOYEES:1", "HR.EMPLOYEES:2"}

    summary = man.summary(rid)
    assert summary["rows_loaded"] == 2
    man.close()


def test_watermark_roundtrip(tmp_path):
    man = _mk(tmp_path)
    man.start_run("run1")
    man.add_table("run1", "HR.EMPLOYEES", "employees")
    man.set_watermark("run1", "HR.EMPLOYEES", "scn", "123456")
    assert man.get_watermark("run1", "HR.EMPLOYEES") == ("scn", "123456")
    man.close()


def test_get_chunks_returns_recorded_plan_all_states(tmp_path):
    man = _mk(tmp_path)
    rid = "run1"
    man.start_run(rid)
    man.add_table(rid, "HR.EMP", "emp")
    man.add_chunk(rid, "HR.EMP", "EMP:0", predicate='"ID" >= 1 AND "ID" < 26',
                  lo="1", hi="26")
    man.add_chunk(rid, "HR.EMP", "EMP:1", predicate='"ID" >= 26 AND "ID" < 51',
                  lo="26", hi="51")
    man.add_table(rid, "HR.LOG", "log")
    man.add_chunk(rid, "HR.LOG", "LOG:0")  # whole-table chunk (NULL predicate/bounds)
    # LOADED state must NOT affect what get_chunks returns — the recorded PLAN is
    # replayed regardless of load progress.
    man.set_chunk_state(rid, "HR.EMP", "EMP:0", M.LOADED, rows_loaded=25)

    recorded = man.get_chunks(rid)
    # deterministic order: table_fqn, chunk_id
    assert [(r.table_fqn, r.chunk_id) for r in recorded] == [
        ("HR.EMP", "EMP:0"), ("HR.EMP", "EMP:1"), ("HR.LOG", "LOG:0")]
    emp0 = recorded[0]
    assert emp0.predicate == '"ID" >= 1 AND "ID" < 26'
    assert (emp0.bounds_lo, emp0.bounds_hi) == ("1", "26")
    log0 = recorded[2]
    assert log0.predicate is None and log0.bounds_lo is None and log0.bounds_hi is None
    # per-table filter
    assert [r.chunk_id for r in man.get_chunks(rid, "HR.EMP")] == ["EMP:0", "EMP:1"]
    man.close()


def test_reset_run_clears_loaded_chunks_for_drop_existing_reload(tmp_path):
    man = _mk(tmp_path)
    rid = "run1"
    man.start_run(rid)
    man.add_table(rid, "HR.EMP", "emp", total_rows_est=2)
    man.add_chunk(rid, "HR.EMP", "HR.EMP:0")
    man.set_chunk_state(rid, "HR.EMP", "HR.EMP:0", M.LOADED, rows_loaded=2)
    assert man.is_chunk_done(rid, "HR.EMP", "HR.EMP:0")
    # A drop_existing re-migrate recreates the table empty, so a surviving LOADED
    # chunk would be wrongly skipped (the silent-data-loss bug). reset_run clears
    # it; re-planning re-adds the chunk as pending so the emptied table reloads.
    man.reset_run(rid)
    assert not man.is_chunk_done(rid, "HR.EMP", "HR.EMP:0")
    man.add_chunk(rid, "HR.EMP", "HR.EMP:0")
    assert not man.is_chunk_done(rid, "HR.EMP", "HR.EMP:0")
    man.close()
