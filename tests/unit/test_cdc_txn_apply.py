"""P2 slice 3 — per-source-transaction atomic apply (FK-ordering / exactly-once).

Hermetic. A source transaction's records carry a shared ``txn_id`` (log-based
capture assigns it); the replicat regroups them and, on a target that proves
multi-statement transactional atomicity, applies each **keymove-free** source
transaction inside ONE target BEGIN/COMMIT. That fixes intra-transaction
FK-ordering and gives all-or-nothing per-txn apply, while keymove-bearing
transactions, untagged/legacy records, and txn-incapable targets keep the
per-record keymove barrier fully intact.

The fake ``_AtomicTarget`` models a real driver: writes inside a transaction are
staged and the (deferrable) FK is verified at COMMIT; writes outside a transaction
self-commit and the FK is checked IMMEDIATELY — so the same fake demonstrates both
the atomic win and the per-record failure it fixes.
"""
from __future__ import annotations

import json

import pytest

from any2heliosdb.cdc.deadletter import DeadLetter
from any2heliosdb.cdc.engine import (
    _read_txn_aligned,
    _registry_path,
    _segment_for_apply,
    _trail_dir,
    _txn_apply_enabled,
    run_replicat,
)
from any2heliosdb.cdc.registry import CdcRegistry
from any2heliosdb.cdc.replicat import Replicat
from any2heliosdb.cdc.trail import Trail
from any2heliosdb.config.model import CdcConfig, Options, ProjectConfig, SourceConfig, TargetConfig
from any2heliosdb.constants import Edition, SourceDialect, TargetDriverKind
from any2heliosdb.core.catalog_model import Column, DataType, PrimaryKey, Schema, Table
from any2heliosdb.core.change_record import DELETE, INSERT, UPDATE, ChangeRecord
from any2heliosdb.target.base import CapabilityMatrix


# --- schema -------------------------------------------------------------------


def _parent():
    return Table(name="PARENT", schema="hr",
                 columns=[Column("ID", DataType.decimal(10, 0)), Column("V", DataType.varchar(10))],
                 primary_key=PrimaryKey(columns=["ID"]))


def _child():
    return Table(name="CHILD", schema="hr",
                 columns=[Column("ID", DataType.decimal(10, 0)),
                          Column("FK", DataType.decimal(10, 0)),
                          Column("V", DataType.varchar(10))],
                 primary_key=PrimaryKey(columns=["ID"]))


def _one_table():
    return Table(name="T", schema="hr",
                 columns=[Column("ID", DataType.decimal(10, 0)), Column("V", DataType.varchar(10)),
                          Column("BODY", DataType.varchar(4000))],
                 primary_key=PrimaryKey(columns=["ID"]))


class _FakeAdapter:
    def __init__(self, tables):
        self._tables = tables

    def connect(self):
        pass

    def introspect_schema(self, schema):
        return Schema(name="hr", tables=list(self._tables))

    def close(self):
        pass


class _FkViolation(Exception):
    pass


