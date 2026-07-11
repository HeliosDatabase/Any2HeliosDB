"""Unit tests for the CDC spine: change-record codec, trail cursor, registry."""
import datetime
from decimal import Decimal

from any2heliosdb.cdc.registry import CdcRegistry
from any2heliosdb.cdc.trail import Trail
from any2heliosdb.core.change_record import UPDATE, ChangeRecord


def test_change_record_roundtrip_preserves_types():
    r = ChangeRecord(
        op=UPDATE, schema="HR", table="EMP",
        key={"ID": Decimal("5")},
        after={"ID": Decimal("5"), "NAME": "x",
               "HIRED": datetime.datetime(2020, 1, 2, 3, 4, 5),
               "PHOTO": b"\x00\x01\xff", "BAL": Decimal("123.45"), "NIL": None},
        scn=999)
    r2 = ChangeRecord.from_json(r.to_json())
    assert r2.op == "U" and r2.table == "EMP" and r2.scn == 999
    assert isinstance(r2.key["ID"], Decimal) and r2.key["ID"] == Decimal("5")
    assert r2.after["HIRED"] == datetime.datetime(2020, 1, 2, 3, 4, 5)
    assert r2.after["PHOTO"] == b"\x00\x01\xff"
    assert r2.after["BAL"] == Decimal("123.45")
    assert r2.after["NIL"] is None


def test_trail_append_and_incremental_cursor(tmp_path):
    t = Trail(str(tmp_path))
    recs = [ChangeRecord(op=UPDATE, schema="S", table="T", key={"id": i}, after={"id": i})
            for i in range(3)]
    assert t.append(recs) == 3
    out, cur = t.read(0)
    assert len(out) == 3 and cur == 3
    # reading again from the advanced cursor yields nothing
    out2, cur2 = t.read(cur)
    assert out2 == [] and cur2 == 3
    # appended records are visible from the old cursor only
    t.append([ChangeRecord(op=UPDATE, schema="S", table="T", key={"id": 9}, after={"id": 9})])
    out3, cur3 = t.read(cur)
    assert len(out3) == 1 and out3[0].key["id"] == 9 and cur3 == 4


def test_registry_tracks_watermark_and_cursor(tmp_path):
    reg = CdcRegistry(str(tmp_path / "cdc.db"))
    try:
        reg.register("e1", "HR", ["A", "B"])
        e = reg.get("e1")
        assert e is not None and e.watermark == 0 and e.apply_cursor == 0 and e.tables == ["A", "B"]
        reg.set_watermark("e1", 12345)
        reg.set_apply_cursor("e1", 7)
        e = reg.get("e1")
        assert e.watermark == 12345 and e.apply_cursor == 7
        assert [x.name for x in reg.list()] == ["e1"]
    finally:
        reg.close()


def test_version_tuple_parses_helios_banner():
    from any2heliosdb.cdc.engine import _version_tuple
    assert _version_tuple("HeliosDB-Nano 3.58.2") == (3, 58, 2)
    assert _version_tuple("3.58.1") == (3, 58, 1)
    assert _version_tuple("v3.60.0-rc1") == (3, 60, 0)
    assert _version_tuple("nano-no-version") is None
    assert _version_tuple("") is None
    assert _version_tuple(None) is None


def test_nano_cdc_apply_gate_threshold():
    # The replicat gate requires Nano >= 3.58.5 (the FK-index backfill correctness
    # fix; 3.58.3's E'...' bytea fix is also needed and subsumed). Older or
    # unparseable versions are refused so a pre-fix Nano can't silently corrupt
    # keyed upserts or serve index-driven lookups from an unbackfilled FK index.
    from any2heliosdb.cdc.engine import _NANO_MIN_CDC_VERSION, _version_tuple

    def allowed(v):
        t = _version_tuple(v)
        return t is not None and t >= _NANO_MIN_CDC_VERSION

    assert allowed("3.58.5") and allowed("3.59.0") and allowed("4.0.0")
    assert not allowed("3.58.4") and not allowed("3.58.3") and not allowed("3.0.0")
    assert not allowed("unknown")


# --- Replicat ordered-apply (BLOCKER 1) ---------------------------------------

from any2heliosdb.cdc.replicat import Replicat  # noqa: E402
from any2heliosdb.core.catalog_model import (  # noqa: E402
    Column,
    DataType,
    PrimaryKey,
    Schema,
    Table,
)
from any2heliosdb.core.change_record import DELETE, INSERT  # noqa: E402


class _UniqueViolation(Exception):
    """A move whose new key collides with a surviving different row — models the
    PK/unique-constraint error a real target raises on ``UPDATE ... SET key=... ``
    that lands on an occupied key (the R1 wedge the keymove apply must avoid)."""


