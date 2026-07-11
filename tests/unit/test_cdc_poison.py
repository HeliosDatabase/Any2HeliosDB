"""H4 — poison-record policy for the replicat.

One bad record must not wedge replication forever: after ``poison_retries``
attempts the replicat moves it to ``dead_letter.jsonl``, advances the cursor past
it, and counts it. Keymoves are NEVER dead-lettered (skipping one diverges key
state) — a keymove failure fails closed. Replays never double-dead-letter (dedup
by ``source_pos``).
"""
from __future__ import annotations

import json
import os

import pytest

from any2heliosdb.cdc.deadletter import DeadLetter
from any2heliosdb.cdc.engine import _registry_path, _trail_dir, run_replicat
from any2heliosdb.cdc.registry import CdcRegistry
from any2heliosdb.cdc.trail import Trail
from any2heliosdb.config.model import CdcConfig, Options, ProjectConfig, SourceConfig, TargetConfig
from any2heliosdb.constants import Edition, SourceDialect, TargetDriverKind
from any2heliosdb.core.catalog_model import Column, DataType, PrimaryKey, Schema, Table
from any2heliosdb.core.change_record import INSERT, UPDATE, ChangeRecord
from any2heliosdb.target.base import CapabilityMatrix


# --- DeadLetter unit ----------------------------------------------------------


def _rec(idv, pos=None, v="a"):
    return ChangeRecord(op=INSERT, schema="hr", table="t", key={"ID": idv},
                        after={"ID": idv, "V": v}, source_pos=pos)


def test_deadletter_append_count_and_dedup(tmp_path):
    dl = DeadLetter(str(tmp_path))
    assert dl.count() == 0 and dl.seen_source_positions() == set()
    dl.append(_rec(1, pos=100), "boom")
    dl.append(_rec(2, pos=[100, 1]), "boom2")
    assert dl.count() == 2
    seen = dl.seen_source_positions()
    assert json.dumps(100, separators=(",", ":")) in seen
    assert json.dumps([100, 1], separators=(",", ":")) in seen
    # A record without a source_pos is stored but not tracked for dedup.
    dl.append(_rec(3), "boom3")
    assert dl.count() == 3
    assert len(dl.seen_source_positions()) == 2


def test_deadletter_entry_carries_table_key_reason(tmp_path):
    dl = DeadLetter(str(tmp_path))
    dl.append(_rec(7, pos=42, v="x"), "ValueError: bad")
    with open(dl.path) as f:
        entry = json.loads(f.readline())
    assert entry["table"] == "t" and entry["reason"] == "ValueError: bad"
    assert entry["source_pos"] == 42 and entry["op"] == "I"


# --- engine-level poison policy -----------------------------------------------


def _table():
    return Table(name="t", schema="hr",
                 columns=[Column("ID", DataType.decimal(10, 0)), Column("V", DataType.varchar(10))],
                 primary_key=PrimaryKey(columns=["ID"]))


class _FakeAdapter:
    def connect(self):
        pass

    def introspect_schema(self, schema):
        return Schema(name="hr", tables=[_table()])

    def close(self):
        pass


class _PoisonTarget:
    """Merge-upsert target that REJECTS any row whose V == 'POISON' (models a
    value the target can never accept), and any UPDATE touching key 999."""

    def __init__(self):
        self.rows = {}
        self.upsert_attempts = 0

    def connect(self):
        pass

    def close(self):
        pass

    def ping(self):
        # A healthy target answers the liveness probe, so a rejected record is
        # genuinely poison (dead-letter it) rather than a transient outage.
        return None

    def probe_capabilities(self):
        return CapabilityMatrix(edition=Edition.FULL, server_version="14.0 (HeliosDB)")

    def upsert(self, tt, key_cols, columns, rows):
        rows = list(rows)
        self.upsert_attempts += 1
        n = 0
        for r in rows:
            prov = dict(zip(columns, r))
            if prov.get("v") == "POISON":
                raise ValueError("target rejects POISON value")
            key = tuple(prov[k] for k in key_cols)
            merged = dict(self.rows.get((tt, key), {}))
            merged.update(prov)
            self.rows[(tt, key)] = merged
            n += 1
        return n

    def update_columns(self, tt, key_cols, set_cols, rows):
        nset = len(set_cols)
        matched = 0
        for r in rows:
            setvals, keyvals = r[:nset], r[nset:]
            key = tuple(keyvals)
            if 999 in key or 999 in tuple(setvals):
                raise ValueError("target rejects keymove onto 999")
            existing = self.rows.get((tt, key))
            if existing is None:
                continue
            existing = dict(existing)
            existing.update(dict(zip(set_cols, setvals)))
            newkey = tuple(existing[k] for k in key_cols)
            if newkey != key:
                self.rows.pop((tt, key), None)
            self.rows[(tt, newkey)] = existing
            matched += 1
        return matched

    def delete_keys(self, tt, key_cols, keys):
        n = 0
        for k in keys:
            self.rows.pop((tt, tuple(k)), None)
            n += 1
        return n