class _AtomicTarget:
    """Atomic multi-statement transaction with an FK constraint.

    Two FK-timing models, both atomic (a rollback undoes every staged write):

    * default (``immediate_fk=False``) — a DEFERRABLE INITIALLY DEFERRED FK: inside
      begin()..commit() writes stage into a working copy and the FK is verified only
      at COMMIT; outside a transaction each write self-commits and the FK is checked
      immediately.
    * ``immediate_fk=True`` — an IMMEDIATE FK: the constraint is checked after EVERY
      statement, even inside a transaction (each apply-seam call re-verifies the
      staged working copy). This is the target on which table-bucketed apply (the D1
      bug) FK-violates mid-transaction while strict source-order apply does not.

    ``fks`` maps ``(child_table, fk_col) -> parent_table`` (lower-cased target names,
    as the replicat emits). ``fail_once_on`` optionally raises a transient error the
    FIRST time a record with that (table, key) is applied, to test whole-txn rollback
    + retry.
    """

    def __init__(self, fks=None, multi_txn=True, fail_once_on=None, poison_value=None,
                 immediate_fk=False):
        self.rows = {}
        self._txn = None
        self.fks = fks or {}
        self.multi_txn = multi_txn
        self.immediate_fk = immediate_fk
        self.begins = 0
        self.commits = 0
        self.rollbacks = 0
        self.calls = []
        self._fail_once_on = fail_once_on          # (table, keytuple) -> raise once
        self._failed_once = set()
        self._poison_value = poison_value          # a 'v' value the target never accepts

    def connect(self):
        pass

    def close(self):
        pass

    def ping(self):
        return None

    def probe_capabilities(self):
        return CapabilityMatrix(edition=Edition.FULL, server_version="14.0 (HeliosDB)",
                                multi_statement_txn=self.multi_txn)

    # --- txn lifecycle ---
    def begin(self):
        self.begins += 1
        self.calls.append("begin")
        self._txn = {k: dict(v) for k, v in self.rows.items()}

    def commit(self):
        self.commits += 1
        self.calls.append("commit")
        self._check_fk(self._txn)
        self.rows = self._txn
        self._txn = None

    def rollback(self):
        self.rollbacks += 1
        self.calls.append("rollback")
        self._txn = None

    def _check_fk(self, store):
        for (tbl, _key), row in store.items():
            for (ctbl, fkcol), ptbl in self.fks.items():
                if tbl == ctbl and row.get(fkcol) is not None:
                    if (ptbl, (row[fkcol],)) not in store:
                        raise _FkViolation(
                            "FK {}.{}={} -> missing {}".format(tbl, fkcol, row[fkcol], ptbl))

    def _guard(self, tt, key, row):
        if self._poison_value is not None and row.get("v") == self._poison_value:
            raise ValueError("target rejects poison value")
        if self._fail_once_on == (tt, key) and (tt, key) not in self._failed_once:
            self._failed_once.add((tt, key))
            raise ValueError("transient failure on {} {}".format(tt, key))

    def _op(self, mutate):
        if self._txn is not None:
            mutate(self._txn)  # inside a txn
            if self.immediate_fk:
                self._check_fk(self._txn)  # IMMEDIATE FK: checked per statement
        else:
            work = {k: dict(v) for k, v in self.rows.items()}
            mutate(work)
            self._check_fk(work)  # outside a txn: immediate FK
            self.rows = work

    def upsert(self, tt, key_cols, columns, rows):
        rows = list(rows)
        self.calls.append(("upsert", tt, len(rows)))

        def mutate(store):
            for r in rows:
                prov = dict(zip(columns, r))
                key = tuple(prov[k] for k in key_cols)
                self._guard(tt, key, prov)
                merged = dict(store.get((tt, key), {}))
                merged.update(prov)
                store[(tt, key)] = merged
        self._op(mutate)
        return len(rows)

    def update_columns(self, tt, key_cols, set_cols, rows):
        rows = list(rows)
        self.calls.append(("update", tt, len(rows)))
        nset = len(set_cols)
        matched = [0]

        def mutate(store):
            for r in rows:
                setvals, keyvals = r[:nset], r[nset:]
                key = tuple(keyvals)
                existing = store.get((tt, key))
                if existing is None:
                    continue
                existing = dict(existing)
                existing.update(dict(zip(set_cols, setvals)))
                newkey = tuple(existing[k] for k in key_cols)
                if newkey != key:
                    store.pop((tt, key), None)
                store[(tt, newkey)] = existing
                matched[0] += 1
        self._op(mutate)
        return matched[0]

    def delete_keys(self, tt, key_cols, keys):
        keys = list(keys)
        self.calls.append(("delete", tt, len(keys)))

        def mutate(store):
            for k in keys:
                store.pop((tt, tuple(k)), None)
        self._op(mutate)
        return len(keys)


def _cfg(tmp_path, tables, apply_batch=0, txn_apply="auto", poison_retries=3):
    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.POSTGRESQL, host="h", port=5432,
                            database="hr", schema="hr", user="u", password="p"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG),
        options=Options(output_dir=str(tmp_path)),
        cdc=CdcConfig(apply_batch=apply_batch, txn_apply=txn_apply,
                      poison_retries=poison_retries))


def _wire(monkeypatch, adapter, target):
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: adapter)
    monkeypatch.setattr(store, "build_target_driver", lambda cfg: target)


def _terminate(records):
    """Stamp ``txn_end=True`` on the LAST record of each maximal contiguous
    same-``txn_id`` run, mirroring what real capture writes to the durable trail
    (PostgreSQL COMMIT / MySQL XID). Untagged records (``txn_id`` None) are left
    alone. Returns the list for chaining. Tests that specifically exercise the
    incomplete-tail HOLD build their trailing txn WITHOUT this terminator."""
    n = len(records)
    for i, r in enumerate(records):
        if r.txn_id is None:
            continue
        if i + 1 == n or records[i + 1].txn_id != r.txn_id:
            r.txn_end = True
    return records


def _setup(cfg, name, tables, records, terminate=True):
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "hr", [t.name for t in tables])
    reg.close()
    if terminate:
        _terminate(records)
    Trail(_trail_dir(cfg, name)).append(records)


def _c(op, table, idv, txn_id, pos, **cols):
    after = {} if op == DELETE else {"ID": idv, **cols}
    return ChangeRecord(op=op, schema="hr", table=table, key={"ID": idv},
                        after=after, source_pos=pos, txn_id=txn_id)


# --- wire format --------------------------------------------------------------


