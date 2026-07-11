"""K2 — extract-side dedup so a crash between trail.append and the position write
never re-appends the same source events as duplicate trail lines.

Covers the trail tail-read (`last_source_pos`), the engine's `_drop_already_trailed`
filter, and a full MySQL extract cycle that re-captures an overlapping window
(the crash-replay) yet appends no duplicate. Legacy trails (records without a
`source_pos`) skip dedup unchanged, and a mixed old/new-format trail still parses.
"""
from __future__ import annotations

import os

import pytest

from any2heliosdb.cdc.engine import _drop_already_trailed, run_extract
from any2heliosdb.cdc.posfile import read_pos, write_pos_atomic
from any2heliosdb.cdc.sources.mysql_binlog import binlog_pos_to_int
from any2heliosdb.cdc.sources.postgres_logical import lsn_to_int
from any2heliosdb.cdc.trail import Trail
from any2heliosdb.config.model import Options, ProjectConfig, SourceConfig, TargetConfig
from any2heliosdb.constants import SourceDialect, TargetDriverKind
from any2heliosdb.core.change_record import INSERT, UPDATE, ChangeRecord, source_pos_key
from any2heliosdb.errors import Any2HeliosError


def _rec(idv, pos, v="a"):
    return ChangeRecord(op=INSERT, schema="s", table="t", key={"id": idv},
                        after={"id": idv, "v": v}, source_pos=pos)


# --- position encoders --------------------------------------------------------


def test_lsn_to_int_is_monotonic_and_backward_compatible():
    assert lsn_to_int("0/1A2") == 0x1A2
    assert lsn_to_int("16/B2C50A8") == (0x16 << 32) | 0xB2C50A8
    assert lsn_to_int("1/0") < lsn_to_int("1/1") < lsn_to_int("2/0")
    assert lsn_to_int("") is None and lsn_to_int("garbage") is None


def test_binlog_pos_to_int_orders_by_file_then_offset():
    assert binlog_pos_to_int("mysql-bin.000001", 100) < binlog_pos_to_int("mysql-bin.000001", 200)
    # A file roll outranks any offset in the previous file.
    assert binlog_pos_to_int("mysql-bin.000001", 10 ** 9) < binlog_pos_to_int("mysql-bin.000002", 4)


def test_binlog_pos_to_int_fails_closed_on_non_numeric_suffix():
    # S5: a file name without a numeric sequence suffix cannot be ordered against
    # a later file; degrading to index 0 could encode new events below the trail
    # tail and silently drop them, so it must raise (never over-drop).
    import pytest

    from any2heliosdb.errors import Any2HeliosError
    with pytest.raises(Any2HeliosError) as ei:
        binlog_pos_to_int("weird-name", 5)
    assert "numeric sequence suffix" in str(ei.value)


# --- trail tail read ----------------------------------------------------------


def test_last_source_pos_empty_and_missing_trail(tmp_path):
    t = Trail(str(tmp_path))
    assert t.last_source_pos() is None            # no file yet
    t.append([])
    assert t.last_source_pos() is None            # still nothing appended


def test_last_source_pos_returns_last_records_position(tmp_path):
    t = Trail(str(tmp_path))
    t.append([_rec(1, 100), _rec(2, 200)])
    t.append([_rec(3, 350)])
    assert t.last_source_pos() == 350             # tail record wins


def test_last_source_pos_none_for_legacy_last_line(tmp_path):
    t = Trail(str(tmp_path))
    t.append([_rec(1, 100), ChangeRecord(op=INSERT, schema="s", table="t",
                                         key={"id": 2}, after={"id": 2})])  # no source_pos
    assert t.last_source_pos() is None            # legacy last line -> no dedup


def test_last_source_pos_survives_large_tail(tmp_path):
    # More than one read chunk of records: the backward tail read still finds the
    # last one without a full forward scan mis-parsing an interior line.
    t = Trail(str(tmp_path))
    t.append([_rec(i, i * 10, v="x" * 50) for i in range(1, 400)])
    assert t.last_source_pos() == 399 * 10


def test_mixed_old_and_new_format_trail_reads_back(tmp_path):
    t = Trail(str(tmp_path))
    legacy = ChangeRecord(op=UPDATE, schema="s", table="t", key={"id": 9}, after={"id": 9, "v": "z"})
    t.append([legacy, _rec(10, 500)])
    out, cur = t.read(0)
    assert cur == 2 and len(out) == 2
    assert out[0].source_pos is None and out[1].source_pos == 500


