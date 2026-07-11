"""H3 — slot lifecycle (``--drop``) + lag visibility (``a2h extracts --lag``).

Nothing dropped the PG logical slot before, so an abandoned extract pinned WAL
until the source disk filled, and lag was invisible. These tests drive the
teardown and lag computation hermetically with fake adapters.
"""
from __future__ import annotations

import os

import pytest

from any2heliosdb.cdc.engine import (
    _binlog_pos_file, _registry_path, _trail_dir, drop_extract, extract_lag)
from any2heliosdb.cdc.posfile import write_pos_atomic
from any2heliosdb.cdc.registry import CdcRegistry
from any2heliosdb.cdc.trail import Trail
from any2heliosdb.config.model import Options, ProjectConfig, SourceConfig, TargetConfig
from any2heliosdb.constants import SourceDialect, TargetDriverKind
from any2heliosdb.core.change_record import INSERT, ChangeRecord
from any2heliosdb.errors import Any2HeliosError


def _cfg(tmp_path, dialect):
    port = {SourceDialect.ORACLE: 1521, SourceDialect.MYSQL: 3306,
            SourceDialect.POSTGRESQL: 5432}[dialect]
    return ProjectConfig(
        source=SourceConfig(dialect=dialect, host="h", port=port, database="hr",
                            schema="public", user="u", password="p"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG),
        options=Options(output_dir=str(tmp_path)))


# --- --drop teardown ----------------------------------------------------------


class _PgDropAdapter:
    calls = []

    def connect(self):
        pass

    def _q1(self, sql, *p):
        type(self).calls.append((sql, p))
        # Model an existing slot so drop_slot proceeds to pg_drop_replication_slot.
        if "pg_replication_slots" in sql:
            return (1,)
        return None

    def close(self):
        pass


class _PgDropFailAdapter:
    """The slot exists but the drop itself fails (e.g. still active elsewhere)."""

    def connect(self):
        pass

    def _q1(self, sql, *p):
        if "pg_replication_slots" in sql:
            return (1,)
        if "pg_drop_replication_slot" in sql:
            raise RuntimeError("replication slot is active for PID 123")
        return None

    def close(self):
        pass


class _PgAbsentSlotAdapter:
    """No slot exists — drop is a clean no-op, registry still removed."""

    def connect(self):
        pass

    def _q1(self, sql, *p):
        return None

    def close(self):
        pass


def test_drop_pg_drops_slot_and_removes_registry(tmp_path, monkeypatch):
    from any2heliosdb.config import store
    _PgDropAdapter.calls = []
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _PgDropAdapter())
    cfg = _cfg(tmp_path, SourceDialect.POSTGRESQL)
    name = "d1"
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "public", ["t"])
    reg.close()
    Trail(_trail_dir(cfg, name)).append(
        [ChangeRecord(op=INSERT, schema="public", table="t", key={"id": 1}, after={"id": 1})])

    r = drop_extract(cfg, name, purge_trail=False)
    assert r["removed"] is True and r["dropped_slot"] is True and r["purged_trail"] is False
    # The slot really was dropped (by its derived name), and the registry entry is gone.
    assert any("pg_drop_replication_slot" in sql for sql, _ in _PgDropAdapter.calls)
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name) is None
    reg.close()
    # The trail is KEPT by default.
    assert os.path.isdir(_trail_dir(cfg, name))


def test_drop_purge_trail_removes_directory(tmp_path, monkeypatch):
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _PgDropAdapter())
    cfg = _cfg(tmp_path, SourceDialect.POSTGRESQL)
    name = "d2"
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "public", ["t"])
    reg.close()
    Trail(_trail_dir(cfg, name)).append(
        [ChangeRecord(op=INSERT, schema="public", table="t", key={"id": 1}, after={"id": 1})])
    r = drop_extract(cfg, name, purge_trail=True)
    assert r["purged_trail"] is True
    assert not os.path.isdir(_trail_dir(cfg, name))


def test_drop_unknown_extract_raises(tmp_path, monkeypatch):
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _PgDropAdapter())
    with pytest.raises(Any2HeliosError):
        drop_extract(_cfg(tmp_path, SourceDialect.POSTGRESQL), "nope")


def test_drop_pg_slot_failure_raises_and_keeps_registry(tmp_path, monkeypatch):
    # M2: a failed slot drop must RAISE and leave the registry entry intact — a
    # still-WAL-pinning slot must not vanish from `a2h extracts` under a false
    # dropped=True.
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _PgDropFailAdapter())
    cfg = _cfg(tmp_path, SourceDialect.POSTGRESQL)
    name = "df"
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "public", ["t"])
    reg.close()
    with pytest.raises(Any2HeliosError):
        drop_extract(cfg, name)
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name) is not None            # registry entry NOT removed
    reg.close()