class _FakeTarget:
    """In-memory apply seam that mirrors row state and logs the call order.

    Models the **merge-upsert** drivers (psycopg ``ON CONFLICT`` / MySQL ``ON
    DUPLICATE KEY UPDATE``): a conflicting ``upsert`` overlays only the provided
    columns and leaves omitted (unchanged-TOAST) columns intact. Keyed by (table,
    key-tuple) so we can assert final presence/absence after a mixed I/U/D stream;
    ``calls`` records the op-class sequence to prove the replicat didn't reorder
    upserts ahead of deletes; ``deleted_keys`` records the exact keys deleted so a
    test can prove the *parent* (old-key) row is never removed by a keymove.
    ``update_columns`` returns rows *matched* (the driver contract) and raises
    :class:`_UniqueViolation` if a key-changing UPDATE lands on an occupied key.
    """

    def __init__(self):
        self.rows = {}
        self.calls = []
        self.deleted_keys = []

    def upsert(self, target_table, key_cols, columns, rows):
        rows = list(rows)
        self.calls.append(("upsert", target_table, len(rows)))
        n = 0
        for r in rows:
            provided = dict(zip(columns, r))
            key = tuple(provided[k] for k in key_cols)
            # Model INSERT ... ON CONFLICT DO UPDATE SET <provided non-key cols>:
            # a new key inserts the provided columns; an existing key overlays only
            # the provided columns and LEAVES omitted (e.g. unchanged-TOAST) columns
            # untouched — never NULLing them.
            merged = dict(self.rows.get((target_table, key), {}))
            merged.update(provided)
            self.rows[(target_table, key)] = merged
            n += 1
        return n

    def update_columns(self, target_table, key_cols, set_cols, rows):
        # Model a real keyed UPDATE: each row is (*set-values, *key-values). Match
        # the key; SET only set_cols (omitted columns keep their value); if set_cols
        # includes a key column whose value changed, the row moves to the new key.
        # Return the number of rows actually MATCHED (0 -> caller falls back) — a
        # value-unchanged UPDATE of an existing row still counts as matched.
        rows = list(rows)
        self.calls.append(("update", target_table, len(rows)))
        nset = len(set_cols)
        matched = 0
        for r in rows:
            setvals, keyvals = r[:nset], r[nset:]
            key = tuple(keyvals)
            existing = self.rows.get((target_table, key))
            if existing is None:
                continue  # no row matched -> rowcount 0
            existing = dict(existing)
            existing.update(dict(zip(set_cols, setvals)))
            newkey = tuple(existing[k] for k in key_cols)
            if newkey != key and (target_table, newkey) in self.rows:
                # A real unique/PK constraint rejects moving onto an occupied key.
                raise _UniqueViolation(
                    "duplicate key {!r} on {}".format(newkey, target_table))
            if newkey != key:
                self.rows.pop((target_table, key), None)
            self.rows[(target_table, newkey)] = existing
            matched += 1
        return matched

    def delete_keys(self, target_table, key_cols, keys):
        keys = list(keys)
        self.calls.append(("delete", target_table, len(keys)))
        n = 0
        for k in keys:
            self.deleted_keys.append((target_table, tuple(k)))
            self.rows.pop((target_table, tuple(k)), None)
            n += 1
        return n


class _NativeFakeTarget(_FakeTarget):
    """Models the **native (Oracle) / plain-INSERT** driver whose ``upsert`` is a
    DELETE-by-key + INSERT: a conflicting key is REPLACED wholesale, so any column
    omitted from the provided tuple is dropped (not merged/preserved). This is the
    semantics the round-2 keymove fallback corrupted on (R2). ``update_columns`` /
    ``delete_keys`` (a real keyed UPDATE / DELETE, matched-count) are inherited
    unchanged from :class:`_FakeTarget`.
    """

    def upsert(self, target_table, key_cols, columns, rows):
        rows = list(rows)
        self.calls.append(("upsert", target_table, len(rows)))
        n = 0
        for r in rows:
            provided = dict(zip(columns, r))
            key = tuple(provided[k] for k in key_cols)
            self.rows[(target_table, key)] = dict(provided)  # REPLACE, no merge
            n += 1
        return n


def _schema(*tables):
    return Schema(name="HR", tables=list(tables))


def _t(name, key_col="ID"):
    return Table(
        name=name,
        schema="HR",
        columns=[Column(key_col, DataType.decimal(10, 0)), Column("V", DataType.varchar(10))],
        primary_key=PrimaryKey(columns=[key_col]),
    )


def _rec(op, table, idval, v=None):
    after = {} if op == DELETE else {"ID": idval, "V": v}
    return ChangeRecord(op=op, schema="HR", table=table, key={"ID": idval}, after=after)


