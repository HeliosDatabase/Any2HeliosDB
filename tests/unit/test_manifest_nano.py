"""Unit tests for the embedded-Nano manifest backend.

Skipped unless the optional `heliosdb-nano-embedded` wheel is installed (the
`any2heliosdb[nano-manifest]` extra), so the default suite stays green without
it. Validates parity with the sqlite backend across the loader's full call
sequence, thread-safety of the shared writer, and the live-monitor read path."""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from any2heliosdb.core import manifest as M
from any2heliosdb.core.manifest import Manifest

pytest.importorskip("heliosdb_nano", reason="needs any2heliosdb[nano-manifest]")

RID = "run1"
T = "HR.EMPLOYEES"


def _nano(tmp_path):
    return Manifest(os.path.join(str(tmp_path), "nano_manifest"), backend=M.NANO)


def test_path_helper_and_detect(tmp_path):
    d = str(tmp_path)
    assert M.manifest_path_for(d, M.SQLITE).endswith("manifest.db")
    assert M.manifest_path_for(d, M.NANO).endswith("manifest.nano")
    # round-trip: a freshly created nano manifest is detected as a directory
    man = _nano(tmp_path)
    man.start_run(RID)
    man.close()
    assert M.detect_backend(os.path.join(d, "nano_manifest")) == M.NANO


def test_full_lifecycle_matches_sqlite(tmp_path):
    """The exact loader call sequence must behave identically on both backends."""
    def run(man):
        man.start_run(RID, config_hash="h1")
        assert not man.snapshot_decided(RID)
        man.set_snapshot(RID, "SCN-1")
        assert man.snapshot_decided(RID) and man.get_snapshot(RID) == "SCN-1"
        man.add_table(RID, T, "employees", total_rows_est=300)
        for i in range(4):
            man.add_chunk(RID, T, "c{}".format(i))
        # total_chunks maintained (count-then-set, no scalar-subquery params)
        snap = man.progress_snapshot(RID)
        tbl = next(t for t in snap["tables"] if t["table_fqn"] == T)
        assert tbl["chunks_total"] == 4
        # idempotent re-plan
        man.add_table(RID, T, "employees", total_rows_est=300)
        man.add_chunk(RID, T, "c0")
        assert {c.chunk_id for c in man.pending_chunks(RID)} == {"c0", "c1", "c2", "c3"}
        # load + crash-recover
        man.set_chunk_state(RID, T, "c0", M.LOADED, rows_loaded=75)
        man.set_chunk_state(RID, T, "c1", M.LOADED, rows_loaded=75)
        man.set_chunk_state(RID, T, "c2", M.IN_PROGRESS)
        assert man.recover(RID) == 1
        assert {c.chunk_id for c in man.pending_chunks(RID)} == {"c2", "c3"}
        assert man.rows_by_table(RID).get(T) == 150
        man.set_chunk_state(RID, T, "c2", M.LOADED, rows_loaded=75)
        man.set_chunk_state(RID, T, "c3", M.LOADED, rows_loaded=75)
        return man.summary(RID), man.progress_snapshot(RID)["complete"]

    nano = _nano(tmp_path)
    try:
        nano_summary, nano_complete = run(nano)
    finally:
        nano.close()
    sqlite = Manifest(os.path.join(str(tmp_path), "manifest.db"))
    try:
        sqlite_summary, sqlite_complete = run(sqlite)
    finally:
        sqlite.close()

    assert nano_summary == sqlite_summary
    assert nano_complete is sqlite_complete is True
    assert nano_summary["rows_loaded"] == 300


def test_reset_run_clears_chunks_bug_f(tmp_path):
    """reset_run's `DELETE ... WHERE run_id=?` is a partial prefix of the composite
    PK (the embedded-Nano BUG F path). It must actually remove the chunks."""
    man = _nano(tmp_path)
    try:
        man.start_run(RID)
        man.add_table(RID, T, "employees")
        for i in range(3):
            man.add_chunk(RID, T, "c{}".format(i))
            man.set_chunk_state(RID, T, "c{}".format(i), M.LOADED, rows_loaded=10)
        man.reset_run(RID)
        assert man.pending_chunks(RID) == []
        assert man.rows_by_table(RID).get(T, 0) == 0
        # and the table row was reset to pending / 0 chunks
        tbl = next(t for t in man.progress_snapshot(RID)["tables"] if t["table_fqn"] == T)
        assert tbl["chunks_total"] == 0
    finally:
        man.close()


