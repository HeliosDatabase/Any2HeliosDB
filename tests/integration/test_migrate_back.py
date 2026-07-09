"""End-to-end integration tests for v2 heterogeneous / migrate-back targets.

Two battle tests, both skipped unless the relevant live DBs are provided:

1. **Oracle -> MySQL** — proves the MySQL target driver + MySQL DDL emitter
   (and the orchestrator's dialect dispatch) by migrating the Oracle sample into
   a fresh MySQL database and asserting row counts both via TEST_COUNT and by
   querying MySQL directly.

2. **HeliosDB -> MySQL** (the migrate-back round trip) — proves the PostgreSQL
   source adapter reading a HeliosDB server plus the MySQL target: first migrate
   Oracle -> HeliosDB via the existing psycopg path, then migrate HeliosDB
   (postgres source) -> MySQL and assert the MySQL row counts match. This proves
   data flows back *out* of HeliosDB.

    A2H_TEST_TARGET_PORT=26931 \
    A2H_TEST_MYSQL_PORT=13306 A2H_TEST_MYSQL_USER=root A2H_TEST_MYSQL_PW=root \
    A2H_TEST_ORACLE_DSN=127.0.0.1:1521/XEPDB1 A2H_TEST_ORACLE_USER=hr A2H_TEST_ORACLE_PW=hr \
    PYTHONPATH=src python -m pytest tests/integration/test_migrate_back.py -q
"""
import os

import pytest

pymysql = pytest.importorskip("pymysql")
psycopg = pytest.importorskip("psycopg")
oracledb = pytest.importorskip("oracledb")

TARGET_PORT = int(os.environ.get("A2H_TEST_TARGET_PORT", "0"))
TARGET_HOST = os.environ.get("A2H_TEST_TARGET_HOST", "127.0.0.1")
TARGET_PW = os.environ.get("A2H_TEST_TARGET_PW") or None
MYSQL_HOST = os.environ.get("A2H_TEST_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("A2H_TEST_MYSQL_PORT", "13306"))
MYSQL_USER = os.environ.get("A2H_TEST_MYSQL_USER", "root")
MYSQL_PW = os.environ.get("A2H_TEST_MYSQL_PW", "root")
ORA_DSN = os.environ.get("A2H_TEST_ORACLE_DSN", "127.0.0.1:1521/XEPDB1")
ORA_USER = os.environ.get("A2H_TEST_ORACLE_USER", "hr")
ORA_PW = os.environ.get("A2H_TEST_ORACLE_PW", "hr")

pytestmark = pytest.mark.skipif(
    TARGET_PORT == 0,
    reason="set A2H_TEST_TARGET_PORT to a running HeliosDB to run integration tests")


def _mysql_ok() -> bool:
    try:
        pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                        password=MYSQL_PW, connect_timeout=3).close()
        return True
    except Exception:
        return False


def _oracle_ok() -> bool:
    try:
        oracledb.connect(user=ORA_USER, password=ORA_PW, dsn=ORA_DSN).close()
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


def _ora_hp():
    host, _, svc = ORA_DSN.partition("/")
    h, _, p = host.partition(":")
    return h, int(p or 1521), (svc or None)


def _ensure_oracle_sample():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures"))
    import oracle_sample  # type: ignore
    oracle_sample.DSN, oracle_sample.USER, oracle_sample.PASSWORD = ORA_DSN, ORA_USER, ORA_PW
    oracle_sample.build()


def _recreate_mysql_db(name: str) -> None:
    conn = pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                           password=MYSQL_PW, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP DATABASE IF EXISTS `{}`".format(name))
            cur.execute("CREATE DATABASE `{}` CHARACTER SET utf8mb4".format(name))
    finally:
        conn.close()


def _mysql_count(db: str, table: str) -> int:
    conn = pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                           password=MYSQL_PW, database=db, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM `{}`".format(table))
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def _oracle_to_mysql_cfg(dbname: str):
    from any2heliosdb.config.model import ProjectConfig, SourceConfig, TargetConfig
    from any2heliosdb.constants import SourceDialect, TargetDriverKind

    h, p, svc = _ora_hp()
    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.ORACLE, host=h, port=p, service_name=svc,
                            user=ORA_USER, password=ORA_PW, schema="HR"),
        target=TargetConfig(driver=TargetDriverKind.MYSQL, host=MYSQL_HOST, port=MYSQL_PORT,
                            dbname=dbname, user=MYSQL_USER, password=MYSQL_PW))


def _oracle_to_helios_cfg():
    from any2heliosdb.config.model import ProjectConfig, SourceConfig, TargetConfig
    from any2heliosdb.constants import SourceDialect, TargetDriverKind

    h, p, svc = _ora_hp()
    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.ORACLE, host=h, port=p, service_name=svc,
                            user=ORA_USER, password=ORA_PW, schema="HR"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG, host=TARGET_HOST, port=TARGET_PORT,
                            user="postgres", password=TARGET_PW, dbname="postgres"))


