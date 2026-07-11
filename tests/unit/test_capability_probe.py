"""Runtime capability probe (target/capability.py) — hermetic, no live server.

A policy-driven fake psycopg connection answers each probe statement: by default
every statement succeeds (a permissive target), or a ``fail(sql)`` predicate makes
specific statements raise (a strict target that rejects a constraint violation, or
a target that rejects every probe). The probe swallows per-statement failures, so
this pins:

* edition detection + version parse from the banner,
* the ``concurrent_writes`` verdict that prevents the documented Nano parallel-load
  permanent hang (edition-derived, never probed — a probe would risk the hang),
* constraint-enforcement verdicts (``enforces_check`` / ``enforces_fk`` are
  ``not <violating-insert-succeeded>``), and
* the failure fallbacks: a garbled banner -> UNKNOWN + ``concurrent_writes``
  False (safe-serial: an unrecognized target may really be a Nano/Lite, whose
  second concurrent writer BLOCKS instead of erroring — a hang the failed-chunk
  mop-up can never recover); a probe that fails every statement -> all-False
  caps without crashing.
"""
from __future__ import annotations

from any2heliosdb.constants import Edition
from any2heliosdb.target.capability import probe_capabilities


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.conn.executed.append(sql)
        if self.conn.fail and self.conn.fail(sql):
            raise RuntimeError("fake reject: " + sql[:40])

    def fetchall(self):
        return [(1,)]

    def copy(self, sql):
        self.conn.executed.append(sql)
        if self.conn.fail and self.conn.fail(sql):
            raise RuntimeError("fake copy reject")
        outer = self.conn

        class _CP:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write_row(self, row):
                outer.written.append(tuple(row))

        return _CP()


class _FakeConn:
    """*fail* is a predicate over the SQL text; matching statements raise."""

    def __init__(self, fail=None):
        self.fail = fail
        self.executed = []
        self.written = []
        self.autocommit = False  # probe must restore this exact value

    def cursor(self):
        return _FakeCursor(self)


def _strict_enforcement(sql):
    # Only the constraint-violating probe inserts are rejected.
    return "VALUES (-1)" in sql or "VALUES (1, 999)" in sql


# --- permissive target (every probe succeeds) --------------------------------
def test_permissive_postgres_target_all_features_but_no_enforcement():
    cm = probe_capabilities(_FakeConn(), "PostgreSQL 16.13 on x86_64-pc-linux-musl")
    assert cm.edition is Edition.POSTGRES
    assert cm.server_version == "PostgreSQL 16.13 on x86_64-pc-linux-musl"
    assert cm.concurrent_writes is True         # stock PG services concurrent writers
    assert cm.copy_from_stdin is True and cm.copy_binary is True
    assert cm.on_conflict and cm.returning and cm.merge
    assert cm.plpgsql_control_flow and cm.materialized_views
    assert cm.has_version_function and cm.gen_random_uuid
    # A permissive target ACCEPTS the violating inserts -> it does NOT enforce.
    assert cm.enforces_check is False and cm.enforces_fk is False


def test_strict_target_reports_constraint_enforcement():
    cm = probe_capabilities(_FakeConn(fail=_strict_enforcement), "14.0 (HeliosDB)")
    assert cm.edition is Edition.FULL
    # violating inserts rejected -> the target enforces the constraints
    assert cm.enforces_check is True and cm.enforces_fk is True
    # unrelated features still probe True (only the two violations were rejected)
    assert cm.copy_from_stdin and cm.on_conflict and cm.merge


# --- concurrent-writes verdict (the Nano hang guard) -------------------------
def test_lite_never_gets_concurrent_writes():
    cm = probe_capabilities(_FakeConn(), "17.0 (HeliosDB-Lite 2.0)")
    assert cm.edition is Edition.LITE
    assert cm.concurrent_writes is False        # Lite must serialize the load


def test_nano_concurrent_writes_gated_by_version():
    old = probe_capabilities(_FakeConn(), "16.0 (HeliosDB Nano 3.60.4)")
    new = probe_capabilities(_FakeConn(), "16.0 (HeliosDB Nano 3.60.7)")
    assert old.edition is Edition.NANO and old.concurrent_writes is False
    assert new.edition is Edition.NANO and new.concurrent_writes is True


# --- failure fallbacks -------------------------------------------------------
def test_garbled_banner_falls_back_to_unknown_and_serial_concurrency():
    # No edition marker: edition UNKNOWN, and concurrent_writes falls back to
    # False (safe-serial). If the unrecognized target is actually a Nano/Lite,
    # a second concurrent writer BLOCKS rather than errors, so the parallel pass
    # hangs permanently — and the serial mop-up only retries chunks that FAILED,
    # so it can never recover a hang. Serial-on-unknown costs speed;
    # optimistic-on-unknown costs a permanent hang.
    cm = probe_capabilities(_FakeConn(), "???not a version banner???")
    assert cm.edition is Edition.UNKNOWN
    assert cm.concurrent_writes is False


def test_probe_that_rejects_everything_returns_safe_all_false_caps():
    # A target that fails every probe (garbled/locked-down) must not crash the
    # probe; every capability defaults False, edition still parsed from the banner.
    cm = probe_capabilities(_FakeConn(fail=lambda s: True), "17.0 (HeliosDB-Lite 2.0)")
    assert cm.edition is Edition.LITE
    assert cm.concurrent_writes is False
    assert cm.copy_from_stdin is False and cm.copy_binary is False
    assert not cm.on_conflict and not cm.returning and not cm.merge
    assert not cm.plpgsql_control_flow and not cm.materialized_views
    assert cm.enforces_check is False and cm.enforces_fk is False
    assert cm.has_version_function is False


# --- bookkeeping -------------------------------------------------------------
def test_probe_restores_prior_autocommit():
    conn = _FakeConn()
    conn.autocommit = False
    probe_capabilities(conn, "PostgreSQL 16")
    assert conn.autocommit is False             # exact prior value restored


def test_accepts_dict_mirrors_probed_capabilities():
    cm = probe_capabilities(_FakeConn(), "PostgreSQL 16")
    assert cm.accepts["copy_from_stdin"] is True
    assert cm.accepts["concurrent_writes"] is True
    assert cm.accepts["version"] is cm.has_version_function
    # the enforcement verdicts flow through the accepts map too
    assert cm.accepts["enforces_check"] is False