# --- the dedup filter ---------------------------------------------------------


def test_drop_already_trailed_drops_overlap_keeps_new(tmp_path):
    t = Trail(str(tmp_path))
    t.append([_rec(1, 100), _rec(2, 200)])        # already durable (last pos 200)
    recapture = [_rec(1, 100), _rec(2, 200), _rec(3, 300)]  # crash-replay overlap + new
    kept = _drop_already_trailed(t, recapture)
    assert [r.source_pos for r in kept] == [300]  # <=200 dropped, only the new one kept


def test_drop_already_trailed_noop_on_empty_trail(tmp_path):
    t = Trail(str(tmp_path))
    recs = [_rec(1, 100), _rec(2, 200)]
    assert _drop_already_trailed(t, recs) == recs  # nothing to dedup against


def test_drop_already_trailed_noop_on_legacy_trail(tmp_path):
    # Last trail record has no source_pos -> dedup disabled (legacy source such as
    # Oracle SCN, whose upserts are idempotent anyway).
    t = Trail(str(tmp_path))
    t.append([ChangeRecord(op=INSERT, schema="s", table="t", key={"id": 1}, after={"id": 1})])
    recs = [_rec(2, 200)]
    assert _drop_already_trailed(t, recs) == recs


# --- S2: compound source_pos total order (multi-row event prefix crash) --------


def test_source_pos_key_int_and_compound_total_order():
    # A legacy/singleton int p orders identically to compound [p, 0], and seq
    # breaks ties within one base coordinate.
    assert source_pos_key(100) == source_pos_key([100, 0]) == (100, 0)
    assert source_pos_key([100, 0]) < source_pos_key([100, 1]) < source_pos_key([101, 0])
    assert source_pos_key(None) is None


def test_prefix_crash_compound_reappends_only_missing_tail(tmp_path):
    # S2 repro (compound): one 3-row binlog event -> rows share a base coordinate,
    # tagged [base,0]/[base,1]/[base,2]. A crash trailed only rows 0 and 1 (row 2
    # lost). The re-capture re-delivers all three; dedup must keep ONLY row 2 — the
    # old all-share-one-int scheme dropped it forever (<= last).
    base = binlog_pos_to_int("mysql-bin.000001", 100)
    t = Trail(str(tmp_path))
    t.append([_rec(1, [base, 0]), _rec(2, [base, 1])])   # rows 0,1 durable
    recapture = [_rec(1, [base, 0]), _rec(2, [base, 1]), _rec(3, [base, 2])]
    kept = _drop_already_trailed(t, recapture)
    assert [r.key["id"] for r in kept] == [3]            # only the never-trailed tail


def test_prefix_crash_int_singletons_reappends_only_missing(tmp_path):
    # S2 repro (plain-int singletons, distinct bases — round-4 single-row events):
    # a prefix crash trailed 2 of 3; dedup re-appends only the third.
    t = Trail(str(tmp_path))
    t.append([_rec(1, 100), _rec(2, 200)])
    kept = _drop_already_trailed(t, [_rec(1, 100), _rec(2, 200), _rec(3, 300)])
    assert [r.key["id"] for r in kept] == [3]


def test_prefix_crash_legacy_trail_disables_dedup(tmp_path):
    # S2 repro (legacy untagged trail): no positions -> dedup disabled (Oracle-SCN
    # style, idempotent apply), so nothing is dropped. Behaviour unchanged.
    t = Trail(str(tmp_path))
    t.append([ChangeRecord(op=INSERT, schema="s", table="t", key={"id": 1}, after={"id": 1}),
              ChangeRecord(op=INSERT, schema="s", table="t", key={"id": 2}, after={"id": 2})])
    recs = [_rec(3, 300), _rec(4, 400)]
    assert _drop_already_trailed(t, recs) == recs


# --- S1: torn-tail self-heal + torn-aware trail read --------------------------


def _append_raw(path, text):
    """Append raw bytes to the trail, simulating a crash that persisted a prefix."""
    with open(path, "ab") as f:
        f.write(text.encode("utf-8"))


def test_last_source_pos_skips_torn_final_fragment(tmp_path):
    t = Trail(str(tmp_path))
    t.append([_rec(1, 100), _rec(2, 200)])
    _append_raw(t.path, '{"op":"I","schema":"s","tab')     # torn tail
    # Skips the torn fragment and returns the last COMPLETE record's position, so
    # dedup stays keyed off a durable record rather than disabled.
    assert t.last_source_pos() == 200


