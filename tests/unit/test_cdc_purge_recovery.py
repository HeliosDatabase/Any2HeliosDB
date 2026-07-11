"""B1 (H5) — a crash mid-purge must RE-APPLY, never silently skip records.

The pre-fix ``purge_applied`` removed the closed segment files first and wrote the
``purged_lines`` bookkeeping once at the very end. A crash in between deleted the
files without bumping the count, so the surviving segments' global line indices
shifted DOWN — un-applied records fell below the durable apply cursor and were
skipped forever.

The fix inverts the ordering (persist meta BEFORE removing each file) so any crash
window shifts indices UP into a safe replay window instead, and records the
last-purged segment number so a counted-but-surviving file is reconciled away
deterministically on the next purge.
"""
from __future__ import annotations

import pytest

import any2heliosdb.cdc.trail as trailmod
from any2heliosdb.cdc.trail import Trail
from any2heliosdb.core.change_record import INSERT, ChangeRecord


def _rec(idv):
    return ChangeRecord(op=INSERT, schema="s", table="t", key={"id": idv},
                        after={"id": idv, "v": "x"})


def _six_single_record_segments(tmp_path):
    """A trail of six single-record segments (records 1..6 at global lines 1..6)."""
    t = Trail(str(tmp_path))
    t.rotate_bytes = 50  # each ~80-byte record forces its own segment
    for i in range(1, 7):
        t.append([_rec(i)])
    # 6 records, each its own segment (5 closed + 1 active).
    assert len(t.segment_paths()) >= 4
    assert t.line_count() == 6
    return t


def test_purge_crash_before_meta_never_drops_records(tmp_path, monkeypatch):
    # Reviewer's exact repro: 6 segments, apply cursor = 2, a crash right at the
    # durable meta write. The pre-fix code has already unlinked the fully-applied
    # closed segments, so reading from cursor 2 drops records 3 and 4. The fix
    # writes the meta BEFORE any unlink, so the failed write leaves the trail
    # untouched and nothing is dropped.
    t = _six_single_record_segments(tmp_path)
    cursor = 2

    real_replace = trailmod.os.replace

    def boom(*a, **k):
        raise OSError("crash before the purge bookkeeping durably lands")

    monkeypatch.setattr(trailmod.os, "replace", boom)
    with pytest.raises(OSError):
        t.purge_applied(cursor)
    monkeypatch.setattr(trailmod.os, "replace", real_replace)

    # Recovery: reading from the durable cursor must still yield records 3..6.
    # (Pre-fix this returns [5, 6] — records 3 and 4 were silently skipped.)
    out, _ = Trail(str(tmp_path)).read(cursor)
    ids = [r.key["id"] for r in out]
    assert 3 in ids and 4 in ids, ids
    assert ids == [3, 4, 5, 6]


def test_purge_crash_after_meta_reapplies_then_reconciles(tmp_path, monkeypatch):
    # A crash AFTER the meta write but BEFORE the file removal leaves a
    # counted-but-present segment. The reader double-counts it, shifting indices UP
    # into a safe replay window (never a skip), and the NEXT purge reconciles the
    # leftover away deterministically.
    t = _six_single_record_segments(tmp_path)  # records 1..6 at global lines 1..6
    cursor = 2  # records 3..6 are NOT yet applied

    real_remove = trailmod.os.remove
    state = {"n": 0}

    def flaky_remove(path, *a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("crash after meta write, before unlink")
        return real_remove(path, *a, **k)

    monkeypatch.setattr(trailmod.os, "remove", flaky_remove)
    with pytest.raises(OSError):
        t.purge_applied(cursor)
    monkeypatch.setattr(trailmod.os, "remove", real_remove)

    # The first closed segment's file survived but its line is already counted, so
    # the reader double-counts it — indices shift UP. Reading from the durable
    # cursor must therefore RE-APPLY (never skip): every un-applied record (3..6)
    # is still returned.
    out, _ = Trail(str(tmp_path)).read(cursor)
    ids = [r.key["id"] for r in out]
    for rid in (3, 4, 5, 6):
        assert rid in ids, (rid, ids)

    # A follow-up purge reconciles the leftover away deterministically, and the
    # global total collapses back to the real 6 with the cursor still valid.
    t2 = Trail(str(tmp_path))
    t2.purge_applied(cursor)
    assert t2.line_count() == 6
    out2, cur2 = t2.read(cursor)
    assert [r.key["id"] for r in out2] == [3, 4, 5, 6] and cur2 == 6


# --- B1 residual: leftover reconcile must never deflate a persisted cursor -----


def test_reconcile_under_lock_before_read_prevents_deflation_skip(tmp_path, monkeypatch):
    # The reviewer's 3-step residual: (1) purge crash leaves a counted-but-
    # surviving segment (indices inflated); (2) a replicat run must NOT read the
    # inflated index space — it reconciles under its exclusive lock FIRST (what
    # engine.run_replicat now does); (3) a later purge/reconcile then has nothing
    # left to shift, so no cursor can ever be deflated past unapplied records.
    t = _six_single_record_segments(tmp_path)

    # Simulate the crash: segment 1 counted into purged_lines but file survives.
    seg1 = t._seg_path(t._segment_indices()[0])
    with open(seg1, encoding="utf-8") as f:
        n_lines = sum(1 for _ in f)
    t._write_purged_meta(n_lines, t._segment_indices()[0])

    # What run_replicat does now: lock, reconcile, THEN read. The leftover is
    # removed before any index is observed, so read(cursor) sees the same
    # records a pre-crash reader saw — nothing skipped, nothing double-counted.
    t2 = Trail(str(tmp_path))
    with t2.exclusive("test replicat"):
        t2.reconcile_purged()
        records, next_cursor = t2.read(n_lines)  # cursor == purged prefix
    ids = [r.key["id"] for r in records]
    assert ids == [2, 3, 4, 5, 6]                # every unapplied record present
    # A second reconcile is a no-op (deterministic recovery, no further shift).
    t3 = Trail(str(tmp_path))
    with t3.exclusive("test replicat 2"):
        assert t3.reconcile_purged() == []
        records2, _ = t3.read(n_lines)
    assert [r.key["id"] for r in records2] == ids


def test_exclusive_lock_conflict_fails_fast(tmp_path):
    t1 = Trail(str(tmp_path))
    t1.append([_rec(1)])
    t2 = Trail(str(tmp_path))
    from any2heliosdb.errors import Any2HeliosError
    with t1.exclusive("a replicat run"):
        with pytest.raises(Any2HeliosError, match="locked by another process"):
            with t2.exclusive("--purge-applied"):
                pass
    # Released: reacquiring works.
    with t2.exclusive("--purge-applied"):
        pass
