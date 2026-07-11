"""K1 — the engine's run_replicat wires the keymove barrier: the apply cursor is
persisted just before AND just after every keymove, so a crash can only ever force
a keymove to replay alone. Hermetic — the source adapter and target driver are
faked; the registry and trail are the real SQLite/file implementations.
"""
from __future__ import annotations

from any2heliosdb.cdc.engine import run_replicat
from any2heliosdb.cdc.registry import CdcRegistry
from any2heliosdb.cdc.trail import Trail
from any2heliosdb.cdc.engine import _trail_dir, _registry_path
from any2heliosdb.config.model import Options, ProjectConfig, SourceConfig, TargetConfig
from any2heliosdb.constants import Edition, SourceDialect, TargetDriverKind
from any2heliosdb.core.catalog_model import Column, DataType, PrimaryKey, Schema, Table
from any2heliosdb.core.change_record import INSERT, UPDATE, ChangeRecord
from any2heliosdb.target.base import CapabilityMatrix


def _table():
    return Table(
        name="t", schema="hr",
        columns=[Column("ID", DataType.decimal(10, 0)), Column("V", DataType.varchar(10)),
                 Column("BODY", DataType.varchar(4000))],
        primary_key=PrimaryKey(columns=["ID"]),
    )


class _FakeAdapter:
    def connect(self):
        pass

    def introspect_schema(self, schema):
        return Schema(name="hr", tables=[_table()])

    def close(self):
        pass


class _FakeTarget:
    """Merge-upsert semantics + matched-count update_columns (mirrors the psycopg
    driver contract the replicat relies on), with connect/probe for the engine."""

    def __init__(self):
        self.rows = {}

    def connect(self):
        pass

    def close(self):
        pass

    def probe_capabilities(self):
        return CapabilityMatrix(edition=Edition.FULL, server_version="14.0 (HeliosDB)")

    def upsert(self, target_table, key_cols, columns, rows):
        n = 0
        for r in rows:
            provided = dict(zip(columns, r))
            key = tuple(provided[k] for k in key_cols)
            merged = dict(self.rows.get((target_table, key), {}))
            merged.update(provided)
            self.rows[(target_table, key)] = merged
            n += 1
        return n

    def update_columns(self, target_table, key_cols, set_cols, rows):
        nset = len(set_cols)
        matched = 0
        for r in rows:
            setvals, keyvals = r[:nset], r[nset:]
            key = tuple(keyvals)
            existing = self.rows.get((target_table, key))
            if existing is None:
                continue
            existing = dict(existing)
            existing.update(dict(zip(set_cols, setvals)))
            newkey = tuple(existing[k] for k in key_cols)
            if newkey != key:
                self.rows.pop((target_table, key), None)
            self.rows[(target_table, newkey)] = existing
            matched += 1
        return matched

    def delete_keys(self, target_table, key_cols, keys):
        n = 0
        for k in keys:
            self.rows.pop((target_table, tuple(k)), None)
            n += 1
        return n


def _cfg(tmp_path):
    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.POSTGRESQL, host="h", port=5432,
                            database="hr", schema="hr", user="u", password="p"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG),
        options=Options(output_dir=str(tmp_path)))


def _wire(monkeypatch, target):
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())
    monkeypatch.setattr(store, "build_target_driver", lambda cfg: target)


def _ins(idv, v, body):
    return ChangeRecord(op=INSERT, schema="hr", table="t", key={"ID": idv},
                        after={"ID": idv, "V": v, "BODY": body})


def _keymove(new_id, old_id, v):
    return ChangeRecord(op=UPDATE, schema="hr", table="t", key={"ID": new_id},
                        after={"ID": new_id, "V": v}, before_key={"ID": old_id})


