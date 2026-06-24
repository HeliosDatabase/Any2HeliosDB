"""Unit tests for the live migration monitor (no DB server, no TTY)."""
import os

from any2heliosdb.core import manifest as M
from any2heliosdb.core.manifest import Manifest
from any2heliosdb.monitor.live import render_snapshot, run_monitor


def _mk(tmp_path):
    return Manifest(os.path.join(str(tmp_path), "run1", "manifest.db"))


def _build(tmp_path):
    """A run with two tables: one mid-flight (mixed states incl. a failure),
    one entirely untouched (pending)."""
    man = _mk(tmp_path)
    rid = "run1"
    man.start_run(rid)

    # Table A: 4 chunks — 2 loaded (5+7 rows), 1 in_progress, 1 failed. est=20.
    man.add_table(rid, "HR.EMPLOYEES", "employees", total_rows_est=20)
    for i in range(4):
        man.add_chunk(rid, "HR.EMPLOYEES", "HR.EMPLOYEES:{}".format(i))
    man.set_chunk_state(rid, "HR.EMPLOYEES", "HR.EMPLOYEES:0", M.LOADED, rows_loaded=5)
    man.set_chunk_state(rid, "HR.EMPLOYEES", "HR.EMPLOYEES:1", M.LOADED, rows_loaded=7)
    man.set_chunk_state(rid, "HR.EMPLOYEES", "HR.EMPLOYEES:2", M.IN_PROGRESS)
    man.set_chunk_state(rid, "HR.EMPLOYEES", "HR.EMPLOYEES:3", M.FAILED, error="boom")

    # Table B: 2 chunks, both pending. No est.
    man.add_table(rid, "HR.DEPARTMENTS", "departments")
    for i in range(2):
        man.add_chunk(rid, "HR.DEPARTMENTS", "HR.DEPARTMENTS:{}".format(i))
    return man, rid


def test_progress_snapshot_per_table_and_totals(tmp_path):
    man, rid = _build(tmp_path)
    try:
        snap = man.progress_snapshot(rid)
    finally:
        man.close()

    assert snap["run_id"] == rid
    by_fqn = {t["table_fqn"]: t for t in snap["tables"]}
    assert set(by_fqn) == {"HR.EMPLOYEES", "HR.DEPARTMENTS"}

    emp = by_fqn["HR.EMPLOYEES"]
    assert emp["target_table"] == "employees"
    assert emp["chunks_total"] == 4
    assert emp["chunks_loaded"] == 2          # the 2 LOADED chunks
    assert emp["chunks_failed"] == 1
    assert emp["chunks_in_progress"] == 1
    assert emp["rows_loaded"] == 12           # 5 + 7
    assert emp["rows_est"] == 20
    # A table with a failed chunk surfaces as failed in the live status.
    assert emp["status"] == M.FAILED

    dep = by_fqn["HR.DEPARTMENTS"]
    assert dep["chunks_total"] == 2
    assert dep["chunks_loaded"] == 0
    assert dep["chunks_failed"] == 0
    assert dep["rows_loaded"] == 0
    assert dep["rows_est"] == 0
    assert dep["status"] == M.PENDING

    # Totals roll-up.
    assert snap["tables_total"] == 2
    assert snap["tables_done"] == 0           # neither table fully loaded
    assert snap["rows_loaded"] == 12
    assert snap["rows_est"] == 20
    assert snap["chunks_total"] == 6
    assert snap["chunks_loaded"] == 2
    assert snap["chunks_failed"] == 1
    assert snap["complete"] is False


def test_snapshot_complete_when_all_loaded(tmp_path):
    man = _mk(tmp_path)
    rid = "run1"
    try:
        man.start_run(rid)
        man.add_table(rid, "S.T", "t", total_rows_est=3)
        man.add_chunk(rid, "S.T", "S.T:0")
        man.set_chunk_state(rid, "S.T", "S.T:0", M.LOADED, rows_loaded=3)
        snap = man.progress_snapshot(rid)
    finally:
        man.close()
    assert snap["tables_done"] == 1
    assert snap["complete"] is True
    assert snap["tables"][0]["status"] == M.LOADED
    # est == loaded -> no volume left.
    assert max(0, snap["rows_est"] - snap["rows_loaded"]) == 0


def test_render_snapshot_returns_rich_renderable(tmp_path):
    man, rid = _build(tmp_path)
    try:
        snap = man.progress_snapshot(rid)
    finally:
        man.close()

    # Pure render must not raise and must yield something Rich can print.
    renderable = render_snapshot(snap, elapsed=12.0)
    assert renderable is not None
    assert hasattr(renderable, "renderables")  # rich.console.Group

    # And it must actually render to a string through a (non-TTY) Console.
    from rich.console import Console
    out = Console(width=120, record=True, force_terminal=False)
    out.print(renderable)
    text = out.export_text()
    assert "HR.EMPLOYEES" in text
    assert "employees" in text
    assert "totals" in text


def test_render_empty_snapshot_does_not_raise():
    empty = {
        "run_id": "run_empty", "tables": [], "tables_total": 0, "tables_done": 0,
        "rows_loaded": 0, "rows_est": 0, "chunks_total": 0, "chunks_loaded": 0,
        "chunks_failed": 0, "complete": False,
    }
    renderable = render_snapshot(empty)
    from rich.console import Console
    out = Console(width=100, record=True, force_terminal=False)
    out.print(renderable)
    assert "run_empty" in out.export_text()


def test_run_monitor_once_readonly(tmp_path):
    """--once path: read-only attach, single frame, exit code reflects done-ness."""
    man = _mk(tmp_path)
    rid = "run1"
    path = man.path
    try:
        man.start_run(rid)
        man.add_table(rid, "S.T", "t", total_rows_est=2)
        man.add_chunk(rid, "S.T", "S.T:0")
        # Not yet loaded -> run is incomplete -> exit code 1.
    finally:
        man.close()

    from rich.console import Console
    rec = Console(width=120, record=True, force_terminal=False)
    code = run_monitor(path, rid, once=True, console=rec)
    assert code == 1
    assert "S.T" in rec.export_text()

    # Now finish the chunk and re-run: complete -> exit 0.
    man2 = Manifest(path)
    try:
        man2.set_chunk_state(rid, "S.T", "S.T:0", M.LOADED, rows_loaded=2)
    finally:
        man2.close()
    rec2 = Console(width=120, record=True, force_terminal=False)
    code2 = run_monitor(path, rid, once=True, console=rec2)
    assert code2 == 0
    assert "COMPLETE" in rec2.export_text()


def test_open_readonly_missing_raises(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        Manifest.open_readonly(os.path.join(str(tmp_path), "nope", "manifest.db"))