def test_txn_id_roundtrips_and_is_optional():
    r = ChangeRecord(op=INSERT, schema="s", table="t", key={"id": 1}, after={"id": 1}, txn_id=700)
    line = r.to_json()
    assert '"txn_id":700' in line
    assert ChangeRecord.from_json(line).txn_id == 700
    # Absent when None -> byte-identical to a pre-slice-3 record.
    r2 = ChangeRecord(op=INSERT, schema="s", table="t", key={"id": 1}, after={"id": 1})
    assert "txn_id" not in r2.to_json()
    assert ChangeRecord.from_json(r2.to_json()).txn_id is None
    # A legacy line with no txn_id parses to None.
    legacy = ('{"op":"U","schema":"S","table":"T","key":{"id":5},'
              '"after":{"id":5,"v":"a"},"scn":0,"commit_ts":""}')
    assert ChangeRecord.from_json(legacy).txn_id is None


# --- gate ---------------------------------------------------------------------


def test_txn_apply_enabled_gate():
    cfg = ProjectConfig(cdc=CdcConfig(txn_apply="auto"))
    cap_yes = CapabilityMatrix(multi_statement_txn=True)
    cap_no = CapabilityMatrix(multi_statement_txn=False)
    assert _txn_apply_enabled(cfg, cap_yes) is True
    assert _txn_apply_enabled(cfg, cap_no) is False          # target can't do txns
    cfg.cdc.txn_apply = "on"
    assert _txn_apply_enabled(cfg, cap_yes) is True
    cfg.cdc.txn_apply = "off"
    assert _txn_apply_enabled(cfg, cap_yes) is False         # forced off
    assert _txn_apply_enabled(ProjectConfig(cdc=CdcConfig()), cap_yes) is True  # auto default


# --- segmentation -------------------------------------------------------------


def _rep(tables):
    return Replicat(_AtomicTarget(), Schema(name="hr", tables=list(tables)))


def test_segment_routes_keymove_free_txn_atomic_and_keymove_txn_barrier():
    rep = _rep([_one_table()])
    ins = lambda i, t: ChangeRecord(op=INSERT, schema="hr", table="T", key={"ID": i},  # noqa: E731
                                    after={"ID": i, "V": "a", "BODY": "b"}, txn_id=t)
    km = lambda new, old, t: ChangeRecord(op=UPDATE, schema="hr", table="T",  # noqa: E731
                                          key={"ID": new}, after={"ID": new, "V": "m"},
                                          before_key={"ID": old}, txn_id=t)
    recs = [ins(1, 10), ins(2, 10),          # txn 10: keymove-free -> atomic
            ins(3, 11), km(5, 3, 11),        # txn 11: has a keymove -> barrier
            ChangeRecord(op=INSERT, schema="hr", table="T", key={"ID": 9},
                         after={"ID": 9, "V": "z", "BODY": "b"})]  # untagged -> barrier
    segs = list(_segment_for_apply(recs, rep, enabled=True))
    kinds = [(k, [r.key["ID"] for r in s]) for k, s in segs]
    assert kinds == [("atomic", [1, 2]), ("barrier", [3, 5, 9])]
    # Disabled -> one barrier segment (today's behaviour).
    assert [k for k, _ in _segment_for_apply(recs, rep, enabled=False)] == ["barrier"]


# --- FK-ordering (the headline: keymove-free cross-table txn) ------------------


def test_fk_ordering_keymove_free_txn_applies_atomically(tmp_path, monkeypatch):
    # A source txn inserts a CHILD (fk=1) then its PARENT (1). Arrival order is
    # child-first, so a per-record self-committing apply hits an immediate FK
    # violation; per-txn atomic apply commits both together (deferred FK) -> no
    # intermediate violation.
    tables = [_parent(), _child()]
    cfg = _cfg(tmp_path, tables)
    name = "fk1"
    recs = [
        _c(INSERT, "CHILD", 100, txn_id=7, pos=1, FK=1, V="c"),
        _c(INSERT, "PARENT", 1, txn_id=7, pos=2, V="p"),
    ]
    _setup(cfg, name, tables, recs)
    target = _AtomicTarget(fks={("child", "fk"): "parent"})
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["applied"] == 2 and res["cursor"] == 2
    assert target.rows[("parent", (1,))]["v"] == "p"
    assert target.rows[("child", (100,))]["fk"] == 1
    assert target.begins == 1 and target.commits == 1 and target.rollbacks == 0