def test_apply_delete_then_insert_leaves_row_present():
    # D id=7 then I id=7 must end PRESENT (the original split-by-op apply upserted
    # first then deleted, wrongly dropping the row).
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T")))
    applied, warnings = rep.apply([_rec(DELETE, "T", 7), _rec(INSERT, "T", 7, "new")])
    assert warnings == []
    assert applied == 2
    assert ("t", (7,)) in tgt.rows and tgt.rows[("t", (7,))]["v"] == "new"
    # And the delete batch must have run before the upsert batch.
    assert [c[0] for c in tgt.calls] == ["delete", "upsert"]


def test_apply_insert_delete_insert_leaves_row_present():
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T")))
    rep.apply([_rec(INSERT, "T", 7, "a"), _rec(DELETE, "T", 7), _rec(INSERT, "T", 7, "b")])
    assert tgt.rows[("t", (7,))]["v"] == "b"
    assert [c[0] for c in tgt.calls] == ["upsert", "delete", "upsert"]


def test_apply_insert_then_delete_leaves_row_absent():
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T")))
    rep.apply([_rec(INSERT, "T", 7, "a"), _rec(DELETE, "T", 7)])
    assert ("t", (7,)) not in tgt.rows
    assert [c[0] for c in tgt.calls] == ["upsert", "delete"]


def test_apply_contiguous_same_class_records_batch_into_one_call():
    # Ordering is preserved without sacrificing batching: a run of upserts is a
    # single driver round-trip, likewise a run of deletes.
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T")))
    rep.apply([
        _rec(INSERT, "T", 1, "a"), _rec(INSERT, "T", 2, "b"),
        _rec(DELETE, "T", 1), _rec(DELETE, "T", 2),
        _rec(INSERT, "T", 3, "c"),
    ])
    assert tgt.calls == [("upsert", "t", 2), ("delete", "t", 2), ("upsert", "t", 1)]
    assert ("t", (1,)) not in tgt.rows and ("t", (2,)) not in tgt.rows
    assert tgt.rows[("t", (3,))]["v"] == "c"


def test_apply_interleaved_keys_across_two_tables():
    # Two tables, interleaved in arrival order; each table's own order is honored.
    # T1: D1 then I1 -> present. T2: I2 then D2 -> absent.
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T1"), _t("T2")))
    rep.apply([
        _rec(DELETE, "T1", 1),
        _rec(INSERT, "T2", 2, "x"),
        _rec(INSERT, "T1", 1, "y"),
        _rec(DELETE, "T2", 2),
    ])
    assert tgt.rows[("t1", (1,))]["v"] == "y"
    assert ("t2", (2,)) not in tgt.rows


def test_apply_skips_table_without_primary_key():
    tgt = _FakeTarget()
    nopk = Table(name="NOPK", schema="HR", columns=[Column("V", DataType.varchar(10))])
    rep = Replicat(tgt, _schema(nopk))
    applied, warnings = rep.apply([_rec(INSERT, "NOPK", 1, "a")])
    assert applied == 0 and tgt.calls == []
    assert len(warnings) == 1 and "no primary key" in warnings[0]


# --- ChangeRecord before_key codec (BLOCKER 3c) -------------------------------


def test_change_record_before_key_roundtrips():
    r = ChangeRecord(op=UPDATE, schema="HR", table="EMP",
                     key={"ID": Decimal("202")}, after={"ID": Decimal("202"), "V": "x"},
                     before_key={"ID": Decimal("201")})
    line = r.to_json()
    assert '"before_key"' in line
    r2 = ChangeRecord.from_json(line)
    assert r2.before_key == {"ID": Decimal("201")}
    assert isinstance(r2.before_key["ID"], Decimal)
    assert r2.key == {"ID": Decimal("202")}


def test_change_record_without_before_key_keeps_wire_format():
    # The common (non-PK-changing) record must not grow the field, so existing
    # trails stay byte-for-byte identical.
    r = ChangeRecord(op=UPDATE, schema="S", table="T", key={"id": 5}, after={"id": 5})
    line = r.to_json()
    assert "before_key" not in line
    assert ChangeRecord.from_json(line).before_key == {}


def test_change_record_old_format_line_still_parses():
    # A trail line written before before_key existed (no field) must parse, with
    # an empty before_key — backward compatibility for on-disk trails.
    old = ('{"op":"U","schema":"S","table":"T","key":{"id":5},'
           '"after":{"id":5,"v":"a"},"scn":0,"commit_ts":""}')
    r = ChangeRecord.from_json(old)
    assert r.before_key == {} and r.key == {"id": 5} and r.after["v"] == "a"


# --- Replicat PK-changing UPDATE deletes the old-key row (BLOCKER 3c) ---------


