"""MySQL binlog CDC integration test: log-based I/U/D capture -> apply.

Guarded: needs a HeliosDB target, a MySQL with ROW binlog, and the
`mysql-replication` library. Mirrors the manual battle test.

    A2H_TEST_TARGET_PORT=26499 A2H_TEST_MYSQL_PORT=13306 \
    python -m pytest tests/integration/test_mysql_binlog.py -q
"""
import os
import time

import pytest

pymysql = pytest.importorskip("pymysql")
psycopg = pytest.importorskip("psycopg")
pytest.importorskip("pymysqlreplication")

TARGET_PORT = int(os.environ.get("A2H_TEST_TARGET_PORT", "0"))
TARGET_HOST = os.environ.get("A2H_TEST_TARGET_HOST", "127.0.0.1")
TARGET_PW = os.environ.get("A2H_TEST_TARGET_PW") or None
MYSQL_HOST = os.environ.get("A2H_TEST_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("A2H_TEST_MYSQL_PORT", "13306"))
MYSQL_USER = os.environ.get("A2H_TEST_MYSQL_USER", "root")
MYSQL_PW = os.environ.get("A2H_TEST_MYSQL_PW", "root")

pytestmark = pytest.mark.skipif(TARGET_PORT == 0, reason="set A2H_TEST_TARGET_PORT")


def _ok(fn):
    try:
        fn().close()
        return True
    except Exception:
        return False


def _cfg(tmp_path):
    from any2heliosdb.config.model import Options, ProjectConfig, SourceConfig, TargetConfig
    from any2heliosdb.constants import SourceDialect, TargetDriverKind

    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.MYSQL, host=MYSQL_HOST, port=MYSQL_PORT,
                            database="hr", schema="hr", user=MYSQL_USER, password=MYSQL_PW),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG, host=TARGET_HOST, port=TARGET_PORT,
                            user="postgres", password=TARGET_PW),
        options=Options(output_dir=str(tmp_path)))


def test_mysql_binlog_cdc(tmp_path):
    if not _ok(lambda: pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                                       password=MYSQL_PW, connect_timeout=3)):
        pytest.skip("MySQL not reachable")
    if not _ok(lambda: psycopg.connect(host=TARGET_HOST, port=TARGET_PORT, user="postgres",
                                       password=TARGET_PW, dbname="postgres", connect_timeout=3)):
        pytest.skip("HeliosDB target not reachable")
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures"))
    import mysql_sample  # type: ignore
    mysql_sample.HOST, mysql_sample.PORT = MYSQL_HOST, MYSQL_PORT
    mysql_sample.USER, mysql_sample.PASSWORD = MYSQL_USER, MYSQL_PW
    mysql_sample.build()

    from any2heliosdb.cdc.engine import run_extract, run_replicat
    from any2heliosdb.config.store import (build_source_adapter, build_target_driver,
                                          build_type_registry)
    from any2heliosdb.core.orchestrator import migrate

    cfg = _cfg(tmp_path)
    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect(); tgt.connect()
    try:
        migrate(src, tgt, schema="hr", registry=build_type_registry(cfg))
        run_extract(cfg, "b1")  # anchor the binlog position (+ set FULL metadata)

        mc = pymysql.connect(host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER,
                             password=MYSQL_PW, database="hr", autocommit=True)
        mcur = mc.cursor()
        mcur.execute("INSERT INTO employees (emp_id,full_name,email,salary,active,dept_id) "
                     "VALUES (6,'Katherine Johnson','kj@example.com',95000,1,20)")
        mcur.execute("UPDATE employees SET salary=999999 WHERE emp_id=1")
        mcur.execute("DELETE FROM employees WHERE emp_id=5")
        mc.close()
        time.sleep(0.5)  # let the binlog flush

        captured = run_extract(cfg, "b1")["captured"]
        assert captured >= 3, "binlog capture missed events ({} captured)".format(captured)
        run_replicat(cfg, "b1", reconcile_deletes=False)  # binlog D records do the delete

        assert tgt.query("SELECT count(*) FROM employees")[0][0] == 5
        assert tgt.query("SELECT full_name FROM employees WHERE emp_id=6")[0][0] == "Katherine Johnson"
        assert float(tgt.query("SELECT salary FROM employees WHERE emp_id=1")[0][0]) == 999999
        assert tgt.query("SELECT count(*) FROM employees WHERE emp_id=5")[0][0] == 0
    finally:
        src.close(); tgt.close()
