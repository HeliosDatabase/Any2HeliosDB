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