def test_fk_ordering_per_record_path_would_violate(tmp_path, monkeypatch):
    # Same records, txn_apply OFF -> the per-record path self-commits the child
    # insert first and the immediate FK rejects it (this is the residual the atomic
    # path fixes). Proves the atomic win is real, not vacuous.
    tables = [_parent(), _child()]
    # poison off so the FK violation fails closed (raises) rather than being
    # dead-lettered — the point is to show the per-record path hits the violation.
    cfg = _cfg(tmp_path, tables, txn_apply="off", poison_retries=0)
    name = "fk2"
    recs = [
        _c(INSERT, "CHILD", 100, txn_id=7, pos=1, FK=1, V="c"),
        _c(INSERT, "PARENT", 1, txn_id=7, pos=2, V="p"),
    ]
    _setup(cfg, name, tables, recs)
    target = _AtomicTarget(fks={("child", "fk"): "parent"})
    _wire(monkeypatch, _FakeAdapter(tables), target)
    with pytest.raises(_FkViolation):
        run_replicat(cfg, name, reconcile_deletes=False)
    assert target.begins == 0  # never entered the atomic path


def test_fk_ordering_split_across_two_txns_orders_by_commit(tmp_path, monkeypatch):
    # The parent and child in SEPARATE source txns (parent first) still apply in
    # commit order: two atomic groups, each its own target transaction.
    tables = [_parent(), _child()]
    cfg = _cfg(tmp_path, tables)
    name = "fk3"
    recs = [
        _c(INSERT, "PARENT", 1, txn_id=7, pos=1, V="p"),
        _c(INSERT, "CHILD", 100, txn_id=8, pos=2, FK=1, V="c"),
    ]
    _setup(cfg, name, tables, recs)
    target = _AtomicTarget(fks={("child", "fk"): "parent"})
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["applied"] == 2 and res["cursor"] == 2
    assert target.begins == 2 and target.commits == 2  # two source txns, two commits
    assert target.rows[("child", (100,))]["fk"] == 1


# --- atomic rollback + retry --------------------------------------------------


def test_atomic_txn_rolls_back_all_on_mid_failure_then_retry_succeeds(tmp_path, monkeypatch):
    # A 3-record txn whose 3rd record fails the FIRST attempt: the whole txn rolls
    # back (nothing persisted), then the retry succeeds and commits all 3. The
    # cursor advances only after the successful commit.
    tables = [_one_table()]
    cfg = _cfg(tmp_path, tables, poison_retries=3)
    name = "rb1"
    recs = [
        _c(INSERT, "T", 1, txn_id=9, pos=1, V="a", BODY="x"),
        _c(INSERT, "T", 2, txn_id=9, pos=2, V="b", BODY="y"),
        _c(INSERT, "T", 3, txn_id=9, pos=3, V="c", BODY="z"),
    ]
    _setup(cfg, name, tables, recs)
    cursors = []
    orig = CdcRegistry.set_apply_cursor
    monkeypatch.setattr(CdcRegistry, "set_apply_cursor",
                        lambda self, n, c: (cursors.append(c), orig(self, n, c))[1])
    # Fail once when record id=3 is first applied (a transient error mid-txn).
    target = _AtomicTarget(fail_once_on=("t", (3,)))
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["applied"] == 3 and res["cursor"] == 3
    # All three present (all-or-nothing then succeed), and a rollback DID happen.
    assert {k[1] for k in target.rows} == {(1,), (2,), (3,)}
    assert target.rollbacks == 1 and target.commits == 1
    # The cursor never advanced to a partial state — first persist is the full 3.
    assert cursors and cursors[0] == 3 and all(c == 3 for c in cursors)


# --- poison in txn: whole-txn dead-letter -------------------------------------


def test_poison_in_txn_dead_letters_whole_group(tmp_path, monkeypatch):
    # A keymove-free txn the target can never accept (a poison value) is retried and
    # then DEAD-LETTERED AS A UNIT (all its records), cursor advancing past it.
    tables = [_one_table()]
    cfg = _cfg(tmp_path, tables, poison_retries=2)
    name = "pz1"
    recs = [
        _c(INSERT, "T", 1, txn_id=5, pos=10, V="ok", BODY="x"),   # own txn, applies
        _c(INSERT, "T", 2, txn_id=6, pos=20, V="POISON", BODY="y"),  # poison txn
        _c(INSERT, "T", 3, txn_id=6, pos=30, V="alsobad", BODY="z"),  # same poison txn
        _c(INSERT, "T", 4, txn_id=7, pos=40, V="ok2", BODY="w"),  # own txn, applies
    ]
    _setup(cfg, name, tables, recs)
    target = _AtomicTarget(poison_value="POISON")
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    # The two good txns applied; the poison txn's BOTH records dead-lettered.
    assert target.rows[("t", (1,))]["v"] == "ok"
    assert target.rows[("t", (4,))]["v"] == "ok2"
    assert ("t", (2,)) not in target.rows and ("t", (3,)) not in target.rows
    assert res["cursor"] == 4
    dl = DeadLetter(_trail_dir(cfg, name))
    assert dl.count() == 2                       # the whole poison txn, as a unit
    poss = {e for e in dl.seen_source_positions()}
    assert json.dumps(20) in poss and json.dumps(30) in poss
    # Re-run is a clean no-op (cursor durably advanced, poison not re-parked).
    res2 = run_replicat(cfg, name, reconcile_deletes=False)
    assert res2["applied"] == 0 and res2["dead_lettered"] == 0


