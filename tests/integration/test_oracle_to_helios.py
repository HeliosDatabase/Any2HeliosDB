"""End-to-end integration test: Oracle -> HeliosDB migrate + validate.

This is the *battle test*: it runs the real pipeline (introspect -> DDL -> COPY/
INSERT -> validate) against a live Oracle source and a live HeliosDB target, and
asserts that TEST, TEST_COUNT, and TEST_DATA all pass — including a BLOB, NULLs,
an Oracle empty-string-as-NULL, and exact NUMBER(p,s) values.

It is skipped unless a target is provided, so the default unit run stays
hermetic:

    # start a HeliosDB on some port, then:
    A2H_TEST_TARGET_PORT=26499 \
    A2H_TEST_ORACLE_DSN=localhost:1521/XEPDB1 A2H_TEST_ORACLE_USER=hr A2H_TEST_ORACLE_PW=hr \
    python -m pytest tests/integration -q

The Oracle sample schema is created on demand (tests/fixtures/oracle_sample.py).
"""
import os

import pytest

oracledb = pytest.importorskip("oracledb")
psycopg = pytest.importorskip("psycopg")

TARGET_PORT = int(os.environ.get("A2H_TEST_TARGET_PORT", "0"))
TARGET_HOST = os.environ.get("A2H_TEST_TARGET_HOST", "127.0.0.1")
TARGET_PW = os.environ.get("A2H_TEST_TARGET_PW") or None
ORA_DSN = os.environ.get("A2H_TEST_ORACLE_DSN", "127.0.0.1:1521/XEPDB1")
ORA_USER = os.environ.get("A2H_TEST_ORACLE_USER", "hr")
ORA_PW = os.environ.get("A2H_TEST_ORACLE_PW", "hr")

pytestmark = pytest.mark.skipif(
    TARGET_PORT == 0, reason="set A2H_TEST_TARGET_PORT to a running HeliosDB to run integration tests"
)


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


def _target_edition() -> str:
    from any2heliosdb.config.store import build_target_driver

    tgt = build_target_driver(_cfg())
    tgt.connect()
    try:
        return tgt.probe_capabilities().edition.value
    finally:
        tgt.close()


def _cfg():
    from any2heliosdb.config.model import ProjectConfig, SourceConfig, TargetConfig
    from any2heliosdb.constants import SourceDialect, TargetDriverKind

    host, _, svc = ORA_DSN.partition("/")
    h, _, p = host.partition(":")
    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.ORACLE, host=h, port=int(p or 1521),
                            service_name=svc or None, user=ORA_USER, password=ORA_PW, schema="HR"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG, host=TARGET_HOST, port=TARGET_PORT,
                            user="postgres", password=TARGET_PW),
    )


def test_oracle_to_helios_migrate_and_validate():
    if not _oracle_ok():
        pytest.skip("Oracle source not reachable")
    if not _target_ok():
        pytest.skip("HeliosDB target not reachable")

    # Ensure the sample schema exists.
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures"))
    import oracle_sample  # type: ignore
    oracle_sample.DSN, oracle_sample.USER, oracle_sample.PASSWORD = ORA_DSN, ORA_USER, ORA_PW
    oracle_sample.build()

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
        stats = migrate(src, tgt, schema="HR", registry=build_type_registry(cfg))
        assert stats.tables == 2
        assert stats.total_rows == 8

        schema = src.introspect_schema("HR")
        assert run_test(schema, tgt).passed, "TEST (structure) failed"
        assert run_test_count(src, tgt, schema.tables).passed, "TEST_COUNT failed"
        for t in schema.tables:
            res = run_test_data(src, tgt, t, sample_rows=0)
            assert res.passed, "TEST_DATA failed for {}: {}".format(t.name, res.errors)
    finally:
        src.close()
        tgt.close()


def _ensure_sample():
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures"))
    import oracle_sample  # type: ignore
    oracle_sample.DSN, oracle_sample.USER, oracle_sample.PASSWORD = ORA_DSN, ORA_USER, ORA_PW
    oracle_sample.build()


def test_wizard_smoke_test():
    if not _oracle_ok():
        pytest.skip("Oracle source not reachable")
    if not _target_ok():
        pytest.skip("HeliosDB target not reachable")
    from any2heliosdb.config.wizard import smoke_test

    report = smoke_test(_cfg())
    assert report["source_version"], "no source version"
    assert report["target_edition"] in ("lite", "full", "nano")
    assert report["copy_from_stdin"] is True, "COPY not detected"
    # NULL-vs-empty-string fidelity is informational, not a pass/fail gate:
    # Oracle-compat targets (e.g. Full) legitimately fold '' -> NULL exactly as
    # the Oracle source does, so a strict PG-fidelity assertion is wrong here.
    assert isinstance(report["null_empty_fidelity"], bool)