def test_concurrent_per_chunk_writers(tmp_path):
    """Mirror loader._load_chunk: many threads each open their OWN Manifest(nano)
    handle per chunk; they must share the single RocksDB writer with no lock
    conflict and converge to the right state."""
    path = os.path.join(str(tmp_path), "nano_manifest")
    plan = Manifest(path, backend=M.NANO)
    try:
        plan.start_run(RID)
        for t in ("S.A", "S.B"):
            plan.add_table(RID, t, t.lower())
            for i in range(8):
                plan.add_chunk(RID, t, "{}-c{}".format(t, i))
    finally:
        plan.close()

    work = [(t, "{}-c{}".format(t, i)) for t in ("S.A", "S.B") for i in range(8)]
    errors = []

    def load(item):
        tbl, cid = item
        m = Manifest(path, backend=M.NANO)  # fresh per-chunk handle, like the loader
        try:
            m.set_chunk_state(RID, tbl, cid, M.IN_PROGRESS, bump_attempt=True)
            m.set_chunk_state(RID, tbl, cid, M.LOADED, rows_loaded=5)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))
        finally:
            m.close()

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(load, work))

    assert not errors, errors[:3]
    chk = Manifest(path, backend=M.NANO)
    try:
        summ = chk.summary(RID)
        assert summ["chunk_states"].get(M.LOADED) == len(work)
        assert summ["rows_loaded"] == len(work) * 5
    finally:
        chk.close()


def test_get_chunks_replay_reader_matches_sqlite(tmp_path):
    """The resume chunk-plan reader must behave identically on both backends — it's
    the shared read API a resume replays, so a nano-manifest resume can't silently
    diverge from a sqlite one."""
    def run(man):
        man.start_run(RID)
        man.add_table(RID, T, "employees")
        man.add_chunk(RID, T, "c0", predicate='"ID" >= 1 AND "ID" < 51', lo="1", hi="51")
        man.add_chunk(RID, T, "c1", predicate='"ID" >= 51 AND "ID" < 101', lo="51", hi="101")
        man.add_table(RID, "S.LOG", "log")
        man.add_chunk(RID, "S.LOG", "log0")  # whole-table chunk: NULL predicate/bounds
        man.set_chunk_state(RID, T, "c0", M.LOADED, rows_loaded=50)  # state irrelevant to replay
        return [(r.table_fqn, r.chunk_id, r.predicate, r.bounds_lo, r.bounds_hi)
                for r in man.get_chunks(RID)]

    nano = _nano(tmp_path)
    try:
        nano_rows = run(nano)
    finally:
        nano.close()
    sqlite = Manifest(os.path.join(str(tmp_path), "manifest.db"))
    try:
        sqlite_rows = run(sqlite)
    finally:
        sqlite.close()

    assert nano_rows == sqlite_rows
    assert nano_rows[0] == (T, "c0", '"ID" >= 1 AND "ID" < 51', "1", "51")
    assert nano_rows[2] == ("S.LOG", "log0", None, None, None)


def test_open_readonly_sees_flushed_writes_while_writer_open(tmp_path):
    """The live-monitor case: a fresh read-only open must see the writer's
    flushed progress even while the writer handle stays open."""
    path = os.path.join(str(tmp_path), "nano_manifest")
    w = Manifest(path, backend=M.NANO)
    try:
        w.start_run(RID)
        w.add_table(RID, T, "employees", total_rows_est=50)
        w.add_chunk(RID, T, "c0")
        w.set_chunk_state(RID, T, "c0", M.LOADED, rows_loaded=25)

        ro = Manifest.open_readonly(path)        # auto-detects nano
        try:
            assert ro.progress_snapshot(RID)["chunks_loaded"] == 1
        finally:
            ro.close()

        w.add_chunk(RID, T, "c1")
        w.set_chunk_state(RID, T, "c1", M.LOADED, rows_loaded=25)
        ro2 = Manifest.open_readonly(path)       # fresh reopen sees the new write
        try:
            snap = ro2.progress_snapshot(RID)
            assert snap["chunks_loaded"] == 2
            assert snap["rows_loaded"] == 50
        finally:
            ro2.close()
    finally:
        w.close()
