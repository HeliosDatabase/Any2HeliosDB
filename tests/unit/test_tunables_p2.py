"""P2 tunables: newly-exposed timeouts/knobs must round-trip through config and
reach each driver's own connect parameter.

Hermetic — every DB driver's ``connect`` is monkeypatched to capture the kwargs
it was called with, so no server is opened. Covers:
  * [source] connect_timeout  -> oracledb tcp_connect_timeout / pymysql
    connect_timeout / pyodbc timeout / psycopg connect_timeout
  * [target] connect_timeout  -> native Oracle-wire driver tcp_connect_timeout
  * [options] native_call_timeout_ms -> native driver call_timeout
  * config TOML round-trip + legacy-config defaults for all of them
"""
from __future__ import annotations

import os
import tempfile

from any2heliosdb.config.model import (Options, ProjectConfig, SourceConfig,
                                       TargetConfig)
from any2heliosdb.config.store import build_target_driver, load_config, save_config
from any2heliosdb.constants import SourceDialect, TargetDriverKind
from any2heliosdb.sources.base import SourceDsn


class _FakeCur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return None


class _FakeConn:
    """Accepts arbitrary attribute writes (autocommit/prepare_threshold/…) and
    yields a no-op cursor context manager."""

    def cursor(self, *a, **k):
        return _FakeCur()

    def execute(self, *a, **k):
        return None


# --- config round-trip -------------------------------------------------------
def test_source_config_carries_connect_timeout_into_dsn():
    d = SourceConfig(connect_timeout=7).to_dsn()
    assert d.connect_timeout == 7
    assert SourceConfig().to_dsn().connect_timeout == 10  # default


def test_target_config_carries_connect_timeout_into_dsn():
    d = TargetConfig(connect_timeout=42).to_dsn()
    assert d.connect_timeout == 42
    assert TargetConfig().to_dsn().connect_timeout == 10  # default


def test_options_tunables_defaults():
    o = Options()
    assert o.chunks_per_worker == 2
    assert o.native_call_timeout_ms == 300_000


def test_all_new_keys_roundtrip_through_toml():
    cfg = ProjectConfig()
    cfg.source = SourceConfig(dialect=SourceDialect.ORACLE, connect_timeout=3)
    cfg.target = TargetConfig(driver=TargetDriverKind.PSYCOPG, connect_timeout=5)
    cfg.options = Options(chunks_per_worker=4, native_call_timeout_ms=120_000)
    fd, p = tempfile.mkstemp(suffix=".toml")
    os.close(fd)
    try:
        save_config(cfg, p)
        back = load_config(p)
        assert back.source.connect_timeout == 3
        assert back.target.connect_timeout == 5
        assert back.options.chunks_per_worker == 4
        assert back.options.native_call_timeout_ms == 120_000
    finally:
        os.remove(p)


def test_legacy_config_without_new_keys_gets_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[source]\ndialect = "oracle"\n[target]\ndriver = "psycopg"\n[options]\n')
    cfg = load_config(str(p))
    assert cfg.source.connect_timeout == 10
    assert cfg.target.connect_timeout == 10
    assert cfg.options.chunks_per_worker == 2
    assert cfg.options.native_call_timeout_ms == 300_000


# --- source adapters thread connect_timeout into the driver ------------------
def test_oracle_source_passes_tcp_connect_timeout(monkeypatch):
    import oracledb

    from any2heliosdb.sources.oracle.adapter import OracleAdapter

    captured = {}
    monkeypatch.setattr(oracledb, "connect",
                        lambda **kw: captured.update(kw) or _FakeConn())
    OracleAdapter(SourceDsn(host="h", service_name="S", user="u",
                            connect_timeout=13)).connect()
    assert captured["tcp_connect_timeout"] == 13


def test_mysql_source_passes_connect_timeout(monkeypatch):
    import pymysql

    from any2heliosdb.sources.mysql.adapter import MySQLAdapter

    captured = {}
    monkeypatch.setattr(pymysql, "connect",
                        lambda **kw: captured.update(kw) or _FakeConn())
    MySQLAdapter(SourceDsn(host="h", database="d", user="u",
                           connect_timeout=14)).connect()
    assert captured["connect_timeout"] == 14


