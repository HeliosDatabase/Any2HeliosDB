"""End-to-end integration test: SQL Server -> HeliosDB migrate + validate.

Mirrors the Oracle/MySQL battle tests. Skipped unless a target is provided:

    A2H_TEST_TARGET_PORT=26499 \
    A2H_TEST_MSSQL_PORT=14433 A2H_TEST_MSSQL_USER=sa A2H_TEST_MSSQL_PW='Strong!Passw0rd' \
    python -m pytest tests/integration/test_mssql_to_helios.py -q

The SQL Server sample schema is created on demand (tests/fixtures/mssql_sample.py).
"""
import os

import pytest

pyodbc = pytest.importorskip("pyodbc")
psycopg = pytest.importorskip("psycopg")

TARGET_PORT = int(os.environ.get("A2H_TEST_TARGET_PORT", "0"))
TARGET_HOST = os.environ.get("A2H_TEST_TARGET_HOST", "127.0.0.1")
TARGET_PW = os.environ.get("A2H_TEST_TARGET_PW") or None
MSSQL_HOST = os.environ.get("A2H_TEST_MSSQL_HOST", "127.0.0.1")
MSSQL_PORT = int(os.environ.get("A2H_TEST_MSSQL_PORT", "14433"))
MSSQL_USER = os.environ.get("A2H_TEST_MSSQL_USER", "sa")
MSSQL_PW = os.environ.get("A2H_TEST_MSSQL_PW", "Strong!Passw0rd")

pytestmark = pytest.mark.skipif(
    TARGET_PORT == 0, reason="set A2H_TEST_TARGET_PORT to a running HeliosDB to run integration tests")


def _mssql_conn_str(database: str) -> str:
    drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
    driver = next((d for d in drivers if "ODBC Driver" in d), drivers[0] if drivers else
                  "ODBC Driver 18 for SQL Server")
    return (
        "DRIVER={{{}}};SERVER={},{};DATABASE={};UID={};PWD={};"
        "TrustServerCertificate=yes;Encrypt=optional".format(
            driver, MSSQL_HOST, MSSQL_PORT, database, MSSQL_USER, MSSQL_PW))


def _mssql_ok() -> bool:
    try:
        pyodbc.connect(_mssql_conn_str("master"), timeout=3).close()
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
        source=SourceConfig(dialect=SourceDialect.MSSQL, host=MSSQL_HOST, port=MSSQL_PORT,
                            database="hr", schema="dbo", user=MSSQL_USER, password=MSSQL_PW),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG, host=TARGET_HOST, port=TARGET_PORT,
                            user="postgres", password=TARGET_PW))


def _ensure_sample():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures"))
    import mssql_sample  # type: ignore
    mssql_sample.HOST, mssql_sample.PORT = MSSQL_HOST, MSSQL_PORT
    mssql_sample.USER, mssql_sample.PASSWORD = MSSQL_USER, MSSQL_PW
    mssql_sample.build()


def test_mssql_to_helios_migrate_and_validate():
    if not _mssql_ok():
        pytest.skip("SQL Server source not reachable")
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
        stats = migrate(src, tgt, schema="dbo", registry=build_type_registry(cfg))
        assert stats.tables == 2
        assert stats.total_rows == 8

        schema = src.introspect_schema("dbo")
        assert run_test(schema, tgt).passed, "TEST (structure) failed"
        assert run_test_count(src, tgt, schema.tables).passed, "TEST_COUNT failed"
        for t in schema.tables:
            res = run_test_data(src, tgt, t, sample_rows=0)
            assert res.passed, "TEST_DATA failed for {}: {}".format(t.name, res.errors)
    finally:
        src.close()
        tgt.close()