def test_poison_disabled_in_txn_fails_closed(tmp_path, monkeypatch):
    # poison_retries=0 -> a failing atomic txn rolls back and RAISES (cursor unmoved).
    tables = [_one_table()]
    cfg = _cfg(tmp_path, tables, poison_retries=0)
    name = "pz2"
    _setup(cfg, name, tables, [_c(INSERT, "T", 1, txn_id=5, pos=10, V="POISON", BODY="x")])
    target = _AtomicTarget(poison_value="POISON")
    _wire(monkeypatch, _FakeAdapter(tables), target)
    with pytest.raises(ValueError):
        run_replicat(cfg, name, reconcile_deletes=False)
    assert target.rollbacks >= 1
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name).apply_cursor == 0
    reg.close()


# --- capability fallback ------------------------------------------------------


def test_txn_incapable_target_uses_per_record_path(tmp_path, monkeypatch):
    # A target that fails the multi_statement_txn probe keeps the per-record barrier
    # path even for txn-tagged records: begin() is never called.
    tables = [_one_table()]
    cfg = _cfg(tmp_path, tables)
    name = "cap1"
    recs = [_c(INSERT, "T", 1, txn_id=5, pos=10, V="a", BODY="x"),
            _c(INSERT, "T", 2, txn_id=5, pos=20, V="b", BODY="y")]
    _setup(cfg, name, tables, recs)
    target = _AtomicTarget(multi_txn=False)
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["applied"] == 2 and res["cursor"] == 2
    assert target.begins == 0  # never used a target transaction
    assert target.rows[("t", (1,))]["v"] == "a"


# --- keymove txn stays on the barrier (refutation scenarios preserved) --------


def test_keymove_bearing_txn_routed_to_barrier_still_converges(tmp_path, monkeypatch):
    # A source txn that CONTAINS a keymove is applied via the barrier even when
    # atomic apply is enabled — so the keymove's replay-convergence + impostor-safety
    # (the barrier's refutation suite) are preserved untouched. The keymove moves
    # 1->2 and re-points nothing; final state has the moved row, old key gone.
    tables = [_one_table()]
    cfg = _cfg(tmp_path, tables)
    name = "km1"
    recs = [
        _c(INSERT, "T", 1, txn_id=3, pos=1, V="pre", BODY="BIG"),   # its own txn (atomic)
        ChangeRecord(op=UPDATE, schema="hr", table="T", key={"ID": 2},
                     after={"ID": 2, "V": "moved"}, before_key={"ID": 1},
                     source_pos=2, txn_id=4),                        # keymove txn (barrier)
    ]
    _setup(cfg, name, tables, recs)
    target = _AtomicTarget()
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["applied"] == 2 and res["cursor"] == 2
    assert target.rows[("t", (2,))] == {"id": 2, "v": "moved", "body": "BIG"}
    assert ("t", (1,)) not in target.rows
    # The insert txn used the atomic path (one begin); the keymove used the barrier
    # (no begin). Exactly one target transaction overall.
    assert target.begins == 1


# --- apply_batch alignment ----------------------------------------------------


def test_read_txn_aligned_does_not_split_a_txn_at_the_batch_edge(tmp_path):
    # apply_batch=2 but txn 100 spans lines 2..4: the aligned read extends to the
    # txn boundary so the whole txn lands in one chunk (never split for atomic apply).
    trail = Trail(_trail_dir_for(tmp_path))
    recs = [
        _c(INSERT, "T", 1, txn_id=99, pos=1, V="a", BODY="x"),   # line 1: txn 99
        _c(INSERT, "T", 2, txn_id=100, pos=2, V="b", BODY="y"),  # line 2: txn 100
        _c(INSERT, "T", 3, txn_id=100, pos=3, V="c", BODY="z"),  # line 3: txn 100
        _c(INSERT, "T", 4, txn_id=100, pos=4, V="d", BODY="w"),  # line 4: txn 100
        _c(INSERT, "T", 5, txn_id=101, pos=5, V="e", BODY="v"),  # line 5: txn 101
    ]
    trail.append(_terminate(recs))
    out, cur = _read_txn_aligned(trail, 0, limit=2, enabled=True)
    # A plain read(limit=2) would stop at line 2 mid-txn-100; alignment extends to
    # the end of txn 100 (line 4).
    assert [r.key["ID"] for r in out] == [1, 2, 3, 4] and cur == 4
    # The next chunk begins cleanly at txn 101.
    out2, cur2 = _read_txn_aligned(trail, cur, limit=2, enabled=True)
    assert [r.key["ID"] for r in out2] == [5] and cur2 == 5