def test_mssql_source_passes_login_timeout(monkeypatch):
    import pyodbc

    from any2heliosdb.sources.mssql.adapter import MSSQLAdapter

    captured = {}

    def fake_connect(connstr, **kw):
        captured["connstr"] = connstr
        captured.update(kw)
        return _FakeConn()

    monkeypatch.setattr(pyodbc, "connect", fake_connect)
    MSSQLAdapter(SourceDsn(host="h", database="d", user="u",
                           connect_timeout=15)).connect()
    # pyodbc's `timeout` kwarg == SQL_ATTR_LOGIN_TIMEOUT (the connect wait)
    assert captured["timeout"] == 15


def test_postgres_source_passes_connect_timeout(monkeypatch):
    import psycopg

    from any2heliosdb.sources.postgres.adapter import PostgresAdapter

    captured = {}
    monkeypatch.setattr(psycopg, "connect",
                        lambda **kw: captured.update(kw) or _FakeConn())
    PostgresAdapter(SourceDsn(host="h", database="d", user="u",
                              connect_timeout=16)).connect()
    assert captured["connect_timeout"] == 16


# --- native target: connect_timeout + call_timeout_ms ------------------------
def test_native_target_threads_call_timeout_and_connect_timeout(monkeypatch):
    import oracledb

    cfg = ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.ORACLE),
        target=TargetConfig(driver=TargetDriverKind.NATIVE, connect_timeout=11),
        options=Options(native_call_timeout_ms=90_000))
    driver = build_target_driver(cfg)
    # options.native_call_timeout_ms reached the driver
    assert driver.call_timeout_ms == 90_000

    captured = {}

    def fake_connect(**kw):
        captured.update(kw)
        return _FakeConn()

    monkeypatch.setattr(oracledb, "connect", fake_connect)
    driver.connect()
    # connect-establishment timeout came from [target] connect_timeout
    assert captured["tcp_connect_timeout"] == 11
    # the per-round-trip call_timeout was set to the configured value (ms)
    assert driver.conn.call_timeout == 90_000


# --- review fixes: hash backward-compat, resume threading, timeout floor -------


def test_config_hash_backward_compatible_at_default_cpw(tmp_path):
    # Manifests recorded BEFORE chunks_per_worker existed hold the 13-field
    # hash; a default-cpw loader must reproduce it bit-for-bit (no spurious
    # reset of every pre-upgrade run), while a non-default cpw diverges.
    import hashlib
    import types

    from any2heliosdb.core.catalog_model import Schema
    from any2heliosdb.core.loader import ResumableLoader

    cfg = types.SimpleNamespace(
        source=types.SimpleNamespace(dialect="oracle", host="h", port=1521,
                                     database="db", schema="HR", user="hr"),
        target=types.SimpleNamespace(driver="native", host="th", port=1521,
                                     dbname="tdb", user="tu"),
        options=types.SimpleNamespace(preserve_case=False, manifest_backend="sqlite"))

    def mk(cpw):
        return ResumableLoader(cfg, Schema("HR", tables=[]),
                               str(tmp_path / "m.db"), "r1", parallelism=2,
                               chunks_per_worker=cpw)
    s, t, o = cfg.source, cfg.target, cfg.options
    src_db = getattr(s, "database", "") or ""
    legacy_key = "|".join(str(x) for x in (
        getattr(s, "dialect", ""), s.host, s.port, src_db, s.schema or "", s.user,
        getattr(t, "driver", ""), t.host, t.port, t.dbname, getattr(t, "user", ""),
        bool(o.preserve_case), 2,
    ))
    legacy_hash = hashlib.sha1(legacy_key.encode("utf-8")).hexdigest()
    assert mk(2)._config_hash() == legacy_hash        # pre-upgrade manifests resume
    assert mk(4)._config_hash() != legacy_hash        # changed knob still resets


def test_connect_timeout_zero_rejected_at_load(tmp_path):
    import pytest

    from any2heliosdb.config.store import load_config
    from any2heliosdb.errors import Any2HeliosError

    p = tmp_path / "c.toml"
    p.write_text("""
[source]
dialect = "mysql"
host = "h"
database = "d"
user = "u"
connect_timeout = 0

[target]
host = "t"
dbname = "db"
""")
    with pytest.raises(Any2HeliosError, match="connect_timeout must be >= 1"):  # ConfigError subclasses it
        load_config(str(p))