def test_apply_pk_changing_update_moves_row_in_place_not_delete_upsert():
    # Round-3 semantics: a PK-changing UPDATE moves the row by updating its key
    # columns WHERE the OLD key (after clearing any stale new-key row) — never a
    # delete-then-upsert of the PARENT row (which would trip an FK on
    # immediate-checking targets) and never NULLing an omitted column.
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T")))
    rep.apply([_rec(INSERT, "T", 1, "orig")])          # seed the old-key row
    assert ("t", (1,)) in tgt.rows
    upd = ChangeRecord(op=UPDATE, schema="HR", table="T",
                       key={"ID": 2}, after={"ID": 2, "V": "moved"},
                       before_key={"ID": 1})
    applied, warnings = rep.apply([upd])
    assert warnings == []
    assert ("t", (1,)) not in tgt.rows                 # old key gone (moved in place)
    assert tgt.rows[("t", (2,))]["v"] == "moved"       # new key present
    assert applied == 1                                # one keymove change applied
    assert tgt.calls[-1][0] == "update"                # ends on the in-place move
    # The OLD-key (parent) row is moved, never deleted; only the (empty) new-key
    # slot is cleared beforehand, so no FK-tripping parent delete ever happens.
    assert ("t", (1,)) not in tgt.deleted_keys


def test_apply_pk_changing_update_preserves_unchanged_toast_column():
    # The reviewer's 3b×3c repro: the moved row's TOASTed BODY is unchanged (its
    # value only ever lived on the old-key row). The in-place UPDATE must carry it
    # to the new key rather than lose it to a delete + subset re-insert.
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t3("T")))
    rep.apply([ChangeRecord(op=INSERT, schema="HR", table="T", key={"ID": 11},
                            after={"ID": 11, "V": "pre", "BODY": "BIG-TOAST"})])
    rep.apply([ChangeRecord(op=UPDATE, schema="HR", table="T", key={"ID": 12},
                            after={"ID": 12, "V": "moved"},   # BODY omitted (unchanged)
                            before_key={"ID": 11})])
    assert ("t", (11,)) not in tgt.rows
    row = tgt.rows[("t", (12,))]
    assert row["v"] == "moved"
    assert row["body"] == "BIG-TOAST"                  # preserved across the PK move


def test_apply_pk_changing_update_falls_back_to_insert_when_old_key_absent():
    # rowcount==0 branch: the old-key row is not on the target (e.g. a replay that
    # starts mid-stream). The in-place UPDATE matches nothing, so we fall back to
    # inserting the provided columns under the new key (nothing to preserve).
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t3("T")))
    rep.apply([ChangeRecord(op=UPDATE, schema="HR", table="T", key={"ID": 12},
                            after={"ID": 12, "V": "moved", "BODY": "NEW"},
                            before_key={"ID": 11})])
    assert ("t", (11,)) not in tgt.rows
    row = tgt.rows[("t", (12,))]
    assert row["v"] == "moved" and row["body"] == "NEW"
    kinds = [c[0] for c in tgt.calls]
    assert "update" in kinds and "upsert" in kinds     # tried UPDATE, then inserted


def test_apply_update_without_key_change_does_not_delete():
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t("T")))
    rep.apply([_rec(INSERT, "T", 1, "a")])
    # before_key equal to the new key (or absent) must not trigger a delete.
    upd = ChangeRecord(op=UPDATE, schema="HR", table="T",
                       key={"ID": 1}, after={"ID": 1, "V": "b"}, before_key={"ID": 1})
    rep.apply([upd])
    assert tgt.rows[("t", (1,))]["v"] == "b"
    assert all(c[0] != "delete" for c in tgt.calls)


# --- Replicat column-subset (unchanged-TOAST) apply (BLOCKER 3b) --------------


def _t3(name):
    return Table(
        name=name, schema="HR",
        columns=[Column("ID", DataType.decimal(10, 0)), Column("V", DataType.varchar(10)),
                 Column("BODY", DataType.varchar(4000))],
        primary_key=PrimaryKey(columns=["ID"]),
    )


def test_apply_partial_update_leaves_omitted_toast_column_intact():
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t3("T")))
    rep.apply([ChangeRecord(op=INSERT, schema="HR", table="T", key={"ID": 1},
                            after={"ID": 1, "V": "a", "BODY": "BIG-TOAST-VALUE"})])
    assert tgt.rows[("t", (1,))]["body"] == "BIG-TOAST-VALUE"
    # UPDATE with BODY omitted (unchanged TOAST): must update V, NOT clobber BODY.
    rep.apply([ChangeRecord(op=UPDATE, schema="HR", table="T", key={"ID": 1},
                            after={"ID": 1, "V": "b"})])
    row = tgt.rows[("t", (1,))]
    assert row["v"] == "b"
    assert row["body"] == "BIG-TOAST-VALUE"     # untouched, not NULL, not clobbered