def test_read_txn_aligned_single_txn_larger_than_batch_applies_whole(tmp_path):
    trail = Trail(_trail_dir_for(tmp_path, "big"))
    recs = [_c(INSERT, "T", i, txn_id=42, pos=i, V="v", BODY="b") for i in range(1, 6)]
    trail.append(_terminate(recs))
    out, cur = _read_txn_aligned(trail, 0, limit=2, enabled=True)
    assert len(out) == 5 and cur == 5          # one giant txn read whole
    # Disabled -> honours the raw line limit (no whole-txn extension).
    out2, cur2 = _read_txn_aligned(trail, 0, limit=2, enabled=False)
    assert len(out2) == 2 and cur2 == 2


def test_txn_straddling_batch_applies_atomically_end_to_end(tmp_path, monkeypatch):
    # End-to-end: a cross-table FK txn straddling apply_batch=1 is NOT split, so it
    # still commits atomically (no intermediate FK violation) despite the small batch.
    tables = [_parent(), _child()]
    cfg = _cfg(tmp_path, tables, apply_batch=1)
    name = "align1"
    recs = [
        _c(INSERT, "CHILD", 100, txn_id=7, pos=1, FK=1, V="c"),
        _c(INSERT, "PARENT", 1, txn_id=7, pos=2, V="p"),
    ]
    _setup(cfg, name, tables, recs)
    target = _AtomicTarget(fks={("child", "fk"): "parent"})
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["applied"] == 2 and res["cursor"] == 2
    assert target.begins == 1 and target.commits == 1  # one atomic txn, not split


# --- mixed old/new trail; old-only byte-identical -----------------------------


def test_mixed_legacy_and_tagged_trail_applies_correctly(tmp_path, monkeypatch):
    # An old prefix (no txn_id) followed by a new suffix (tagged) after an upgrade:
    # the legacy records apply via the barrier, the tagged keymove-free txn atomically.
    tables = [_one_table()]
    cfg = _cfg(tmp_path, tables)
    name = "mix1"
    legacy = ChangeRecord(op=INSERT, schema="hr", table="T", key={"ID": 1},
                          after={"ID": 1, "V": "leg", "BODY": "x"})  # no txn_id, no source_pos
    tagged = [_c(INSERT, "T", 2, txn_id=8, pos=2, V="new", BODY="y"),
              _c(INSERT, "T", 3, txn_id=8, pos=3, V="new3", BODY="z")]
    _setup(cfg, name, tables, [legacy] + tagged)
    target = _AtomicTarget()
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["applied"] == 3 and res["cursor"] == 3
    assert target.rows[("t", (1,))]["v"] == "leg"
    assert target.rows[("t", (2,))]["v"] == "new"
    assert target.rows[("t", (3,))]["v"] == "new3"
    assert target.begins == 1  # only the tagged txn was atomic; legacy used barrier


def test_old_only_trail_never_uses_atomic_path(tmp_path, monkeypatch):
    # A pre-upgrade trail (all untagged) applies exactly as before: barrier only,
    # no target transaction — byte-identical behaviour even on an atomic-capable target.
    tables = [_one_table()]
    cfg = _cfg(tmp_path, tables)
    name = "old1"
    recs = [ChangeRecord(op=INSERT, schema="hr", table="T", key={"ID": i},
                         after={"ID": i, "V": "v", "BODY": "b"}) for i in (1, 2, 3)]
    _setup(cfg, name, tables, recs)
    target = _AtomicTarget()
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["applied"] == 3 and res["cursor"] == 3
    assert target.begins == 0


# --- apply_txn poison ping (sick target) --------------------------------------


class _SickAtomicTarget(_AtomicTarget):
    def ping(self):
        raise ConnectionError("target unreachable")

    def commit(self):
        raise ConnectionError("target unreachable")


def test_atomic_txn_sick_target_raises_cursor_unmoved(tmp_path, monkeypatch):
    # A dead target (commit + ping both fail) during atomic apply must RAISE (outage,
    # not poison) and dead-letter NOTHING, leaving the cursor at 0.
    tables = [_one_table()]
    cfg = _cfg(tmp_path, tables, poison_retries=3)
    name = "sick1"
    _setup(cfg, name, tables, [_c(INSERT, "T", i, txn_id=5, pos=i, V="ok", BODY="x")
                               for i in range(1, 4)])
    target = _SickAtomicTarget()
    _wire(monkeypatch, _FakeAdapter(tables), target)
    with pytest.raises(Exception):
        run_replicat(cfg, name, reconcile_deletes=False)
    assert DeadLetter(_trail_dir(cfg, name)).count() == 0
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name).apply_cursor == 0
    reg.close()


