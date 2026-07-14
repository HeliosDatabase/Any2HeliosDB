"""PsycopgDriver write paths — the primary data-load and CDC-apply primitives.

Hermetic: a fake psycopg connection/cursor seam (mirrors the SQL-interpreting fake
in ``test_psycopg_delete_keys.py``) captures the *rendered* SQL and the returned
counts, with no live server. The literal-SQL apply paths (``upsert`` /
``update_columns`` / ``delete_keys``) build ``psycopg.sql`` Composables, which the
fake renders via ``as_string()`` with NO connection context — psycopg's fallback
encoder, not the exact bytes a connected dumper would put on the wire (e.g. bytes
render as an octal-escaped ``E'..'::bytea`` literal instead of the connected hex
form). The assertions therefore pin the statement SHAPE the driver emits —
identifier quoting, literal escaping (quotes / NULs / bytea / NULL), and
ON CONFLICT column subsets — not byte-exact server SQL.

The literal-SQL choice is the driver's documented HeliosDB-Lite portability
guarantee (see ``PsycopgDriver.upsert``): Lite ignores ON CONFLICT / a
parameterized WHERE when the values are bind params, so the apply SQL must carry
its values as inline literals, NOT ``%s`` placeholders. The upsert/update/delete
tests below assert precisely that (no bind params passed alongside the statement).
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal

from any2heliosdb.target.base import TargetDsn
from any2heliosdb.target.psycopg_driver import PsycopgDriver


def _render(q):
    """A real psycopg cursor renders a Composable to wire SQL; do the same."""
    return q.as_string() if hasattr(q, "as_string") else q


class _FakeCursor:
    def __init__(self, rowcounts=None):
        self.sql = []          # rendered statement per execute()
        self.params = []       # bind params per execute() (None on the literal path)
        self.copies = []       # rendered COPY statements
        self.written = []      # rows handed to write_row()
        self._rc = list(rowcounts or [])
        self._i = 0
        self.rowcount = 0
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self.sql.append(_render(query))
        self.params.append(params)
        # Default rowcount 1 (row matched) unless the test scripted a sequence.
        self.rowcount = self._rc[self._i] if self._i < len(self._rc) else 1
        self._i += 1

    def copy(self, stmt):
        self.copies.append(_render(stmt))
        outer = self

        class _CP:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def write_row(self, row):
                outer.written.append(tuple(row))

        return _CP()

    def fetchall(self):
        return []


class _FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rowcounts=None):
        self._cur = _FakeCursor(rowcounts)
        self.autocommit = True

    def transaction(self):
        return _FakeTxn()

    def cursor(self):
        return self._cur


def _driver(rowcounts=None):
    d = PsycopgDriver(TargetDsn())
    d._conn = _FakeConn(rowcounts)  # inject fake; skip real connect()
    return d


def _cur(d):
    return d._conn._cur


# --- copy_rows ---------------------------------------------------------------
def test_copy_rows_renders_copy_stdin_and_counts_rows():
    d = _driver()
    n = d.copy_rows("hr.emp", ["id", "name"], [(1, "a"), (2, "b")])
    assert n == 2
    # schema-qualified table quoted per-part (bare here); columns via quote_ident.
    assert _cur(d).copies == ["COPY hr.emp (id, name) FROM STDIN"]
    assert _cur(d).written == [(1, "a"), (2, "b")]


def test_copy_rows_quotes_reserved_identifiers():
    d = _driver()
    d.copy_rows("order", ["select", "id"], [(1, 2)])
    # "order"/"select" are reserved -> double-quoted; "id" stays bare.
    assert _cur(d).copies == ['COPY "order" ("select", id) FROM STDIN']


# --- load_range --------------------------------------------------------------
def test_load_range_copy_path_deletes_then_copies():
    d = _driver()
    n = d.load_range("emp", ["id", "val"], [(1, "x")], where="id >= 1 AND id < 26")
    assert n == 1
    # DELETE of the chunk range, then COPY the replacement rows — one transaction.
    assert _cur(d).sql == ["DELETE FROM emp WHERE id >= 1 AND id < 26"]
    assert _cur(d).copies == ["COPY emp (id, val) FROM STDIN"]
    assert _cur(d).written == [(1, "x")]


def test_load_range_without_where_deletes_whole_table():
    d = _driver()
    d.load_range("emp", ["id"], [(1,)])
    assert _cur(d).sql == ["DELETE FROM emp"]


def test_load_range_insert_fallback_binds_per_row():
    d = _driver()
    n = d.load_range("emp", ["id", "val"], [(1, "x"), (2, "y")], where="id < 26",
                     use_copy=False)
    assert n == 2
    # DELETE (no params) then one parameterized INSERT per row (no pipeline).
    assert _cur(d).sql == [
        "DELETE FROM emp WHERE id < 26",
        "INSERT INTO emp (id, val) VALUES (%s, %s)",
        "INSERT INTO emp (id, val) VALUES (%s, %s)",
    ]
    assert _cur(d).params == [None, (1, "x"), (2, "y")]


# --- insert_rows -------------------------------------------------------------
def test_insert_rows_quotes_reserved_cols_and_counts():
    d = _driver()
    n = d.insert_rows("select", ["id", "order"], [(1, 2), (3, 4)],
                      on_conflict_do_nothing=True)
    assert n == 2
    stmt = 'INSERT INTO "select" (id, "order") VALUES (%s, %s) ON CONFLICT DO NOTHING'
    assert _cur(d).sql == [stmt, stmt]     # one execute per row
    assert _cur(d).params == [(1, 2), (3, 4)]


def test_insert_rows_without_on_conflict_omits_clause():
    d = _driver()
    d.insert_rows("emp", ["id"], [(1,)])
    assert _cur(d).sql == ["INSERT INTO emp (id) VALUES (%s)"]


def test_insert_rows_coerces_datetime_decimal_to_text_params():
    # _coerce_param renders binary-prone types as text before binding (portable
    # across editions that can't cast a BINARY timestamp/decimal bind param).
    d = _driver()
    ts = _dt.datetime(2026, 7, 8, 9, 30, 0)
    day = _dt.date(2026, 7, 8)
    dec = Decimal("12.50")
    d.insert_rows("t", ["a", "b", "c", "d"], [(ts, day, dec, 7)])
    assert _cur(d).params == [("2026-07-08 09:30:00", "2026-07-08", "12.50", 7)]


# --- upsert (literal SQL, ON CONFLICT DO UPDATE) -----------------------------
def test_upsert_renders_literal_on_conflict_do_update_last_wins():
    d = _driver()
    # Two records for the same key within a batch: the last after-image wins.
    n = d.upsert("t", ["id"], ["id", "name"], [(1, "first"), (1, "second")])
    assert n == 1
    assert _cur(d).sql == [
        'INSERT INTO t ("id", "name") VALUES (1, \'second\') '
        'ON CONFLICT ("id") DO UPDATE SET "name" = EXCLUDED."name"'
    ]
    # Lite-safe: values are inline literals, NOT %s bind params.
    assert _cur(d).params == [None]


def test_upsert_escapes_embedded_single_quote():
    d = _driver()
    d.upsert("t", ["id"], ["id", "name"], [(1, "O'Brien")])
    # single quote doubled inside a literal
    assert "VALUES (1, 'O''Brien')" in _cur(d).sql[0]


def test_upsert_renders_bytea_and_null_literals():
    d = _driver()
    d.upsert("t", ["id"], ["id", "b"], [(1, b"ab\x00c"), (2, None)])
    sql1, sql2 = _cur(d).sql
    # bytes -> a bytea literal (NUL survives); None -> the SQL NULL keyword.
    assert "::bytea" in sql1
    assert "VALUES (2, NULL)" in sql2


def test_upsert_all_key_columns_uses_do_nothing():
    d = _driver()
    d.upsert("t", ["id", "sub"], ["id", "sub"], [(1, 2)])
    assert _cur(d).sql == [
        'INSERT INTO t ("id", "sub") VALUES (1, 2) ON CONFLICT ("id", "sub") DO NOTHING'
    ]


def test_upsert_empty_is_noop():
    d = _driver()
    assert d.upsert("t", ["id"], ["id", "name"], []) == 0
    assert _cur(d).sql == []


# --- update_columns (literal SQL, keyed column subset) -----------------------
def test_update_columns_renders_literal_set_where_and_counts_matches():
    # rowcounts [1, 0]: first row matched, second matched nothing.
    d = _driver([1, 0])
    n = d.update_columns("emp", ["id"], ["name", "age"],
                         [("Al", 30, 1), ("Bo", 40, 2)])
    assert n == 1  # summed matched rowcount, not rows attempted
    assert _cur(d).sql == [
        'UPDATE emp SET "name" = \'Al\', "age" = 30 WHERE "id" = 1',
        'UPDATE emp SET "name" = \'Bo\', "age" = 40 WHERE "id" = 2',
    ]
    assert _cur(d).params == [None, None]  # literal path, no binds


def test_update_columns_empty_or_no_setcols_is_noop():
    d = _driver()
    assert d.update_columns("emp", ["id"], ["name"], []) == 0
    assert d.update_columns("emp", ["id"], [], [(1,)]) == 0
    assert _cur(d).sql == []


def test_update_columns_rowcount_minus_one_counts_zero():
    # A server that reports no tag count (psycopg rowcount == -1) must clamp to
    # 0 matched — never propagate -1 into the CDC existence-probe arithmetic
    # (a negative count would read as "row absent" AND corrupt summed totals).
    d = _driver([-1])
    assert d.update_columns("emp", ["id"], ["name"], [("Al", 1)]) == 0


# --- delete_keys (literal SQL) — SQL shape (counts covered in test_psycopg_delete_keys) -
def test_delete_keys_renders_literal_composite_where():
    d = _driver([1, 1])
    n = d.delete_keys("t", ["a", "b"], [(1, 2), (3, 4)])
    assert n == 2
    assert _cur(d).sql == [
        'DELETE FROM t WHERE "a" = 1 AND "b" = 2',
        'DELETE FROM t WHERE "a" = 3 AND "b" = 4',
    ]
    assert _cur(d).params == [None, None]


def test_delete_keys_rowcount_minus_one_counts_zero():
    # rowcount == -1 (no tag count) on every DELETE -> report 0, not -1 or -2.
    d = _driver([-1, -1])
    assert d.delete_keys("t", ["a"], [(1,), (2,)]) == 0


# --- ping / truncate ---------------------------------------------------------
def test_ping_issues_select_one():
    d = _driver()
    d.ping()
    assert _cur(d).sql == ["SELECT 1"]


def test_truncate_renders_truncate_table():
    d = _driver()
    d.truncate("hr.emp")
    assert _cur(d).sql == ["TRUNCATE TABLE hr.emp"]


# --- per-source-transaction composition (P2 slice 3) -------------------------


class _TxnConn(_FakeConn):
    """Fake conn that tracks commit/rollback and how often ``transaction()`` is
    entered — so we can prove the apply-seam methods join the OUTER replicat
    transaction (no nested committing scope) while ``begin()`` is in effect."""

    def __init__(self, rowcounts=None):
        super().__init__(rowcounts)
        self.commits = 0
        self.rollbacks = 0
        self.tx_entered = 0

    def transaction(self):
        outer = self

        class _TX:
            def __enter__(self):
                outer.tx_entered += 1
                return self

            def __exit__(self, *a):
                return False

        return _TX()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _txn_driver(rowcounts=None):
    d = PsycopgDriver(TargetDsn())
    d._conn = _TxnConn(rowcounts)
    return d


def test_begin_sets_in_txn_and_disables_autocommit():
    d = _txn_driver()
    assert d._in_txn is False and d._conn.autocommit is True
    d.begin()
    assert d._in_txn is True and d._conn.autocommit is False


def test_commit_and_rollback_clear_in_txn_and_restore_autocommit():
    d = _txn_driver()
    d.begin()
    d.commit()
    assert d._in_txn is False and d._conn.autocommit is True and d._conn.commits == 1
    d.begin()
    d.rollback()
    assert d._in_txn is False and d._conn.autocommit is True and d._conn.rollbacks == 1


def test_apply_seam_joins_outer_txn_without_nested_scope():
    # Inside begin()..commit() the apply-seam calls must NOT open their own
    # committing conn.transaction() — they join the open replicat transaction and
    # commit atomically at commit().
    d = _txn_driver()
    d.begin()
    d.upsert("t", ["id"], ["id", "v"], [(1, "a")])
    d.update_columns("t", ["id"], ["v"], [("b", 1)])
    d.delete_keys("t", ["id"], [(2,)])
    assert d._conn.tx_entered == 0        # no nested committing scope while in txn
    assert d._conn.commits == 0           # nothing committed until the replicat commits
    d.commit()
    assert d._conn.commits == 1


def test_apply_seam_outside_txn_uses_own_transaction_scope():
    # The pre-slice-3 path is unchanged: each apply-seam call opens its own
    # autocommitting conn.transaction().
    d = _txn_driver()
    d.upsert("t", ["id"], ["id", "v"], [(1, "a")])
    d.delete_keys("t", ["id"], [(2,)])
    assert d._conn.tx_entered == 2        # one committing scope per call
    assert d._conn.commits == 0           # conn.transaction() handles its own commit