def test_apply_mixed_full_and_partial_images_preserve_order_and_batching():
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t3("T")))
    # Two full-image inserts batch into one upsert; then a partial update is its
    # own column-subset upsert (different signature) applied after, in order.
    rep.apply([
        ChangeRecord(op=INSERT, schema="HR", table="T", key={"ID": 1},
                     after={"ID": 1, "V": "a", "BODY": "X"}),
        ChangeRecord(op=INSERT, schema="HR", table="T", key={"ID": 2},
                     after={"ID": 2, "V": "c", "BODY": "Y"}),
        ChangeRecord(op=UPDATE, schema="HR", table="T", key={"ID": 1},
                     after={"ID": 1, "V": "a2"}),
    ])
    # Two full images batch into one upsert; the partial update is a keyed
    # column-subset UPDATE (its own call), applied after, in order.
    assert tgt.calls == [("upsert", "t", 2), ("update", "t", 1)]
    assert tgt.rows[("t", (1,))] == {"id": 1, "v": "a2", "body": "X"}
    assert tgt.rows[("t", (2,))] == {"id": 2, "v": "c", "body": "Y"}


def test_apply_partial_update_inserts_provided_columns_when_row_absent():
    # F1 rowcount==0 branch: a partial (TOAST-omitted) UPDATE for a row not yet on
    # the target (replay from a mid-stream cursor) inserts the columns we have; the
    # omitted column simply stays unset (there is no prior value to preserve).
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t3("T")))
    rep.apply([ChangeRecord(op=UPDATE, schema="HR", table="T", key={"ID": 1},
                            after={"ID": 1, "V": "b"})])   # BODY omitted, no such row yet
    row = tgt.rows[("t", (1,))]
    assert row["v"] == "b"
    assert "body" not in row                              # omitted stays unset (not NULL-clobber)
    assert [c[0] for c in tgt.calls] == ["update", "upsert"]  # UPDATE missed, then inserted


# --- MySQL binlog fail-closed verification (BLOCKER 2) ------------------------

import pytest  # noqa: E402

from any2heliosdb.cdc.sources.mysql_binlog import (  # noqa: E402
    _check_image_columns,
    _require_full_row_image,
)
from any2heliosdb.errors import Any2HeliosError  # noqa: E402


def _full_vars():
    return {"binlog_format": "ROW", "binlog_row_metadata": "FULL", "binlog_row_image": "FULL"}


def test_require_full_row_image_accepts_full_settings():
    # Case-insensitive on both names and values; no raise on a fully-FULL server.
    _require_full_row_image(_full_vars())
    _require_full_row_image({"BINLOG_FORMAT": "row", "binlog_row_metadata": "full",
                             "binlog_row_image": "Full"})


def test_require_full_row_image_rejects_minimal_row_image():
    v = _full_vars()
    v["binlog_row_image"] = "MINIMAL"
    with pytest.raises(Any2HeliosError) as ei:
        _require_full_row_image(v)
    assert "binlog_row_image" in str(ei.value)


def test_require_full_row_image_rejects_statement_format():
    v = _full_vars()
    v["binlog_format"] = "STATEMENT"
    with pytest.raises(Any2HeliosError) as ei:
        _require_full_row_image(v)
    assert "binlog_format" in str(ei.value)


def test_require_full_row_image_rejects_minimal_metadata():
    v = _full_vars()
    v["binlog_row_metadata"] = "MINIMAL"
    with pytest.raises(Any2HeliosError) as ei:
        _require_full_row_image(v)
    assert "binlog_row_metadata" in str(ei.value)


def test_require_full_row_image_rejects_missing_variable():
    # An unreadable variable (denied privilege) must fail closed, not pass.
    v = _full_vars()
    del v["binlog_row_image"]
    with pytest.raises(Any2HeliosError):
        _require_full_row_image(v)


def test_check_image_columns_accepts_full_image():
    _check_image_columns("T", "UPDATE", {"ID", "V", "NAME"}, ["ID", "V", "NAME"])


def test_check_image_columns_rejects_unknown_col_markers():
    with pytest.raises(Any2HeliosError) as ei:
        _check_image_columns("T", "INSERT", {"UNKNOWN_COL0", "UNKNOWN_COL1"}, ["ID", "V"])
    assert "binlog_row_metadata" in str(ei.value)


def test_check_image_columns_rejects_partial_image():
    # MINIMAL UPDATE: after-image omits the unchanged column -> would be NULLed.
    with pytest.raises(Any2HeliosError) as ei:
        _check_image_columns("T", "UPDATE", {"ID"}, ["ID", "V", "NAME"])
    assert "binlog_row_image" in str(ei.value)