def test_heal_torn_tail_truncates_to_last_complete_line(tmp_path):
    t = Trail(str(tmp_path))
    t.append([_rec(1, 100), _rec(2, 200)])
    _append_raw(t.path, '{"op":"I","schema":"s","tab')     # torn tail
    assert t.heal_torn_tail() is True                      # truncated
    out, cur = t.read(0)
    assert cur == 2 and [r.key["id"] for r in out] == [1, 2]
    assert t.heal_torn_tail() is False                     # idempotent: now clean


def test_heal_torn_tail_noop_on_clean_trail(tmp_path):
    t = Trail(str(tmp_path))
    t.append([_rec(1, 100)])
    assert t.heal_torn_tail() is False


def test_heal_torn_tail_removes_lone_torn_first_record(tmp_path):
    # A crash during the very first append leaves only a torn fragment (no complete
    # line): healing truncates the file to empty.
    t = Trail(str(tmp_path))
    _append_raw(t.path, '{"op":"I","schem')
    assert t.heal_torn_tail() is True
    out, cur = t.read(0)
    assert out == [] and cur == 0


def test_read_stops_before_torn_final_line(tmp_path):
    t = Trail(str(tmp_path))
    t.append([_rec(1, 100), _rec(2, 200)])
    _append_raw(t.path, '{"op":"I","schema":"s"')          # torn in-flight append
    out, cur = t.read(0)
    assert [r.key["id"] for r in out] == [1, 2]            # applied up to last complete
    assert cur == 2                                        # cursor EXCLUDES the torn line


def test_read_raises_on_corrupt_mid_file_line(tmp_path):
    t = Trail(str(tmp_path))
    t.append([_rec(1, 100)])
    _append_raw(t.path, "this is not json\n")              # terminated but corrupt
    t.append([_rec(3, 300)])                               # a real line after it
    with pytest.raises(Any2HeliosError) as ei:
        t.read(0)
    assert "corrupt" in str(ei.value).lower()


def test_mysql_extract_self_heals_torn_tail_no_dup_no_loss(tmp_path, monkeypatch):
    # Full extract cycle: a prior run appended R1 fully + R2 partially (torn), then
    # crashed before the pos write. The next extract heals the torn tail, re-reads
    # from the stale cursor, and via dedup re-appends R2 + a new R3 — no duplicate
    # of R1, no loss of R2.
    from any2heliosdb.config import store
    from any2heliosdb.cdc.sources import mysql_binlog

    name = "eh"
    trail_dir = os.path.join(str(tmp_path), "trail", name)
    posf = os.path.join(trail_dir, "binlog.pos")
    p1 = binlog_pos_to_int("mysql-bin.000001", 100)
    p2 = binlog_pos_to_int("mysql-bin.000001", 200)
    p3 = binlog_pos_to_int("mysql-bin.000001", 300)
    t = Trail(trail_dir)
    t.append([_rec(1, p1)])                                # R1 durable
    _append_raw(t.path, '{"op":"I","schema":"s","table":"t","key":{"i')  # R2 torn
    write_pos_atomic(posf, "mysql-bin.000001:50")          # stale cursor (pre-R1)

    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())

    class _FakeSource:
        def __init__(self, *a, **k):
            pass

        def capture(self, since, limit=0):
            return ([_rec(1, p1), _rec(2, p2), _rec(3, p3)], "mysql-bin.000001:300")

    monkeypatch.setattr(mysql_binlog, "MySqlBinlogSource", _FakeSource)
    res = run_extract(_cfg(tmp_path), name)
    assert res["captured"] == 2                            # R2 (re) + R3 (new)
    out, cur = Trail(trail_dir).read(0)
    assert cur == 3 and [r.key["id"] for r in out] == [1, 2, 3]   # no dup, no loss
    assert [r.source_pos for r in out] == [p1, p2, p3]
    assert read_pos(posf) == "mysql-bin.000001:300"


def test_drop_already_trailed_keeps_positionless_records(tmp_path):
    # A record without a source_pos is never dropped (can't be ordered).
    t = Trail(str(tmp_path))
    t.append([_rec(1, 100)])
    legacy = ChangeRecord(op=INSERT, schema="s", table="t", key={"id": 2}, after={"id": 2})
    kept = _drop_already_trailed(t, [_rec(1, 100), legacy, _rec(3, 300)])
    assert [r.key["id"] for r in kept] == [2, 3]  # pos 100 dropped; legacy + new kept


