"""Runtime capability probe for a HeliosDB target.

Rather than assume what an edition supports, we *ask the live server* with a
battery of tiny, self-cleaning probes. The resulting :class:`CapabilityMatrix`
drives two things:

* the DDL emitters and PL/SQL rewrite layer only translate what this target
  cannot accept (so the ``native`` path is near-passthrough), and
* whatever the target lacks becomes a target-gap recommendation — the actionable
  backlog for HeliosDB Lite/Full/Nano.

Probes run in autocommit mode using uniquely-named throwaway objects that are
dropped before and after, so the probe is correct whether or not the target
implements transactional DDL or temp tables.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .base import CapabilityMatrix, detect_edition, supports_concurrent_writes

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg

_PREFIX = "_a2h_probe_"
_OBJECTS = [
    ("table", _PREFIX + "cp"),
    ("table", _PREFIX + "oc"),
    ("table", _PREFIX + "rt"),
    ("table", _PREFIX + "chk"),
    ("table", _PREFIX + "mg"),
    ("table", _PREFIX + "chld"),  # child first (FK dependency)
    ("table", _PREFIX + "par"),
    ("matview", _PREFIX + "mv"),
    ("function", _PREFIX + "fn()"),
]


def _drop_all(conn: "psycopg.Connection") -> None:
    stmts = {
        "table": "DROP TABLE IF EXISTS {} CASCADE",
        "matview": "DROP MATERIALIZED VIEW IF EXISTS {}",
        "function": "DROP FUNCTION IF EXISTS {}",
    }
    for kind, name in _OBJECTS:
        try:
            with conn.cursor() as c:
                c.execute(stmts[kind].format(name))
        except Exception:
            pass


def _probe_multi_statement_txn(conn: "psycopg.Connection") -> bool:
    """Live-probe whether *conn* services a multi-statement transaction atomically.

    Turns autocommit OFF, then: BEGIN implicitly, INSERT a row, ROLLBACK — the row
    must be GONE (proves rollback undoes an uncommitted write); then INSERT, COMMIT
    — the row must be PRESENT (proves commit lands). A target that silently
    autocommits (row survives the rollback) or drops committed data fails the probe
    and the replicat keeps the per-record keymove-barrier apply. Self-cleaning and
    autocommit-state-restoring; any exception -> ``False`` (fail safe, not closed:
    the fallback path is always correct, just per-record).
    """
    tbl = _PREFIX + "txn"
    prev_autocommit = conn.autocommit
    try:
        conn.autocommit = True
        with conn.cursor() as c:
            c.execute("DROP TABLE IF EXISTS {}".format(tbl))
            c.execute("CREATE TABLE {} (n int)".format(tbl))
        conn.autocommit = False
        # (1) a rolled-back write must vanish.
        with conn.cursor() as c:
            c.execute("INSERT INTO {} VALUES (1)".format(tbl))
        conn.rollback()
        with conn.cursor() as c:
            c.execute("SELECT count(*) FROM {}".format(tbl))
            row = c.fetchone()
            after_rollback = row[0] if row else None
        # (2) a committed write must land.
        with conn.cursor() as c:
            c.execute("INSERT INTO {} VALUES (2)".format(tbl))
        conn.commit()
        with conn.cursor() as c:
            c.execute("SELECT count(*) FROM {}".format(tbl))
            row = c.fetchone()
            after_commit = row[0] if row else None
        return after_rollback == 0 and after_commit == 1
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            conn.autocommit = True
            with conn.cursor() as c:
                c.execute("DROP TABLE IF EXISTS {}".format(tbl))
        except Exception:
            pass
        conn.autocommit = prev_autocommit


def probe_capabilities(conn: "psycopg.Connection", banner: str) -> CapabilityMatrix:
    """Probe *conn* (a psycopg connection) and return its capability matrix."""
    cm = CapabilityMatrix(raw_banner=banner)
    cm.server_version = banner
    cm.edition = detect_edition(banner)
    # Concurrent write transactions: the Apache editions (Nano/Lite) block rather
    # than error on a second concurrent writer, so probing for it would risk the
    # hang itself. Derive it from the edition (the one capability we don't probe).
    cm.concurrent_writes = supports_concurrent_writes(cm.edition, cm.server_version)

    prev_autocommit = conn.autocommit
    conn.autocommit = True

    def attempt(sql: str, fetch: bool = False) -> bool:
        try:
            with conn.cursor() as c:
                c.execute(sql)
                if fetch:
                    c.fetchall()
            return True
        except Exception:
            return False

    def copy_attempt(create_sql: str, copy_sql: str) -> bool:
        try:
            with conn.cursor() as c:
                c.execute(create_sql)
                with c.copy(copy_sql) as cp:
                    cp.write_row((1, "x"))
            return True
        except Exception:
            return False

    try:
        _drop_all(conn)

        # Scalar / system functions
        cm.has_version_function = attempt("SELECT version()", fetch=True)
        cm.gen_random_uuid = attempt("SELECT gen_random_uuid()", fetch=True)

        # COPY FROM STDIN (text) — the bulk fast path
        cm.copy_from_stdin = copy_attempt(
            "CREATE TABLE {}cp (n int, s text)".format(_PREFIX),
            "COPY {}cp (n, s) FROM STDIN".format(_PREFIX),
        )
        if cm.copy_from_stdin:
            # Binary COPY (expected unsupported on HeliosDB today)
            try:
                with conn.cursor() as c:
                    c.execute("DROP TABLE IF EXISTS {}cp".format(_PREFIX))
                    c.execute("CREATE TABLE {}cp (n int, s text)".format(_PREFIX))
                    with c.copy("COPY {}cp (n, s) FROM STDIN WITH (FORMAT binary)".format(_PREFIX)) as cp:
                        cp.write_row((1, "x"))
                cm.copy_binary = True
            except Exception:
                cm.copy_binary = False
        attempt("DROP TABLE IF EXISTS {}cp".format(_PREFIX))

        # ON CONFLICT upsert
        if attempt("CREATE TABLE {}oc (id int PRIMARY KEY)".format(_PREFIX)):
            cm.on_conflict = attempt(
                "INSERT INTO {}oc VALUES (1) ON CONFLICT DO NOTHING".format(_PREFIX)
            )
        attempt("DROP TABLE IF EXISTS {}oc".format(_PREFIX))

        # RETURNING
        if attempt("CREATE TABLE {}rt (id int)".format(_PREFIX)):
            cm.returning = attempt(
                "INSERT INTO {}rt VALUES (1) RETURNING id".format(_PREFIX), fetch=True
            )
        attempt("DROP TABLE IF EXISTS {}rt".format(_PREFIX))

        # MERGE
        if attempt("CREATE TABLE {}mg (id int PRIMARY KEY, v int)".format(_PREFIX)):
            cm.merge = attempt(
                "MERGE INTO {0}mg t USING (SELECT 1 AS id, 1 AS v) s ON (t.id = s.id) "
                "WHEN NOT MATCHED THEN INSERT (id, v) VALUES (s.id, s.v)".format(_PREFIX)
            )
        attempt("DROP TABLE IF EXISTS {}mg".format(_PREFIX))

        # PL/pgSQL control flow
        plpgsql_def = (
            "CREATE FUNCTION {}fn() RETURNS int LANGUAGE plpgsql AS "
            "$$ DECLARE x int := 0; BEGIN FOR i IN 1..3 LOOP x := x + i; END LOOP; "
            "RETURN x; END $$".format(_PREFIX)
        )
        if attempt(plpgsql_def):
            cm.plpgsql_control_flow = attempt("SELECT {}fn()".format(_PREFIX), fetch=True)
        attempt("DROP FUNCTION IF EXISTS {}fn()".format(_PREFIX))

        # Materialized views
        if attempt("CREATE MATERIALIZED VIEW {}mv AS SELECT 1 AS one".format(_PREFIX)):
            cm.materialized_views = True
        attempt("DROP MATERIALIZED VIEW IF EXISTS {}mv".format(_PREFIX))

        # Multi-statement transaction atomicity: the CDC replicat's per-source-txn
        # atomic apply gates on this. A rolled-back write MUST leave no row, and a
        # committed write MUST land — probed live (the Apache editions' PG-wire
        # BEGIN/COMMIT is real, but we never assume it; a target that silently
        # autocommits or ignores ROLLBACK stays on the per-record apply path). The
        # probe self-manages autocommit internally (saves the current mode, flips it
        # off for the BEGIN/ROLLBACK/COMMIT sequence, restores it), so it is correct
        # regardless of the autocommit state at this call site.
        cm.multi_statement_txn = _probe_multi_statement_txn(conn)

        # CHECK enforcement: a violating insert must be rejected
        if attempt("CREATE TABLE {}chk (n int CHECK (n > 0))".format(_PREFIX)):
            inserted_bad = attempt("INSERT INTO {}chk VALUES (-1)".format(_PREFIX))
            cm.enforces_check = not inserted_bad
        attempt("DROP TABLE IF EXISTS {}chk".format(_PREFIX))

        # FK enforcement: an orphan child insert must be rejected
        par_ok = attempt("CREATE TABLE {}par (id int PRIMARY KEY)".format(_PREFIX))
        chld_ok = attempt(
            "CREATE TABLE {0}chld (id int, pid int REFERENCES {0}par(id))".format(_PREFIX)
        )
        if par_ok and chld_ok:
            inserted_orphan = attempt("INSERT INTO {}chld VALUES (1, 999)".format(_PREFIX))
            cm.enforces_fk = not inserted_orphan
        attempt("DROP TABLE IF EXISTS {}chld CASCADE".format(_PREFIX))
        attempt("DROP TABLE IF EXISTS {}par CASCADE".format(_PREFIX))

        _drop_all(conn)
    finally:
        conn.autocommit = prev_autocommit

    cm.accepts = {
        "version": cm.has_version_function,
        "gen_random_uuid": cm.gen_random_uuid,
        "copy_from_stdin": cm.copy_from_stdin,
        "copy_binary": cm.copy_binary,
        "concurrent_writes": cm.concurrent_writes,
        "multi_statement_txn": cm.multi_statement_txn,
        "on_conflict": cm.on_conflict,
        "returning": cm.returning,
        "merge": cm.merge,
        "plpgsql_control_flow": cm.plpgsql_control_flow,
        "materialized_views": cm.materialized_views,
        "enforces_check": cm.enforces_check,
        "enforces_fk": cm.enforces_fk,
    }
    return cm