# --- Replicat keymove replay-idempotency matrix (round 3) ---------------------
# Each scenario's slice, applied TWICE (a full replay — the at-least-once model
# re-runs the whole slice after a crash/failure), must reach the SAME final state
# as a single apply, with NO exception (no unique violation, the R1 wedge) and no
# lost unchanged-TOAST column (the R2 corruption) — against BOTH the merge-upsert
# semantics (_FakeTarget) AND the native DELETE+INSERT-upsert semantics
# (_NativeFakeTarget). The scenarios are exactly the ones the round-3 invariants
# name: [full INSERT, keymove], [keymove w/ omitted TOAST], [keymove, later
# partial UPDATE at the new key], plus a pure-PK change and a full-image keymove.


def _ins(idv, **cols):
    return ChangeRecord(op=INSERT, schema="HR", table="T", key={"ID": idv},
                        after={"ID": idv, **cols})


def _keymove(new_id, old_id, **after):
    return ChangeRecord(op=UPDATE, schema="HR", table="T",
                        key={"ID": new_id}, after={"ID": new_id, **after},
                        before_key={"ID": old_id})


def _partial(idv, **after):
    return ChangeRecord(op=UPDATE, schema="HR", table="T",
                        key={"ID": idv}, after={"ID": idv, **after})


def _final_rows(target_cls, table, preload, slice_recs, times):
    tgt = target_cls()
    rep = Replicat(tgt, _schema(table))
    if preload:
        rep.apply(list(preload))         # already-committed prefix (applied once)
    for _ in range(times):
        rep.apply(list(slice_recs))      # the slice under test (replayed `times`)
    return tgt.rows


# scenario -> (preload, slice, expected final rows)
_REPLAY_SCENARIOS = {
    "full_insert_then_keymove": (
        [], [_ins(1, V="a", BODY="BIG"), _keymove(2, 1, V="moved")],  # BODY omitted on move
        {("t", (2,)): {"id": 2, "v": "moved", "body": "BIG"}},
    ),
    "keymove_omitted_toast": (
        [_ins(1, V="pre", BODY="BIG")], [_keymove(2, 1, V="moved")],  # BODY unchanged/omitted
        {("t", (2,)): {"id": 2, "v": "moved", "body": "BIG"}},
    ),
    "keymove_then_partial_at_new_key": (
        [_ins(1, V="pre", BODY="BIG")],
        [_keymove(2, 1, V="moved"), _partial(2, V="v2")],             # BODY omitted twice
        {("t", (2,)): {"id": 2, "v": "v2", "body": "BIG"}},
    ),
    "pure_pk_change_values_unchanged": (
        [_ins(1, V="val", BODY="BIG")], [_keymove(2, 1, V="val")],    # only the PK changes
        {("t", (2,)): {"id": 2, "v": "val", "body": "BIG"}},
    ),
    "keymove_full_image": (
        [_ins(1, V="pre", BODY="OLD")], [_keymove(2, 1, V="moved", BODY="NEW")],  # no omission
        {("t", (2,)): {"id": 2, "v": "moved", "body": "NEW"}},
    ),
}


@pytest.mark.parametrize("target_cls", [_FakeTarget, _NativeFakeTarget],
                         ids=["merge", "native"])
@pytest.mark.parametrize("scenario", sorted(_REPLAY_SCENARIOS))
def test_keymove_replay_matrix_converges_without_exception(scenario, target_cls):
    preload, slice_recs, expected = _REPLAY_SCENARIOS[scenario]
    once = _final_rows(target_cls, _t3("T"), preload, slice_recs, times=1)
    twice = _final_rows(target_cls, _t3("T"), preload, slice_recs, times=2)  # full replay
    # Single apply and full replay converge to the identical state — and it is the
    # expected one, with the unchanged-TOAST BODY preserved on every driver.
    assert once == expected
    assert twice == expected


# --- K1: apply-cursor barrier around keymove records (round 4) -----------------
# The reviewer's round-3 repro proved that replaying a WHOLE trail slice that
# contains a keymove together with a neighbouring record can diverge: a keymove is
# the one op whose replay is not target-state-idempotent (old-key row "not yet
# moved" vs "impostor that reused the key" is undecidable from target state). K1
# closes this structurally: the engine persists the apply cursor just before AND
# just after every keymove, so a keymove can only ever REPLAY ALONE (proven
# convergent), never batched with the record before or after it. These tests drive
# Replicat.apply_barriered the way the engine does — a crash resumes from the last
# persisted cursor — and prove convergence across EVERY crash window.


def _barrier_segments(rep, records):
    """The (start, end) record-index ranges apply_barriered flushes as segments:
    each keymove alone, maximal non-keymove runs batched. Mirrors the engine's
    resume boundaries (each segment end is a persisted-cursor point)."""
    segs, start, cur = [], 0, []
    for idx, r in enumerate(records):
        if rep._is_keymove(r):
            if cur:
                segs.append((start, idx))
                cur = []
            segs.append((idx, idx + 1))
            start = idx + 1
        else:
            if not cur:
                start = idx
            cur.append(r)
    if cur:
        segs.append((start, len(records)))
    return segs