def test_run_replicat_persists_cursor_around_keymove(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    name = "e1"
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "hr", ["t"])
    reg.close()
    # Trail: [insert id=1, keymove 1->2, insert id=3]. The keymove is the middle
    # record, so its barrier must persist a cursor at 1 (before) and 2 (after).
    Trail(_trail_dir(cfg, name)).append(
        [_ins(1, "a", "BIG"), _keymove(2, 1, "moved"), _ins(3, "c", "Z")])

    cursors = []
    orig = CdcRegistry.set_apply_cursor

    def spy(self, ename, cursor):
        cursors.append(cursor)
        return orig(self, ename, cursor)

    monkeypatch.setattr(CdcRegistry, "set_apply_cursor", spy)

    target = _FakeTarget()
    _wire(monkeypatch, target)
    res = run_replicat(cfg, name, reconcile_deletes=False)

    # The cursor advances 1 (past the leading insert), 2 (past the keymove alone),
    # 3 (past the trailing insert), then the exact final set — so every persisted
    # boundary isolates the keymove.
    assert cursors == [1, 2, 3, 3]
    assert res["applied"] == 3 and res["cursor"] == 3
    # Final state: row moved 1->2 (BODY preserved), row 3 inserted, row 1 gone.
    assert target.rows[("t", (2,))] == {"id": 2, "v": "moved", "body": "BIG"}
    assert target.rows[("t", (3,))]["v"] == "c"
    assert ("t", (1,)) not in target.rows
    # The durable cursor really is 3 after the run.
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name).apply_cursor == 3
    reg.close()


def _cfg_dialect(tmp_path, dialect):
    port = {SourceDialect.ORACLE: 1521, SourceDialect.MYSQL: 3306,
            SourceDialect.POSTGRESQL: 5432}[dialect]
    return ProjectConfig(
        source=SourceConfig(dialect=dialect, host="h", port=port, database="hr",
                            schema="hr", user="u", password="p"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG),
        options=Options(output_dir=str(tmp_path)))


def test_default_reconcile_deletes_is_mode_aware(tmp_path):
    # S3: OFF for log-based sources (explicit D events + keymove race), ON for the
    # Oracle SCN source (no delete events) — derived from the dialect, not hardcoded.
    from any2heliosdb.cdc.engine import _default_reconcile_deletes
    assert _default_reconcile_deletes(_cfg_dialect(tmp_path, SourceDialect.ORACLE)) is True
    assert _default_reconcile_deletes(_cfg_dialect(tmp_path, SourceDialect.MYSQL)) is False
    assert _default_reconcile_deletes(_cfg_dialect(tmp_path, SourceDialect.POSTGRESQL)) is False


def _run_with_reconcile_spy(tmp_path, monkeypatch, dialect, name, reconcile_arg):
    """Run run_replicat over an empty trail, recording whether reconcile ran."""
    from any2heliosdb.cdc.engine import run_replicat
    cfg = _cfg_dialect(tmp_path, dialect)
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "hr", ["t"])
    reg.close()
    calls = []
    from any2heliosdb.cdc.replicat import Replicat

    def _spy(self, adapter):
        calls.append(True)
        return 0, []

    monkeypatch.setattr(Replicat, "reconcile_deletes", _spy)
    _wire(monkeypatch, _FakeTarget())
    run_replicat(cfg, name, reconcile_deletes=reconcile_arg)
    return bool(calls)


def test_run_replicat_default_reconcile_off_for_log_based(tmp_path, monkeypatch):
    assert _run_with_reconcile_spy(tmp_path, monkeypatch, SourceDialect.MYSQL, "m", None) is False
    assert _run_with_reconcile_spy(tmp_path, monkeypatch, SourceDialect.POSTGRESQL, "p", None) is False


def test_run_replicat_default_reconcile_on_for_oracle(tmp_path, monkeypatch):
    assert _run_with_reconcile_spy(tmp_path, monkeypatch, SourceDialect.ORACLE, "o", None) is True


def test_run_replicat_explicit_flag_overrides_mode_default(tmp_path, monkeypatch):
    # --reconcile-deletes forces ON even on a log-based source; --no-deletes forces
    # OFF even on Oracle.
    assert _run_with_reconcile_spy(tmp_path, monkeypatch, SourceDialect.MYSQL, "m2", True) is True
    assert _run_with_reconcile_spy(tmp_path, monkeypatch, SourceDialect.ORACLE, "o2", False) is False


def test_run_replicat_keymove_free_slice_flushes_final_cursor(tmp_path, monkeypatch):
    # A slice with no keymove keeps today's behaviour: the cursor lands on the
    # trail's end (one advance past the whole slice).
    cfg = _cfg(tmp_path)
    name = "e2"
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "hr", ["t"])
    reg.close()
    Trail(_trail_dir(cfg, name)).append([_ins(1, "a", "X"), _ins(2, "b", "Y")])

    target = _FakeTarget()
    _wire(monkeypatch, target)
    res = run_replicat(cfg, name, reconcile_deletes=False)
    assert res["cursor"] == 2 and res["applied"] == 2
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name).apply_cursor == 2
    reg.close()