def _cfg(tmp_path, poison_retries=3):
    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.POSTGRESQL, host="h", port=5432,
                            database="hr", schema="hr", user="u", password="p"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG),
        options=Options(output_dir=str(tmp_path)),
        cdc=CdcConfig(apply_batch=0, poison_retries=poison_retries))


def _wire(monkeypatch, target):
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())
    monkeypatch.setattr(store, "build_target_driver", lambda cfg: target)


def _setup(cfg, name, records):
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "hr", ["t"])
    reg.close()
    Trail(_trail_dir(cfg, name)).append(records)


def test_poison_record_dead_lettered_cursor_advances(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    name = "p1"
    _setup(cfg, name, [_rec(1, pos=10, v="ok"),
                       _rec(2, pos=20, v="POISON"),
                       _rec(3, pos=30, v="ok2")])
    target = _PoisonTarget()
    _wire(monkeypatch, target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    # The two good rows applied; the poison one parked, cursor advanced PAST it.
    assert target.rows[("t", (1,))]["v"] == "ok"
    assert target.rows[("t", (3,))]["v"] == "ok2"
    assert ("t", (2,)) not in target.rows
    assert res["cursor"] == 3 and res["dead_lettered"] == 1
    dl = DeadLetter(_trail_dir(cfg, name))
    assert dl.count() == 1
    # A loud warning names the dead-lettered record.
    assert any("dead-lettered" in w and "key=" in w for w in res["warnings"])
    # The durable cursor really advanced, so a re-run is a clean no-op.
    res2 = run_replicat(cfg, name, reconcile_deletes=False)
    assert res2["applied"] == 0 and res2["dead_lettered"] == 0


def test_poison_retries_honored(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, poison_retries=4)
    name = "p2"
    _setup(cfg, name, [_rec(1, pos=10, v="POISON")])
    target = _PoisonTarget()
    _wire(monkeypatch, target)
    run_replicat(cfg, name, reconcile_deletes=False)
    # The whole-batch attempt (1) + 4 per-record retries = 5 upsert attempts.
    assert target.upsert_attempts == 5


def test_poison_disabled_raises(tmp_path, monkeypatch):
    # poison_retries=0 -> a failing record raises exactly as before (fail closed).
    cfg = _cfg(tmp_path, poison_retries=0)
    name = "p3"
    _setup(cfg, name, [_rec(1, pos=10, v="POISON")])
    _wire(monkeypatch, _PoisonTarget())
    with pytest.raises(ValueError):
        run_replicat(cfg, name, reconcile_deletes=False)


def test_keymove_never_dead_lettered_fails_closed(tmp_path, monkeypatch):
    # A keymove the target rejects must FAIL CLOSED (raise), never be dead-lettered
    # — skipping it would diverge key state.
    cfg = _cfg(tmp_path, poison_retries=3)
    name = "p4"
    km = ChangeRecord(op=UPDATE, schema="hr", table="t", key={"ID": 999},
                      after={"ID": 999, "V": "moved"}, before_key={"ID": 1}, source_pos=20)
    _setup(cfg, name, [_rec(1, pos=10, v="seed"), km])
    target = _PoisonTarget()
    _wire(monkeypatch, target)
    with pytest.raises(ValueError):
        run_replicat(cfg, name, reconcile_deletes=False)
    # The keymove was NOT parked in the dead-letter file.
    assert DeadLetter(_trail_dir(cfg, name)).count() == 0


def test_replay_does_not_double_dead_letter(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    name = "p5"
    _setup(cfg, name, [_rec(1, pos=10, v="ok"), _rec(2, pos=20, v="POISON")])
    target = _PoisonTarget()
    _wire(monkeypatch, target)
    run_replicat(cfg, name, reconcile_deletes=False)
    assert DeadLetter(_trail_dir(cfg, name)).count() == 1
    # Simulate a crash that lost the cursor advance: rewind and re-run. The poison
    # record's source_pos is already recorded, so it is NOT dead-lettered again.
    reg = CdcRegistry(_registry_path(cfg))
    reg.set_apply_cursor(name, 0)
    reg.close()
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert DeadLetter(_trail_dir(cfg, name)).count() == 1        # still one, not two
    assert res["dead_lettered"] == 0                             # skipped as already-seen
    assert any("already-dead-lettered" in w for w in res["warnings"])


# --- B2 (H4): a sick target must not dead-letter the whole backlog ------------


class _SickTarget:
    """Every apply fails AND ``ping`` fails — models a target outage (connection
    refused), not poison data."""

    def __init__(self):
        self.upserts = 0

    def connect(self):
        pass

    def close(self):
        pass

    def ping(self):
        raise ConnectionError("target unreachable")

    def probe_capabilities(self):
        return CapabilityMatrix(edition=Edition.FULL, server_version="14.0 (HeliosDB)")

    def upsert(self, tt, key_cols, columns, rows):
        self.upserts += 1
        raise ConnectionError("target unreachable")

    def update_columns(self, tt, key_cols, set_cols, rows):
        raise ConnectionError("target unreachable")

    def delete_keys(self, tt, key_cols, keys):
        raise ConnectionError("target unreachable")


def test_target_outage_raises_with_zero_dead_letters_and_cursor_unmoved(tmp_path, monkeypatch):
    # A dead target fails every record's retries; without the ping guard all 100
    # records would be dead-lettered and the cursor advanced. With the guard the
    # run RAISES, dead-letters NOTHING, and leaves the cursor at 0.
    cfg = _cfg(tmp_path, poison_retries=3)
    name = "out1"
    _setup(cfg, name, [_rec(i, pos=i, v="ok") for i in range(1, 101)])
    target = _SickTarget()
    _wire(monkeypatch, target)
    with pytest.raises(Exception):
        run_replicat(cfg, name, reconcile_deletes=False)
    assert DeadLetter(_trail_dir(cfg, name)).count() == 0     # nothing parked
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name).apply_cursor == 0                    # cursor unmoved
    reg.close()


def test_poison_on_healthy_target_still_parks_after_retries(tmp_path, monkeypatch):
    # The ping guard must not disable the policy: a genuinely-poison record on a
    # healthy (ping-answering) target is still parked after its retries.
    cfg = _cfg(tmp_path, poison_retries=2)
    name = "out2"
    _setup(cfg, name, [_rec(1, pos=10, v="ok"), _rec(2, pos=20, v="POISON")])
    target = _PoisonTarget()
    _wire(monkeypatch, target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["dead_lettered"] == 1 and res["cursor"] == 2
    assert DeadLetter(_trail_dir(cfg, name)).count() == 1


def test_poison_max_per_run_circuit_breaker(tmp_path, monkeypatch):
    # More than poison_max_per_run dead-letters in one run -> raise (mass poison
    # smells like an environment fault). With max=3 and 5 poison records, exactly 4
    # get parked (the one that trips >3) and then the run raises.
    cfg = _cfg(tmp_path, poison_retries=1)
    cfg.cdc.poison_max_per_run = 3
    name = "out3"
    _setup(cfg, name, [_rec(i, pos=i * 10, v="POISON") for i in range(1, 6)])
    target = _PoisonTarget()
    _wire(monkeypatch, target)
    with pytest.raises(Exception):
        run_replicat(cfg, name, reconcile_deletes=False)
    assert DeadLetter(_trail_dir(cfg, name)).count() == 4     # 4th trips the breaker


def test_dead_letter_records_trail_cursor(tmp_path, monkeypatch):
    # The engine threads each dead-lettered record's trail cursor into the entry.
    cfg = _cfg(tmp_path, poison_retries=1)
    name = "out4"
    _setup(cfg, name, [_rec(1, pos=10, v="ok"), _rec(2, pos=20, v="POISON")])
    _wire(monkeypatch, _PoisonTarget())
    run_replicat(cfg, name, reconcile_deletes=False)
    with open(DeadLetter(_trail_dir(cfg, name)).path) as f:
        entry = json.loads(f.readline())
    # The poison record is the 2nd trail line, so its recorded cursor is 2.
    assert entry["cursor"] == 2


def test_dead_letter_file_uses_atomic_append(tmp_path):
    # Each dead-letter is a single fsync'd JSONL line (the trail's durability
    # discipline); the file is valid JSONL after multiple appends.
    dl = DeadLetter(str(tmp_path))
    for i in range(5):
        dl.append(_rec(i, pos=i), "boom")
    with open(dl.path) as f:
        lines = [json.loads(ln) for ln in f if ln.strip()]
    assert len(lines) == 5 and [e["source_pos"] for e in lines] == list(range(5))
    assert os.path.exists(dl.path)