def test_drop_pg_absent_slot_clean_removal(tmp_path, monkeypatch):
    # M2: an already-absent slot is not an error — dropped_slot=False, entry removed.
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _PgAbsentSlotAdapter())
    cfg = _cfg(tmp_path, SourceDialect.POSTGRESQL)
    name = "da"
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "public", ["t"])
    reg.close()
    r = drop_extract(cfg, name)
    assert r["removed"] is True and r["dropped_slot"] is False
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name) is None
    reg.close()


def test_drop_mysql_removes_registry_without_slot(tmp_path):
    # MySQL has no server-side slot to drop; the registry entry is still removed.
    cfg = _cfg(tmp_path, SourceDialect.MYSQL)
    name = "d3"
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "hr", ["t"])
    reg.close()
    r = drop_extract(cfg, name)
    assert r["removed"] is True and r["dropped_slot"] is False
    reg = CdcRegistry(_registry_path(cfg))
    assert reg.get(name) is None
    reg.close()


# --- lag ----------------------------------------------------------------------


class _PgLagAdapter:
    def connect(self):
        pass

    def _q1(self, sql, *p):
        if "pg_replication_slots" in sql:
            return ("0/100", "0/50")           # confirmed_flush, restart
        if "pg_current_wal_lsn" in sql:
            return ("0/200",)
        return None

    def close(self):
        pass


def test_extract_lag_pg_computes_bytes_behind(tmp_path, monkeypatch):
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _PgLagAdapter())
    cfg = _cfg(tmp_path, SourceDialect.POSTGRESQL)
    reg = CdcRegistry(_registry_path(cfg))
    reg.register("L1", "public", ["t"])
    ext = reg.get("L1")
    reg.close()
    lag = extract_lag(cfg, ext)
    assert lag is not None
    assert lag["confirmed_flush_lsn"] == "0/100" and lag["current_wal_lsn"] == "0/200"
    assert lag["bytes_behind"] == 0x200 - 0x100     # 256 bytes of WAL still pinned


def test_extract_lag_mysql_computes_bytes_behind(tmp_path, monkeypatch):
    from any2heliosdb.cdc.sources import mysql_binlog
    monkeypatch.setattr(mysql_binlog.MySqlBinlogSource, "master_position",
                        lambda self: "mysql-bin.000005:900")
    cfg = _cfg(tmp_path, SourceDialect.MYSQL)
    name = "L2"
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "hr", ["t"])
    ext = reg.get(name)
    reg.close()
    write_pos_atomic(_binlog_pos_file(cfg, name), "mysql-bin.000005:100")
    lag = extract_lag(cfg, ext)
    assert lag is not None and lag["mode"] == "binlog"
    assert lag["trailed_pos"] == "mysql-bin.000005:100"
    assert lag["current_pos"] == "mysql-bin.000005:900"
    # Same file: files_behind=0 and bytes_behind is the true byte delta.
    assert lag["files_behind"] == 0
    assert lag["bytes_behind"] == 900 - 100


def test_extract_lag_mysql_reports_files_behind_across_rollover(tmp_path, monkeypatch):
    # Across a binlog file rollover a raw encoded delta is meaningless; report the
    # count of whole files behind and the head's own offset within its file.
    from any2heliosdb.cdc.sources import mysql_binlog
    monkeypatch.setattr(mysql_binlog.MySqlBinlogSource, "master_position",
                        lambda self: "mysql-bin.000006:250")
    cfg = _cfg(tmp_path, SourceDialect.MYSQL)
    name = "L2b"
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "hr", ["t"])
    ext = reg.get(name)
    reg.close()
    write_pos_atomic(_binlog_pos_file(cfg, name), "mysql-bin.000004:900")
    lag = extract_lag(cfg, ext)
    assert lag is not None and lag["files_behind"] == 2      # 000004 -> 000006
    assert lag["bytes_behind"] == 250                        # head offset in its own file


class _OracleLagAdapter:
    def connect(self):
        pass

    def current_scn(self):
        return 5000

    def close(self):
        pass


def test_extract_lag_oracle_computes_scn_behind(tmp_path, monkeypatch):
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _OracleLagAdapter())
    cfg = _cfg(tmp_path, SourceDialect.ORACLE)
    name = "L3"
    reg = CdcRegistry(_registry_path(cfg))
    reg.register(name, "HR", ["t"])
    reg.set_watermark(name, 4200)
    ext = reg.get(name)
    reg.close()
    lag = extract_lag(cfg, ext)
    assert lag is not None and lag["mode"] == "scn"
    assert lag["watermark_scn"] == 4200 and lag["current_scn"] == 5000
    assert lag["scn_behind"] == 800


def test_extract_lag_returns_none_when_source_unreachable(tmp_path, monkeypatch):
    from any2heliosdb.config import store

    def _boom(cfg):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(store, "build_source_adapter", _boom)
    cfg = _cfg(tmp_path, SourceDialect.POSTGRESQL)
    reg = CdcRegistry(_registry_path(cfg))
    reg.register("L4", "public", ["t"])
    ext = reg.get("L4")
    reg.close()
    assert extract_lag(cfg, ext) is None      # advisory: never fails the listing
