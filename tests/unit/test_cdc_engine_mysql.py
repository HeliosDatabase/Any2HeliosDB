"""Hermetic tests for the CDC engine's MySQL extract path — fail-closed on an
empty binlog anchor (F3) without a live MySQL or HeliosDB server.

``run_extract`` imports ``build_source_adapter`` and ``MySqlBinlogSource`` lazily,
so we monkeypatch both: a fake adapter supplies the schema, and a fake source
returns the capture result under test. The registry and trail are the real
SQLite/file implementations writing under ``tmp_path``.
"""
from __future__ import annotations

import os

import pytest

from any2heliosdb.cdc.engine import run_extract
from any2heliosdb.cdc.posfile import read_pos
from any2heliosdb.config.model import Options, ProjectConfig, SourceConfig, TargetConfig
from any2heliosdb.constants import SourceDialect, TargetDriverKind
from any2heliosdb.errors import Any2HeliosError


class _FakeTable:
    def __init__(self, name):
        self.name = name


class _FakeSchemaIR:
    name = "hr"
    tables = [_FakeTable("t1")]


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


def _patch_capture(monkeypatch, capture_result):
    from any2heliosdb.config import store
    from any2heliosdb.cdc.sources import mysql_binlog

    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())

    class _FakeSource:
        def __init__(self, *a, **k):
            pass

        def capture(self, since, limit=0):
            return capture_result

    monkeypatch.setattr(mysql_binlog, "MySqlBinlogSource", _FakeSource)


def _posfile(tmp_path, name):
    return os.path.join(str(tmp_path), "trail", name, "binlog.pos")


def test_empty_anchor_raises_and_writes_no_pos_file(tmp_path, monkeypatch):
    # capture() could not read a binlog coordinate (log_bin=OFF or no REPLICATION
    # CLIENT): the extract must fail closed, NOT persist an empty pos file (which
    # would make every later run abort on a corrupt cursor).
    _patch_capture(monkeypatch, ([], ""))
    with pytest.raises(Any2HeliosError) as ei:
        run_extract(_cfg(tmp_path), "e1")
    msg = str(ei.value).lower()
    assert "log_bin" in msg and "replication client" in msg
    assert not os.path.exists(_posfile(tmp_path, "e1"))


def test_healthy_cycle_persists_pos_atomically(tmp_path, monkeypatch):
    # A real anchor is written atomically and reads back (round-1 durability kept).
    _patch_capture(monkeypatch, ([], "mysql-bin.000001:120"))
    res = run_extract(_cfg(tmp_path), "e1")
    assert res["watermark"] == "mysql-bin.000001:120"
    assert res["mode"] == "binlog"
    assert read_pos(_posfile(tmp_path, "e1")) == "mysql-bin.000001:120"