def _barrier_once(target_cls, table, preload, slice_recs):
    tgt = target_cls()
    rep = Replicat(tgt, _schema(table))
    if preload:
        rep.apply(list(preload))
    rep.apply_barriered(list(slice_recs))
    return tgt.rows


def _assert_barrier_converges(target_cls, table, preload, slice_recs):
    """apply-once == every crash-window resume, with no exception. A crash forces
    a resume from the last persisted cursor (a segment boundary); the segment it
    crashed in may have fully run (cursor lost) or not have run yet — both must
    converge to the once-through state."""
    expected = _barrier_once(target_cls, table, preload, slice_recs)
    segs = _barrier_segments(Replicat(target_cls(), _schema(table)), list(slice_recs))
    for (start_s, end_s) in segs:
        # crash_state = records durably applied when the crash hit: end_s (segment
        # ran, its cursor never persisted -> resume REPLAYS it) or start_s (segment
        # had not run yet). Resume re-reads records[start_s:] from the trail.
        for crash_state in (end_s, start_s):
            tgt = target_cls()
            rep = Replicat(tgt, _schema(table))
            if preload:
                rep.apply(list(preload))
            rep.apply_barriered(list(slice_recs[:crash_state]))
            rep.apply_barriered(list(slice_recs[start_s:]))
            assert tgt.rows == expected, (start_s, end_s, crash_state, tgt.rows, expected)
    return expected


# The reviewer's three diverging repro slices (repro_keymove.py), each × both
# driver semantics. Under the barrier every crash window converges.
_BARRIER_SCENARIOS = {
    # A keymove frees key 1, then a brand-new INSERT reuses key 1 in the same slice.
    "keymove_then_reinsert_old_key": (
        [_ins(1, V="pre", BODY="OLDBODY")],
        [_keymove(2, 1, V="moved"), _ins(1, V="b", BODY="NEWBODY")],
    ),
    # A partial (TOAST-omitted) UPDATE of the old-key row, then the keymove.
    "partial_then_keymove": (
        [_ins(1, V="pre", BODY="OLDBODY")],
        [_partial(1, V="v1"), _keymove(2, 1, V="moved")],
    ),
    # Chained keymoves 1->2->3 of one row in one slice, both omitting BODY.
    "chained_keymoves": (
        [_ins(1, V="pre", BODY="OLDBODY")],
        [_keymove(2, 1, V="m1"), _keymove(3, 2, V="m2")],
    ),
}


@pytest.mark.parametrize("target_cls", [_FakeTarget, _NativeFakeTarget],
                         ids=["merge", "native"])
@pytest.mark.parametrize("scenario", sorted(_BARRIER_SCENARIOS))
def test_keymove_barrier_repro_cells_converge(scenario, target_cls):
    preload, slice_recs = _BARRIER_SCENARIOS[scenario]
    _assert_barrier_converges(target_cls, _t3("T"), preload, slice_recs)


def test_keymove_barrier_reinsert_reaches_expected_state():
    # The full expected state, not just self-convergence: the moved row keeps the
    # OLD body, and the reused key holds the brand-new row's body.
    preload, slice_recs = _BARRIER_SCENARIOS["keymove_then_reinsert_old_key"]
    rows = _assert_barrier_converges(_FakeTarget, _t3("T"), preload, slice_recs)
    assert rows == {("t", (2,)): {"id": 2, "v": "moved", "body": "OLDBODY"},
                    ("t", (1,)): {"id": 1, "v": "b", "body": "NEWBODY"}}


@pytest.mark.parametrize("target_cls", [_FakeTarget, _NativeFakeTarget],
                         ids=["merge", "native"])
def test_chained_keymoves_one_slice_full_replay_preserves_toast(target_cls):
    # The refuted round-3 claim: chained keymoves 1->2->3 in one slice, replayed,
    # converge with the unchanged TOAST BODY preserved. Under the barrier each
    # keymove is its own segment, so the strongest replay is one keymove alone.
    preload, slice_recs = _BARRIER_SCENARIOS["chained_keymoves"]
    rows = _assert_barrier_converges(target_cls, _t3("T"), preload, slice_recs)
    assert rows == {("t", (3,)): {"id": 3, "v": "m2", "body": "OLDBODY"}}