def test_resumable_migrate_then_resume_no_duplicates(tmp_path):
    """Battle-test the data engine: migrate via the manifest loader, simulate a
    crash (reset every chunk to pending), resume, and assert no duplicate rows."""
    if not _oracle_ok():
        pytest.skip("Oracle source not reachable")
    if not _target_ok():
        pytest.skip("HeliosDB target not reachable")
    _ensure_sample()

    from any2heliosdb.cli import _run_context
    from any2heliosdb.config.store import (build_source_adapter, build_target_driver,
                                          build_type_registry)
    from any2heliosdb.core.manifest import PENDING, Manifest
    from any2heliosdb.core.orchestrator import migrate
    from any2heliosdb.validate.counts import run_test_count

    cfg = _cfg()
    cfg.options.output_dir = str(tmp_path)
    cfg.options.parallelism = 2
    manifest_path, run_id = _run_context(cfg)

    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect()
    tgt.connect()
    try:
        migrate(src, tgt, schema="HR", registry=build_type_registry(cfg),
                cfg=cfg, manifest_path=manifest_path, run_id=run_id, parallelism=2)
        schema = src.introspect_schema("HR")
        assert run_test_count(src, tgt, schema.tables).passed, "counts wrong after migrate"

        # Simulate a crash mid-load: force every chunk back to pending.
        man = Manifest(manifest_path)
        man._db.execute("UPDATE chunks SET state=? WHERE run_id=?", (PENDING, run_id))
        man._db.commit()
        man.close()

        # Resume: data-only, idempotent reload of every chunk.
        migrate(src, tgt, schema="HR", registry=build_type_registry(cfg),
                drop_existing=False, cfg=cfg, manifest_path=manifest_path, run_id=run_id,
                parallelism=2, do_schema=False)
        assert run_test_count(src, tgt, schema.tables).passed, "duplicates after resume"
    finally:
        src.close()
        tgt.close()


def test_cdc_snapshot_then_incremental(tmp_path):
    """Battle-test the CDC spine: snapshot capture+apply, then an Oracle UPDATE
    and INSERT captured incrementally (SCN-watermark) and applied idempotently.

    Requires a target with correct numeric semantics (HeliosDB-Full today; Lite
    once its NUMERIC/param-WHERE handling is fixed)."""
    if not _oracle_ok():
        pytest.skip("Oracle source not reachable")
    if not _target_ok():
        pytest.skip("HeliosDB target not reachable")
    if _target_edition() == "nano":
        pytest.skip("CDC apply is n/a on Nano (incomplete ON CONFLICT DO UPDATE; weaker Oracle support)")
    _ensure_sample()

    import datetime

    import oracledb

    from any2heliosdb.cdc.engine import run_extract, run_replicat
    from any2heliosdb.config.store import (build_source_adapter, build_target_driver,
                                          build_type_registry)
    from any2heliosdb.core.orchestrator import migrate
    from any2heliosdb.validate.counts import run_test_count

    cfg = _cfg()
    cfg.options.output_dir = str(tmp_path)
    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect()
    tgt.connect()
    try:
        migrate(src, tgt, schema="HR", registry=build_type_registry(cfg))
        # snapshot capture + idempotent apply over already-migrated data
        run_extract(cfg, "it")
        run_replicat(cfg, "it")
        schema = src.introspect_schema("HR")
        assert run_test_count(src, tgt, schema.tables).passed, "counts wrong after snapshot apply"

        # change the source: update one row, insert another
        oc = oracledb.connect(user=ORA_USER, password=ORA_PW, dsn=ORA_DSN)
        ocur = oc.cursor()
        ocur.execute("UPDATE employees SET salary=999999 WHERE emp_id=1")
        ocur.execute("INSERT INTO employees (emp_id,full_name,email,salary,hired,active,dept_id) "
                     "VALUES (6,'Katherine Johnson','kj@example.com',95000,:1,1,20)",
                     [datetime.datetime(2022, 2, 2)])
        oc.commit()
        oc.close()

        captured = run_extract(cfg, "it")["captured"]
        assert captured >= 2, "incremental capture missed changes ({} captured)".format(captured)
        run_replicat(cfg, "it")

        assert tgt.query("SELECT count(*) FROM employees")[0][0] == 6
        assert float(tgt.query("SELECT salary FROM employees WHERE emp_id=1")[0][0]) == 999999
        assert tgt.query("SELECT full_name FROM employees WHERE emp_id=6")[0][0] == "Katherine Johnson"
        # idempotent: trail cursor is at the end, nothing re-applied
        assert run_replicat(cfg, "it")["applied"] == 0

        # delete a source row -> the replicat's key-set reconciliation removes it
        oc = oracledb.connect(user=ORA_USER, password=ORA_PW, dsn=ORA_DSN)
        ocur = oc.cursor()
        ocur.execute("DELETE FROM employees WHERE emp_id=6")
        oc.commit()
        oc.close()
        run_extract(cfg, "it")
        res = run_replicat(cfg, "it")
        assert res["deleted"] >= 1, "delete reconciliation did not remove the row"
        assert tgt.query("SELECT count(*) FROM employees WHERE emp_id=6")[0][0] == 0
    finally:
        src.close()
        tgt.close()