def _helios_to_mysql_cfg(dbname: str):
    from any2heliosdb.config.model import ProjectConfig, SourceConfig, TargetConfig
    from any2heliosdb.constants import SourceDialect, TargetDriverKind

    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.POSTGRESQL, host=TARGET_HOST, port=TARGET_PORT,
                            database="postgres", user="postgres", password=TARGET_PW,
                            schema="public"),
        target=TargetConfig(driver=TargetDriverKind.MYSQL, host=MYSQL_HOST, port=MYSQL_PORT,
                            dbname=dbname, user=MYSQL_USER, password=MYSQL_PW))


def test_oracle_to_mysql_migrate_and_validate():
    """Oracle -> MySQL: MySQL target driver + MySQL DDL + dialect dispatch."""
    if not _oracle_ok():
        pytest.skip("Oracle source not reachable")
    if not _mysql_ok():
        pytest.skip("MySQL target not reachable")
    _ensure_oracle_sample()
    _recreate_mysql_db("hr_fwd")

    from any2heliosdb.config.store import (build_source_adapter, build_target_driver,
                                          build_type_registry)
    from any2heliosdb.core.orchestrator import migrate
    from any2heliosdb.validate.counts import run_test_count
    from any2heliosdb.validate.data import run_test_data
    from any2heliosdb.validate.structure import run_test

    cfg = _oracle_to_mysql_cfg("hr_fwd")
    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect()
    tgt.connect()
    try:
        stats = migrate(src, tgt, schema="HR", registry=build_type_registry(cfg))
        assert stats.tables == 2
        assert stats.total_rows == 8
        assert stats.load_mode == "insert"  # MySQL has no COPY

        schema = src.introspect_schema("HR")
        assert run_test(schema, tgt).passed, "TEST (structure) failed"
        assert run_test_count(src, tgt, schema.tables).passed, "TEST_COUNT failed"
        for t in schema.tables:
            res = run_test_data(src, tgt, t, sample_rows=0)
            assert res.passed, "TEST_DATA failed for {}: {}".format(t.name, res.errors)
    finally:
        src.close()
        tgt.close()

    # Independent assertion straight from MySQL.
    assert _mysql_count("hr_fwd", "departments") == 3
    assert _mysql_count("hr_fwd", "employees") == 5


def test_helios_to_mysql_migrate_back():
    """HeliosDB -> MySQL round trip: PG source adapter (HeliosDB) + MySQL target.

    Migrates Oracle -> HeliosDB first (existing psycopg path), then migrates the
    HeliosDB data back out to MySQL and asserts the MySQL row counts match. PK/FK
    metadata is not exposed by HeliosDB's catalog, so TEST_COUNT (row parity) is
    the structural gate here, mirroring the documented limitation.
    """
    if not _oracle_ok():
        pytest.skip("Oracle source not reachable")
    if not _mysql_ok():
        pytest.skip("MySQL target not reachable")
    if not _target_ok():
        pytest.skip("HeliosDB (postgres source) not reachable")
    _ensure_oracle_sample()

    from any2heliosdb.config.store import (build_source_adapter, build_target_driver,
                                          build_type_registry)
    from any2heliosdb.core.orchestrator import migrate
    from any2heliosdb.validate.counts import run_test_count
    from any2heliosdb.validate.structure import run_test

    # Step 1: Oracle -> HeliosDB (psycopg).
    o2h = _oracle_to_helios_cfg()
    s = build_source_adapter(o2h)
    t = build_target_driver(o2h)
    s.connect()
    t.connect()
    try:
        st = migrate(s, t, schema="HR", registry=build_type_registry(o2h))
        assert st.total_rows == 8
    finally:
        s.close()
        t.close()

    # Step 2: HeliosDB (postgres source) -> MySQL.
    _recreate_mysql_db("hr_back")
    h2m = _helios_to_mysql_cfg("hr_back")
    src = build_source_adapter(h2m)
    tgt = build_target_driver(h2m)
    src.connect()
    tgt.connect()
    try:
        assert getattr(tgt, "dialect", None) == "mysql"
        stats = migrate(src, tgt, schema="public", registry=build_type_registry(h2m))
        assert stats.tables == 2
        assert stats.total_rows == 8

        schema = src.introspect_schema("public")
        assert run_test(schema, tgt).passed, "TEST (structure) failed"
        assert run_test_count(src, tgt, schema.tables).passed, "TEST_COUNT failed"
    finally:
        src.close()
        tgt.close()

    # Independent assertion: data is back out of HeliosDB and in MySQL.
    assert _mysql_count("hr_back", "departments") == 3
    assert _mysql_count("hr_back", "employees") == 5