# --- D1: strict cross-table order inside the atomic txn -----------------------


def _scenario_a():
    # The headline keymove-free cross-table transaction: insert parent, insert child
    # (fk->parent), delete child, delete parent. Net-empty, but the intermediate
    # ordering is load-bearing on an FK-enforcing target.
    return [
        _c(INSERT, "PARENT", 1, txn_id=7, pos=1, V="p"),
        _c(INSERT, "CHILD", 10, txn_id=7, pos=2, FK=1, V="c"),
        _c(DELETE, "CHILD", 10, txn_id=7, pos=3),
        _c(DELETE, "PARENT", 1, txn_id=7, pos=4),
    ]


def test_apply_ordered_respects_source_order_where_bucketed_apply_violates():
    # Unit-level D1 proof against an IMMEDIATE-check FK target. Table-bucketed apply()
    # groups PARENT ([ins,del]) before CHILD ([ins,del]), so it deletes the parent
    # before the child insert -> FK violation. apply_ordered() walks strict source
    # order (ins parent, ins child, del child, del parent) -> no intermediate violation.
    schema = Schema(name="hr", tables=[_parent(), _child()])
    fks = {("child", "fk"): "parent"}
    with pytest.raises(_FkViolation):
        Replicat(_AtomicTarget(fks=fks, immediate_fk=True), schema).apply(_scenario_a())
    a, w = Replicat(_AtomicTarget(fks=fks, immediate_fk=True), schema).apply_ordered(_scenario_a())
    assert a == 4 and w == []


def test_apply_ordered_coalesces_contiguous_same_table_kind_runs():
    # Only a maximal contiguous (table, kind) run coalesces: two PARENT inserts batch
    # into one upsert, the CHILD insert is its own call, the trailing PARENT delete is
    # its own call — all emitted in strict source order.
    target = _AtomicTarget()
    rep = Replicat(target, Schema(name="hr", tables=[_parent(), _child()]))
    recs = [
        _c(INSERT, "PARENT", 1, txn_id=1, pos=1, V="p1"),
        _c(INSERT, "PARENT", 2, txn_id=1, pos=2, V="p2"),   # coalesces with the prior
        _c(INSERT, "CHILD", 10, txn_id=1, pos=3, FK=1, V="c"),
        _c(DELETE, "PARENT", 1, txn_id=1, pos=4),
    ]
    a, w = rep.apply_ordered(recs)
    assert a == 4 and w == []
    assert target.calls == [
        ("upsert", "parent", 2), ("upsert", "child", 1), ("delete", "parent", 1)]


def test_apply_ordered_rejects_a_keymove():
    # apply_ordered is the atomic (keymove-free) primitive; a keymove reaching it is a
    # broken engine invariant, so it fails closed rather than mis-applying out of order.
    rep = _rep([_one_table()])
    km = ChangeRecord(op=UPDATE, schema="hr", table="T", key={"ID": 2},
                      after={"ID": 2, "V": "m"}, before_key={"ID": 1}, txn_id=1)
    with pytest.raises(Exception):
        rep.apply_ordered([km])


def test_scenario_a_immediate_fk_txn_applies_atomically_and_replays_once(tmp_path, monkeypatch):
    # End-to-end D1: scenario A as ONE source txn on an IMMEDIATE-check FK target
    # applies atomically in strict order with NO intermediate FK violation, and a
    # replay of the same trail (cursor reset -> crash-before-persist) converges to the
    # identical state exactly-once.
    tables = [_parent(), _child()]
    cfg = _cfg(tmp_path, tables)
    name = "sa1"
    _setup(cfg, name, tables, _scenario_a())
    target = _AtomicTarget(fks={("child", "fk"): "parent"}, immediate_fk=True)
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["applied"] == 4 and res["cursor"] == 4
    assert target.begins == 1 and target.commits == 1 and target.rollbacks == 0
    assert target.rows == {}                         # net-empty, no mid-txn violation
    # Replay: reset the apply cursor and re-run -> re-applied idempotently, same state.
    reg = CdcRegistry(_registry_path(cfg))
    reg.set_apply_cursor(name, 0)
    reg.close()
    res2 = run_replicat(cfg, name, reconcile_deletes=False)
    assert res2["applied"] == 4 and res2["cursor"] == 4
    assert target.rows == {} and target.rollbacks == 0


# --- D2: a partial/uncertified trailing txn is HELD, never atomically committed ---