# --- full MySQL extract cycle: crash-replay appends no duplicate --------------


class _FakeTable:
    def __init__(self, name):
        self.name = name


class _FakeSchemaIR:
    name = "hr"
    tables = [_FakeTable("t")]


class _FakeAdapter:
    def connect(self):
        pass

    def introspect_schema(self, schema):
        return _FakeSchemaIR()

    def close(self):
        pass


def _cfg(tmp_path):
    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.MYSQL, host="h", port=3306,
                            database="hr", schema="hr", user="u", password="p"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG),
        options=Options(output_dir=str(tmp_path)))


def test_mysql_extract_crash_replay_appends_no_duplicate(tmp_path, monkeypatch):
    from any2heliosdb.config import store
    from any2heliosdb.cdc.sources import mysql_binlog

    name = "e1"
    trail_dir = os.path.join(str(tmp_path), "trail", name)
    posf = os.path.join(trail_dir, "binlog.pos")

    # Simulate the crash state: batch R1 is already durable in the trail, but the
    # pos file still holds the OLD coordinate (the pos write never happened).
    p1 = binlog_pos_to_int("mysql-bin.000001", 100)
    p2 = binlog_pos_to_int("mysql-bin.000001", 200)
    p3 = binlog_pos_to_int("mysql-bin.000001", 300)
    Trail(trail_dir).append([_rec(1, p1), _rec(2, p2)])
    write_pos_atomic(posf, "mysql-bin.000001:50")  # stale (pre-R1) coordinate

    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())

    class _FakeSource:
        def __init__(self, *a, **k):
            pass

        def capture(self, since, limit=0):
            # Re-reading from the stale coordinate re-delivers R1 (pos 100/200)
            # plus a genuinely new event R2 (pos 300).
            return ([_rec(1, p1), _rec(2, p2), _rec(3, p3)], "mysql-bin.000001:300")

    monkeypatch.setattr(mysql_binlog, "MySqlBinlogSource", _FakeSource)

    res = run_extract(_cfg(tmp_path), name)
    assert res["captured"] == 1                    # only the new event appended
    out, cur = Trail(trail_dir).read(0)
    assert cur == 3                                 # R1(2) + R2(1), no duplicate lines
    assert [r.source_pos for r in out] == [p1, p2, p3]
    assert read_pos(posf) == "mysql-bin.000001:300"  # cursor advanced past the batch


# --- dedup window gating & coordinate-epoch guard ------------------------------


def test_drop_already_trailed_fresh_anchor_never_drops(tmp_path):
    # A fresh anchor (since_key=None) starts at the source's CURRENT position and
    # can never re-read old events; a surviving (possibly other-epoch) tail must
    # not cause drops — the stale-tail sanity lives in _require_same_epoch.
    t = Trail(str(tmp_path))
    t.append([_rec(1, binlog_pos_to_int("mysql-bin.000042", 500))])  # high old tail
    recs = [_rec(2, binlog_pos_to_int("mysql-bin.000001", 100))]     # new-epoch event
    assert _drop_already_trailed(t, recs, since_key=None) == recs


def test_drop_already_trailed_no_window_when_tail_behind_since(tmp_path):
    # The crash-window overlap can only cover (since, tail]; a tail BEHIND the
    # resume coordinate means no window exists, so nothing may be dropped even if
    # a captured record orders at-or-below the tail.
    t = Trail(str(tmp_path))
    t.append([_rec(1, 200)])                                  # tail (200, 0)
    recs = [_rec(2, 150), _rec(3, 400)]
    kept = _drop_already_trailed(t, recs, since_key=(300, 0))  # since ahead of tail
    assert kept == recs


def test_mysql_coord_key_parses_pos_strings():
    from any2heliosdb.cdc.engine import _mysql_coord_key

    assert _mysql_coord_key("mysql-bin.000002:34") == \
        source_pos_key(binlog_pos_to_int("mysql-bin.000002", 34))
    assert _mysql_coord_key("") is None
    assert _mysql_coord_key("no-colon") is None