def test_keymove_barrier_crash_pre_and_post_keymove_resume():
    # The spec's two named crash windows for a keymove segment. Slice = [partial,
    # keymove]: segment boundaries are after the partial (cursor pre-keymove) and
    # after the keymove (cursor post-keymove).
    table = _t3("T")
    preload = [_ins(1, V="pre", BODY="BIG")]
    slice_recs = [_partial(1, V="v1"), _keymove(2, 1, V="moved")]
    expected = {("t", (2,)): {"id": 2, "v": "moved", "body": "BIG"}}

    # (a) cursor persisted PRE-keymove (partial applied, keymove not yet run) then
    #     resume replays [keymove] alone.
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(table))
    rep.apply(list(preload))
    rep.apply_barriered([slice_recs[0]])           # segment 0: partial, cursor -> 1
    rep.apply_barriered([slice_recs[1]])           # resume: keymove alone
    assert tgt.rows == expected

    # (b) keymove RAN but its cursor was lost -> resume replays [keymove] on the
    #     already-moved state (the dangerous "between apply and persist" window).
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(table))
    rep.apply(list(preload))
    rep.apply_barriered(list(slice_recs))          # both segments run
    rep.apply_barriered([slice_recs[1]])           # keymove replays alone
    assert tgt.rows == expected


def test_is_keymove_classifies_only_pk_changing_updates():
    rep = Replicat(_FakeTarget(), _schema(_t3("T")))
    assert rep._is_keymove(_keymove(2, 1, V="m")) is True
    assert rep._is_keymove(_ins(1, V="a", BODY="b")) is False         # insert
    assert rep._is_keymove(_partial(1, V="v")) is False               # partial update
    assert rep._is_keymove(_rec(DELETE, "T", 1)) is False             # delete
    # before_key equal to the new key is NOT a move.
    same = ChangeRecord(op=UPDATE, schema="HR", table="T", key={"ID": 1},
                        after={"ID": 1, "V": "b"}, before_key={"ID": 1})
    assert rep._is_keymove(same) is False
    # Unknown table -> never a keymove (apply warns-and-skips it).
    assert rep._is_keymove(ChangeRecord(
        op=UPDATE, schema="HR", table="NOSUCH", key={"ID": 2},
        after={"ID": 2}, before_key={"ID": 1})) is False


def test_apply_barriered_flushes_cursor_around_each_keymove():
    # The engine passes on_flush to persist the apply cursor; it must fire just
    # before and just after each keymove (isolating it), and once at the end of a
    # trailing non-keymove batch. Slice = [insert, keymove, insert, insert].
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t3("T")))
    recs = [_ins(1, V="a", BODY="x"), _keymove(2, 1, V="m"),
            _ins(3, V="c", BODY="z"), _ins(4, V="d", BODY="w")]
    flushes = []
    applied, warnings = rep.apply_barriered(recs, on_flush=flushes.append)
    # Cumulative consumed at each flush: after the leading insert (1), after the
    # keymove alone (2), after the trailing two-insert batch (4).
    assert flushes == [1, 2, 4]
    assert applied == 4 and warnings == []
    assert tgt.rows[("t", (2,))]["v"] == "m"
    assert ("t", (1,)) not in tgt.rows


def test_apply_barriered_without_keymove_flushes_once():
    # A keymove-free slice keeps today's per-slice behaviour: one flush at the end,
    # same (applied, warnings) as a plain apply.
    tgt = _FakeTarget()
    rep = Replicat(tgt, _schema(_t3("T")))
    recs = [_ins(1, V="a", BODY="x"), _ins(2, V="b", BODY="y"), _partial(1, V="a2")]
    flushes = []
    applied, warnings = rep.apply_barriered(recs, on_flush=flushes.append)
    assert flushes == [3]                       # single flush past the whole slice
    assert applied == 3 and warnings == []


def test_apply_barriered_matches_apply_result_for_keymove_free_slice():
    recs = [_rec(DELETE, "T", 7), _rec(INSERT, "T", 7, "new")]
    a = _FakeTarget()
    Replicat(a, _schema(_t("T"))).apply(list(recs))
    b = _FakeTarget()
    Replicat(b, _schema(_t("T"))).apply_barriered(list(recs))
    assert a.rows == b.rows and a.calls == b.calls


# --- K2: source_pos codec (round 4) -------------------------------------------


def test_change_record_source_pos_roundtrips():
    r = ChangeRecord(op=INSERT, schema="S", table="T", key={"id": 1},
                     after={"id": 1}, source_pos=(9 << 48) | 4242)
    line = r.to_json()
    assert '"source_pos"' in line
    assert ChangeRecord.from_json(line).source_pos == (9 << 48) | 4242


def test_change_record_without_source_pos_keeps_wire_format():
    r = ChangeRecord(op=INSERT, schema="S", table="T", key={"id": 5}, after={"id": 5})
    line = r.to_json()
    assert "source_pos" not in line
    assert ChangeRecord.from_json(line).source_pos is None


def test_change_record_old_format_line_parses_with_none_source_pos():
    old = ('{"op":"U","schema":"S","table":"T","key":{"id":5},'
           '"after":{"id":5,"v":"a"},"scn":0,"commit_ts":""}')
    assert ChangeRecord.from_json(old).source_pos is None
