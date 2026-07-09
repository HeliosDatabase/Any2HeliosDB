"""End-to-end integration test: MySQL -> HeliosDB migrate + validate.

Mirrors the Oracle battle test. Skipped unless a target is provided:

    A2H_TEST_TARGET_PORT=26499 \
    A2H_TEST_MYSQL_PORT=13306 A2H_TEST_MYSQL_USER=root A2H_TEST_MYSQL_PW=root \
    python -m pytest tests/integration/test_mysql_to_helios.py -q

The MySQL sample schema is created on demand (tests/fixtures/mysql_sample.py).
"""
import os

import pytest

pymysql = pytest.importorskip("pymysql")
psycopg = pytest.importorskip("psycopg")

TARGET_PORT = int(os.environ.get("A2H_TEST_TARGET_PORT", "0"))
TARGET_HOST = os.environ.get("A2H_TEST_TARGET_HOST", "127.0.0.1")
TARGET_PW = os.environ.get("A2H_TEST_TARGET_PW") or None
MYSQL_HOST = os.environ.get("A2H_TEST_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("A2H_TEST_MYSQL_PORT", "13306"))
MYSQL_USER = os.environ.get("A2H_TEST_MYSQL_USER", "root")
MYSQL_PW = os.environ.get("A2H_TEST_MYSQL_PW", "root")

pytestmark = pytest.mark.skipif(
    TARGET_PORT == 0, reason="set A2H_TEST_TARGET_PORT to a running HeliosDB to run integration tests")


def _mysql_ok() -> bool:
    try:
        pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                        password=MYSQL_PW, connect_timeout=3).close()
        return True
    except Exception:
        return False


def _target_ok() -> bool:
    try:
        psycopg.connect(host=TARGET_HOST, port=TARGET_PORT, user="postgres",
                        password=TARGET_PW, dbname="postgres", connect_timeout=3).close()
        return True
    except Exception:
        return False


def _cfg():
    from any2heliosdb.config.model import ProjectConfig, SourceConfig, TargetConfig
    from any2heliosdb.constants import SourceDialect, TargetDriverKind

    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.MYSQL, host=MYSQL_HOST, port=MYSQL_PORT,
                            database="hr", schema="hr", user=MYSQL_USER, password=MYSQL_PW),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG, host=TARGET_HOST, port=TARGET_PORT,
                            user="postgres", password=TARGET_PW))


def _ensure_sample():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures"))
    import mysql_sample  # type: ignore
    mysql_sample.HOST, mysql_sample.PORT = MYSQL_HOST, MYSQL_PORT
    mysql_sample.USER, mysql_sample.PASSWORD = MYSQL_USER, MYSQL_PW
    mysql_sample.build()


def test_mysql_to_helios_migrate_and_validate():
    if not _mysql_ok():
        pytest.skip("MySQL source not reachable")
    if not _target_ok():
        pytest.skip("HeliosDB target not reachable")
    _ensure_sample()

    from any2heliosdb.config.store import (build_source_adapter, build_target_driver,
                                          build_type_registry)
    from any2heliosdb.core.orchestrator import migrate
    from any2heliosdb.validate.counts import run_test_count
    from any2heliosdb.validate.data import run_test_data
    from any2heliosdb.validate.structure import run_test

    cfg = _cfg()
    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect()
    tgt.connect()
    try:
        stats = migrate(src, tgt, schema="hr", registry=build_type_registry(cfg))
        assert stats.tables == 2
        assert stats.total_rows == 8

        schema = src.introspect_schema("hr")
        assert run_test(schema, tgt).passed, "TEST (structure) failed"
        assert run_test_count(src, tgt, schema.tables).passed, "TEST_COUNT failed"
        for t in schema.tables:
            res = run_test_data(src, tgt, t, sample_rows=0)
            assert res.passed, "TEST_DATA failed for {}: {}".format(t.name, res.errors)
    finally:
        src.close()
        tgt.close()