def test_epoch_guard_raises_when_tail_base_ahead(tmp_path):
    from any2heliosdb.cdc.engine import _mysql_coord_key, _require_same_epoch

    t = Trail(str(tmp_path))
    t.append([_rec(1, binlog_pos_to_int("mysql-bin.000042", 500))])
    with pytest.raises(Any2HeliosError) as ei:
        _require_same_epoch(t, _mysql_coord_key("mysql-bin.000001:300"), "e", "RESET MASTER")
    msg = str(ei.value)
    assert "coordinate" in msg and "Archive" in msg


def test_epoch_guard_tolerates_multirow_tail_at_stream_end(tmp_path):
    # Rows of a multi-row event share the event's END coordinate as their base
    # with seq 0..n; a tail of (P, 2) is the normal state when the stream end is
    # P — only a strictly greater BASE means the coordinate space restarted.
    from any2heliosdb.cdc.engine import _mysql_coord_key, _require_same_epoch

    base = binlog_pos_to_int("mysql-bin.000007", 900)
    t = Trail(str(tmp_path))
    t.append([_rec(1, [base, 0]), _rec(2, [base, 1]), _rec(3, [base, 2])])
    _require_same_epoch(t, _mysql_coord_key("mysql-bin.000007:900"), "e", "n/a")  # no raise
    _require_same_epoch(t, None, "e", "n/a")                                      # no end: no-op


def test_mysql_fresh_anchor_epoch_restart_fails_closed(tmp_path, monkeypatch):
    # Operator re-anchored (deleted the pos file) after RESET MASTER: the surviving
    # trail tail (file 000042) orders ahead of the fresh stream end (file 000001).
    # Silently deduping would drop every new event as "already trailed"
    # (captured=0 forever); the extract must fail closed instead, and must NOT
    # append across the epoch break.
    from any2heliosdb.config import store
    from any2heliosdb.cdc.sources import mysql_binlog

    name = "ep1"
    trail_dir = os.path.join(str(tmp_path), "trail", name)
    Trail(trail_dir).append([_rec(1, binlog_pos_to_int("mysql-bin.000042", 500))])
    # NOTE: no pos file -> fresh anchor.

    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())

    class _FakeSource:
        def __init__(self, *a, **k):
            pass

        def capture(self, since, limit=0):
            return ([_rec(2, binlog_pos_to_int("mysql-bin.000001", 100))],
                    "mysql-bin.000001:300")

    monkeypatch.setattr(mysql_binlog, "MySqlBinlogSource", _FakeSource)
    with pytest.raises(Any2HeliosError) as ei:
        run_extract(_cfg(tmp_path), name)
    assert "Archive" in str(ei.value)
    out, cur = Trail(trail_dir).read(0)
    assert cur == 1 and [r.key["id"] for r in out] == [1]   # nothing appended


def test_mysql_fresh_anchor_same_epoch_appends_without_dedup(tmp_path, monkeypatch):
    # Fresh anchor with a same-epoch surviving tail (tail base <= stream end):
    # nothing to guard against, nothing deduped — the new event appends normally
    # and the pos file is (re)established.
    from any2heliosdb.config import store
    from any2heliosdb.cdc.sources import mysql_binlog

    name = "ep2"
    trail_dir = os.path.join(str(tmp_path), "trail", name)
    posf = os.path.join(trail_dir, "binlog.pos")
    Trail(trail_dir).append([_rec(1, binlog_pos_to_int("mysql-bin.000001", 100))])

    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())

    class _FakeSource:
        def __init__(self, *a, **k):
            pass

        def capture(self, since, limit=0):
            return ([_rec(2, binlog_pos_to_int("mysql-bin.000001", 300))],
                    "mysql-bin.000001:300")

    monkeypatch.setattr(mysql_binlog, "MySqlBinlogSource", _FakeSource)
    res = run_extract(_cfg(tmp_path), name)
    assert res["captured"] == 1
    out, cur = Trail(trail_dir).read(0)
    assert cur == 2 and [r.key["id"] for r in out] == [1, 2]
    assert read_pos(posf) == "mysql-bin.000001:300"


# --- PG coordinate-epoch identity (system_identifier:timeline) -----------------


def _pg_cfg(tmp_path):
    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.POSTGRESQL, host="h", port=5432,
                            database="hr", schema="public", user="u", password="p"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG),
        options=Options(output_dir=str(tmp_path)))


