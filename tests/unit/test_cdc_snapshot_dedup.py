"""M1 (H2/H1) — snapshot source_pos must not collide with the cycle's captured events.

Snapshot rows are tagged ``[base, seq]`` at the cycle's advance coordinate. If they
restart ``seq`` at 0 they reuse the seqs the cycle's last multi-row event already
occupies at that same ``base`` — the trail tail stops being monotonic and a
crash-window re-capture of the event re-appends its higher-seq rows past dedup
(a duplicate row, potentially a duplicate keymove). The fix starts the snapshot
seq ABOVE the maximum seq any captured record used at that base.
"""
from __future__ import annotations

from any2heliosdb.cdc.engine import (
    _drop_already_trailed, _snapshot_start_seq, _snapshot_tables)
from any2heliosdb.cdc.trail import Trail
from any2heliosdb.core.catalog_model import Column, DataType, PrimaryKey, Schema, Table
from any2heliosdb.core.change_record import INSERT, UPDATE, ChangeRecord, source_pos_key

_BASE = (5 << 48) | 900  # a plausible MySQL binlog-encoded base coordinate


def _event_row(seq):
    return ChangeRecord(op=INSERT, schema="db", table="orders", key={"ID": seq},
                        after={"ID": seq, "V": "x"}, source_pos=[_BASE, seq])


def _event_keymove(seq, new_id, old_id):
    return ChangeRecord(op=UPDATE, schema="db", table="orders", key={"ID": new_id},
                        after={"ID": new_id, "V": "moved"}, before_key={"ID": old_id},
                        source_pos=[_BASE, seq])


class _SnapAdapter:
    rows = {"t2": [(1, "a"), (2, "b"), (3, "c")]}

    def stream_rows(self, table, cols, where=None, arraysize=1000):
        for r in type(self).rows.get(table.name, []):
            yield r


def _t2_schema():
    return Schema(name="db", tables=[
        Table(name="t2", schema="db",
              columns=[Column("ID", DataType.decimal(10, 0)), Column("V", DataType.varchar(20))],
              primary_key=PrimaryKey(columns=["ID"]))])


def test_snapshot_start_seq_is_above_captured_event_max():
    # Five-row event at _BASE (seqs 0..4). The snapshot must start at seq 5.
    event = [_event_row(0), _event_row(1), _event_row(2), _event_row(3),
             _event_keymove(4, new_id=99, old_id=4)]
    assert _snapshot_start_seq(event, _BASE) == 5
    # A bare-int position counts as seq 0.
    assert _snapshot_start_seq([ChangeRecord(op=INSERT, schema="db", table="orders",
                                             key={}, source_pos=_BASE)], _BASE) == 1
    # No records at this base -> start at 0; base None (no dedup) -> 0.
    assert _snapshot_start_seq([], _BASE) == 0
    assert _snapshot_start_seq(event, None) == 0


def test_snapshot_seqs_keep_trail_tail_monotonic_so_recapture_is_deduped(tmp_path):
    trail = Trail(str(tmp_path))
    event = [_event_row(0), _event_row(1), _event_row(2), _event_row(3),
             _event_keymove(4, new_id=99, old_id=4)]
    trail.append(event)

    start = _snapshot_start_seq(event, _BASE)
    _snapshot_tables(_SnapAdapter(), trail, "db", _t2_schema(), ["t2"], _BASE, start_seq=start)

    # The trail tail now orders strictly ABOVE every captured event row.
    tail = source_pos_key(trail.last_source_pos())
    assert tail is not None and tail > source_pos_key([_BASE, 4])

    # So a crash-window re-capture of the SAME event is fully deduped — nothing
    # re-appended (with the buggy seq-0 snapshot the event's higher-seq rows,
    # including the keymove, would survive dedup and duplicate).
    kept = _drop_already_trailed(trail, list(event), since_key=(0, 0))
    assert kept == []


def test_snapshot_start_seq_folds_in_durable_tail():
    # M1 residual: when extract-start dedup dropped every re-captured record
    # (prior crash between append and pos write), the records list is empty but
    # the trail tail still holds [base, 0..k]. start_seq must come from the
    # TAIL, not restart at 0 and collide.
    from any2heliosdb.cdc.engine import _snapshot_start_seq

    base = 1000
    assert _snapshot_start_seq([], base, tail_key=(base, 4)) == 5
    assert _snapshot_start_seq([], base, tail_key=(999, 7)) == 0   # other base
    assert _snapshot_start_seq([], base, tail_key=None) == 0
    # Max over both sources: records reach seq 2, tail reaches seq 6.
    recs = [ChangeRecord(op=INSERT, schema="s", table="t", key={"id": i},
                         after={"id": i}, source_pos=[base, i]) for i in range(3)]
    assert _snapshot_start_seq(recs, base, tail_key=(base, 6)) == 7