def test_incomplete_trailing_txn_is_held_then_applied_atomically_once(tmp_path, monkeypatch):
    # Simulate a buffered-writer flush / pre-fsync crash: the leading record of a
    # child-before-parent txn is durable but its terminator (the parent) is NOT.
    # Atomically committing the child alone would FK-violate at commit (deferred) and,
    # under poison, dead-letter and DIVERGE. The uncertified tail must be HELD instead
    # (cursor before it, nothing applied); once the terminated remainder is durable the
    # whole txn applies atomically exactly once. Closes the review's divergence path.
    tables = [_parent(), _child()]
    cfg = _cfg(tmp_path, tables)
    name = "hold1"
    child = _c(INSERT, "CHILD", 10, txn_id=7, pos=1, FK=1, V="c")
    _setup(cfg, name, tables, [child], terminate=False)     # NO txn_end -> incomplete
    target = _AtomicTarget(fks={("child", "fk"): "parent"})
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    # Held: nothing applied / begun / committed / dead-lettered; cursor stays before it.
    assert res["applied"] == 0 and res["cursor"] == 0
    assert target.begins == 0 and target.commits == 0 and target.rollbacks == 0
    assert ("child", (10,)) not in target.rows
    assert DeadLetter(_trail_dir(cfg, name)).count() == 0
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name).apply_cursor == 0
    reg.close()
    # The recovered terminator (the txn's remaining record, the parent) lands durably.
    parent = _c(INSERT, "PARENT", 1, txn_id=7, pos=2, V="p")
    Trail(_trail_dir(cfg, name)).append(_terminate([parent]))
    res2 = run_replicat(cfg, name, reconcile_deletes=False)
    assert res2["applied"] == 2 and res2["cursor"] == 2
    assert target.begins == 1 and target.commits == 1 and target.rollbacks == 0
    assert target.rows[("parent", (1,))]["v"] == "p"
    assert target.rows[("child", (10,))]["fk"] == 1
    # Applied exactly once: a third run is a clean no-op.
    res3 = run_replicat(cfg, name, reconcile_deletes=False)
    assert res3["applied"] == 0 and res3["cursor"] == 2


def test_incomplete_tail_held_after_a_complete_prefix_txn(tmp_path, monkeypatch):
    # A complete txn 5 followed by an unterminated txn 6 (torn tail): txn 5 is
    # certified (terminated) and applies atomically; txn 6 is held, so its records
    # never reach the target until it is completed.
    tables = [_one_table()]
    cfg = _cfg(tmp_path, tables, apply_batch=2)   # also exercise the bounded overshoot
    name = "hold2"
    complete = _c(INSERT, "T", 1, txn_id=5, pos=1, V="a", BODY="x")
    partial = [_c(INSERT, "T", 2, txn_id=6, pos=2, V="b", BODY="y"),
               _c(INSERT, "T", 3, txn_id=6, pos=3, V="c", BODY="z")]
    _terminate([complete])                        # txn 5 terminated
    _setup(cfg, name, tables, [complete] + partial, terminate=False)  # txn 6 NOT
    target = _AtomicTarget()
    _wire(monkeypatch, _FakeAdapter(tables), target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["applied"] == 1 and res["cursor"] == 1     # only txn 5 applied
    assert target.rows[("t", (1,))]["v"] == "a"
    assert ("t", (2,)) not in target.rows and ("t", (3,)) not in target.rows
    assert target.begins == 1
    # Complete txn 6 with its terminator; now the whole txn applies atomically.
    Trail(_trail_dir(cfg, name)).append(_terminate(
        [_c(INSERT, "T", 4, txn_id=6, pos=4, V="d", BODY="w")]))
    res2 = run_replicat(cfg, name, reconcile_deletes=False)
    assert res2["applied"] == 3 and res2["cursor"] == 4
    assert {k[1] for k in target.rows} == {(1,), (2,), (3,), (4,)}


def test_txn_end_terminator_roundtrips_and_is_optional():
    # Additive + backward compatible: txn_end serializes only when True; absent -> False.
    r = ChangeRecord(op=INSERT, schema="s", table="t", key={"id": 1}, after={"id": 1},
                     txn_id=9, txn_end=True)
    line = r.to_json()
    assert '"txn_end":true' in line
    assert ChangeRecord.from_json(line).txn_end is True
    r2 = ChangeRecord(op=INSERT, schema="s", table="t", key={"id": 1}, after={"id": 1}, txn_id=9)
    assert "txn_end" not in r2.to_json()
    assert ChangeRecord.from_json(r2.to_json()).txn_end is False
    # A legacy line with no txn_end parses to False.
    legacy = ('{"op":"U","schema":"S","table":"T","key":{"id":5},'
              '"after":{"id":5,"v":"a"},"scn":0,"commit_ts":""}')
    assert ChangeRecord.from_json(legacy).txn_end is False


def _trail_dir_for(tmp_path, sub="t"):
    import os
    return os.path.join(str(tmp_path), "trail", sub)