class _FakePgSource:
    """Engine-facing fake: identity + records set per test via class attrs."""
    identity = "sys1:1"
    records = ()
    last_lsn = "0/200"
    advanced = []

    def __init__(self, *a, **k):
        pass

    def epoch_identity(self):
        return type(self).identity

    def capture(self, limit=0):
        return (list(type(self).records), type(self).last_lsn, [])

    def advance(self, lsn):
        type(self).advanced.append(lsn)


def _patch_pg(monkeypatch, identity, records, last_lsn="0/200"):
    from any2heliosdb.config import store
    from any2heliosdb.cdc.sources import postgres_logical

    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())
    _FakePgSource.identity = identity
    _FakePgSource.records = tuple(records)
    _FakePgSource.last_lsn = last_lsn
    _FakePgSource.advanced = []
    monkeypatch.setattr(postgres_logical, "PostgresLogicalSource", _FakePgSource)


def test_pg_epoch_identity_mismatch_fails_closed(tmp_path, monkeypatch):
    # The refuter's straddling-rewind attack: a PITR-restored cluster (new
    # timeline) emits LSNs that straddle the stale trail tail, which the
    # LSN-order guard cannot distinguish from a crash-window overlap. The
    # persisted coordinate-space identity catches it BEFORE capture/dedup can
    # silently drop the genuinely new below-tail changes.
    name = "pgep1"
    trail_dir = os.path.join(str(tmp_path), "trail", name)
    Trail(trail_dir).append([_rec(1, lsn_to_int("0/100"))])       # old-epoch tail
    write_pos_atomic(os.path.join(trail_dir, "epoch.id"), "sysOLD:1")

    _patch_pg(monkeypatch, identity="sysNEW:2",
              records=[_rec(2, lsn_to_int("0/60")), _rec(3, lsn_to_int("0/150"))])
    with pytest.raises(Any2HeliosError) as ei:
        run_extract(_pg_cfg(tmp_path), name)
    msg = str(ei.value)
    assert "epoch" in msg and "Archive" in msg
    out, cur = Trail(trail_dir).read(0)
    assert cur == 1 and [r.key["id"] for r in out] == [1]         # nothing appended
    assert _FakePgSource.advanced == []                           # slot untouched


def test_pg_epoch_identity_first_run_writes_file_then_matches(tmp_path, monkeypatch):
    name = "pgep2"
    trail_dir = os.path.join(str(tmp_path), "trail", name)
    _patch_pg(monkeypatch, identity="sysA:1", records=[_rec(1, lsn_to_int("0/50"))],
              last_lsn="0/50")
    res = run_extract(_pg_cfg(tmp_path), name)
    assert res["captured"] == 1
    with open(os.path.join(trail_dir, "epoch.id")) as f:
        assert f.read().strip() == "sysA:1"                       # identity persisted
    # Second run, same identity: proceeds and appends normally.
    _patch_pg(monkeypatch, identity="sysA:1", records=[_rec(2, lsn_to_int("0/80"))],
              last_lsn="0/80")
    res = run_extract(_pg_cfg(tmp_path), name)
    assert res["captured"] == 1
    out, cur = Trail(trail_dir).read(0)
    assert cur == 2 and [r.key["id"] for r in out] == [1, 2]


def test_pg_epoch_identity_unavailable_skips_check(tmp_path, monkeypatch):
    # Restricted environments where pg_control_* is unavailable: the identity
    # probe returns None -> check skipped (LSN-order guard still applies), no
    # file written, capture proceeds.
    name = "pgep3"
    trail_dir = os.path.join(str(tmp_path), "trail", name)
    _patch_pg(monkeypatch, identity=None, records=[_rec(1, lsn_to_int("0/50"))],
              last_lsn="0/50")
    res = run_extract(_pg_cfg(tmp_path), name)
    assert res["captured"] == 1
    assert not os.path.exists(os.path.join(trail_dir, "epoch.id"))


def test_drop_already_trailed_never_drops_malformed_pos(tmp_path):
    # A malformed source_pos (codec bug / hand-edited trail) has no order key;
    # dropping it would turn a bug into silent data loss — it must be kept.
    t = Trail(str(tmp_path))
    t.append([_rec(1, 500)])                                      # tail (500, 0)
    weird = ChangeRecord(op=INSERT, schema="s", table="t", key={"id": 2},
                         after={"id": 2}, source_pos="garbage")   # type: ignore[arg-type]
    kept = _drop_already_trailed(t, [_rec(3, 400), weird], since_key=(100, 0))
    assert [r.key["id"] for r in kept] == [2]                     # 400 dropped, weird kept
