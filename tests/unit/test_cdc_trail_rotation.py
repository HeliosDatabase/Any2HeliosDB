"""H5 — trail rotation + retention.

The apply cursor stays a single GLOBAL line index across segments (no
``(segment, line)`` migration), so a legacy single-file trail and its integer
cursor keep working while rotation bounds file growth and ``purge_applied``
reclaims disk. These tests drive the mechanics with tiny data by forcing the
rotate threshold to a few hundred bytes (``rotate_bytes``), never writing MB.
"""
from __future__ import annotations

import os

import pytest

from any2heliosdb.cdc.trail import Trail
from any2heliosdb.core.change_record import INSERT, ChangeRecord
from any2heliosdb.errors import Any2HeliosError


def _rec(idv, pos=None):
    return ChangeRecord(op=INSERT, schema="s", table="t", key={"id": idv},
                        after={"id": idv, "v": "x"}, source_pos=pos)


def _numbered(trail_dir):
    return sorted(n for n in os.listdir(trail_dir) if n.startswith("trail.") and n != "trail.jsonl")


# --- backward compat: rotation disabled -> single file ------------------------


def test_no_rotation_keeps_single_file_and_global_cursor(tmp_path):
    t = Trail(str(tmp_path))  # rotate_mb=0
    t.append([_rec(i) for i in range(5)])
    assert os.path.exists(t.path) and _numbered(str(tmp_path)) == []
    out, cur = t.read(0)
    assert [r.key["id"] for r in out] == list(range(5)) and cur == 5


# --- segment rollover ---------------------------------------------------------


def test_rollover_creates_numbered_segment_and_spans_cursor(tmp_path):
    t = Trail(str(tmp_path))
    t.rotate_bytes = 120  # force rotation after ~1 small record
    t.append([_rec(1)])                 # segment 0 (trail.jsonl)
    t.append([_rec(2)])                 # segment 0 now over cap -> next append rotates
    t.append([_rec(3)])                 # rotates to trail.00001.jsonl
    assert _numbered(str(tmp_path)) == ["trail.00001.jsonl"]
    # The global cursor is continuous across the rotation boundary.
    out, cur = t.read(0)
    assert [r.key["id"] for r in out] == [1, 2, 3] and cur == 3
    # Reading from a mid-cursor that lands inside the second segment works too.
    out2, cur2 = t.read(2)
    assert [r.key["id"] for r in out2] == [3] and cur2 == 3


def test_multiple_rollovers_chain(tmp_path):
    t = Trail(str(tmp_path))
    t.rotate_bytes = 100
    for i in range(6):
        t.append([_rec(i)])
    # Each append after the active segment exceeds the cap starts a new segment.
    assert len(_numbered(str(tmp_path))) >= 2
    out, cur = t.read(0)
    assert [r.key["id"] for r in out] == list(range(6)) and cur == 6


# --- bounded read (H1 apply_batch composition) --------------------------------


def test_bounded_read_limits_records_and_advances_cursor(tmp_path):
    t = Trail(str(tmp_path))
    t.append([_rec(i) for i in range(10)])
    seen, start = [], 0
    while True:
        recs, nxt = t.read(start, limit=3)
        if not recs:
            break
        seen += [r.key["id"] for r in recs]
        assert len(recs) <= 3
        start = nxt
    assert seen == list(range(10)) and start == 10


def test_bounded_read_spans_segments(tmp_path):
    t = Trail(str(tmp_path))
    t.rotate_bytes = 100
    for i in range(8):
        t.append([_rec(i)])
    seen, start = [], 0
    while True:
        recs, nxt = t.read(start, limit=2)
        if not recs:
            break
        seen += [r.key["id"] for r in recs]
        start = nxt
    assert seen == list(range(8)) and start == 8


# --- purge safety -------------------------------------------------------------


def test_purge_applied_removes_only_fully_applied_closed_segments(tmp_path):
    t = Trail(str(tmp_path))
    t.rotate_bytes = 100
    for i in range(6):
        t.append([_rec(i)])
    segs_before = t.segment_paths()
    assert len(segs_before) >= 3  # several closed + one active
    total = t.line_count()
    # Purge with the cursor set PAST the first closed segment only.
    first_seg_lines = None
    with open(segs_before[0]) as f:
        first_seg_lines = sum(1 for _ in f)
    deleted = t.purge_applied(first_seg_lines)   # exactly the first segment applied
    assert len(deleted) == 1 and not os.path.exists(segs_before[0])
    # The global cursor is still valid: reading from the purge point returns the
    # remaining records with the SAME global indices as before the purge.
    out, cur = t.read(first_seg_lines)
    assert [r.key["id"] for r in out] == list(range(first_seg_lines, 6))
    assert cur == total == 6
    assert t.line_count() == 6  # purged lines still counted in the global total


def test_purge_never_deletes_active_or_past_cursor(tmp_path):
    t = Trail(str(tmp_path))
    t.rotate_bytes = 100
    for i in range(6):
        t.append([_rec(i)])
    segs = t.segment_paths()
    # cursor=0 (nothing applied) -> nothing may be purged.
    assert t.purge_applied(0) == []
    assert t.segment_paths() == segs
    # A huge cursor must still never delete the ACTIVE (last) segment.
    t.purge_applied(10_000)
    remaining = t.segment_paths()
    assert remaining and remaining[-1] == segs[-1]  # active survives


def test_purge_noop_on_single_file_trail(tmp_path):
    t = Trail(str(tmp_path))
    t.append([_rec(i) for i in range(3)])
    assert t.purge_applied(3) == []          # only the active segment exists
    out, cur = t.read(0)
    assert [r.key["id"] for r in out] == [0, 1, 2] and cur == 3


# --- torn tail across segments ------------------------------------------------


def test_torn_tail_in_active_segment_stops_before_it(tmp_path):
    t = Trail(str(tmp_path))
    t.rotate_bytes = 100
    for i in range(4):
        t.append([_rec(i)])
    active = t._active_path()
    with open(active, "ab") as f:
        f.write(b'{"op":"I","schema":"s","tab')   # torn in-flight append
    out, cur = t.read(0)
    assert [r.key["id"] for r in out] == [0, 1, 2, 3]   # torn line excluded
    # heal truncates only the active segment's torn fragment.
    assert t.heal_torn_tail() is True
    out2, cur2 = t.read(0)
    assert [r.key["id"] for r in out2] == [0, 1, 2, 3] and cur2 == 4


def test_unterminated_line_in_closed_segment_raises(tmp_path):
    t = Trail(str(tmp_path))
    t.rotate_bytes = 100
    for i in range(4):
        t.append([_rec(i)])
    segs = t.segment_paths()
    closed = segs[0]
    # Corrupt a CLOSED (non-active) segment with an unterminated line.
    with open(closed, "ab") as f:
        f.write(b'{"op":"I","schema":"s"')
    with pytest.raises(Any2HeliosError) as ei:
        t.read(0)
    assert "closed segment" in str(ei.value) or "corrupt" in str(ei.value).lower()


def test_read_caches_closed_segment_counts_and_skips_them(tmp_path):
    # Reading from a cursor past several closed segments must not re-scan them each
    # chunk: their (immutable) line counts are cached, and the active segment is
    # never cached. Correctness is preserved either way.
    t = Trail(str(tmp_path))
    t.rotate_bytes = 50  # one record per segment
    for i in range(6):
        t.append([_rec(i)])
    active_idx = t._active_index()
    # Read from a mid cursor that lands past the first few closed segments.
    out, cur = t.read(4)
    assert [r.key["id"] for r in out] == [4, 5] and cur == 6
    # Closed segments touched below the cursor are cached; the active one is not.
    assert t._seg_line_counts, "closed-segment line counts should be cached"
    assert active_idx not in t._seg_line_counts
    # A second read from an even higher cursor still returns the right tail.
    out2, cur2 = t.read(5)
    assert [r.key["id"] for r in out2] == [5] and cur2 == 6


def test_last_source_pos_spans_to_previous_segment_when_active_empty(tmp_path):
    t = Trail(str(tmp_path))
    t.rotate_bytes = 100
    t.append([_rec(1, pos=100)])
    t.append([_rec(2, pos=200)])   # segment 0 over cap
    t.append([_rec(3, pos=300)])   # -> segment 1 active, tail pos 300
    assert t.last_source_pos() == 300
    # Rotation preserves the tail read used by extract-start dedup.
