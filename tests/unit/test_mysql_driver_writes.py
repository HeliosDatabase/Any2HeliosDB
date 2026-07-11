"""MySQLTargetDriver write paths (the migrate-back direction).

Hermetic: a fake PyMySQL connection/cursor seam captures the rendered SQL, the
``executemany`` batches, and the returned counts — no live MySQL. The MySQL path
is not PG-wire, so the assertions pin the MySQL-dialect shape the driver actually
renders: backtick identifier quoting, array-INSERT bulk load (no COPY), and the
``INSERT ... ON DUPLICATE KEY UPDATE`` upsert merge semantics.

``update_columns`` returns rows *matched* (its CDC existence-probe contract); the
real driver guarantees that by connecting with ``CLIENT_FOUND_ROWS`` — the fake
just scripts the ``rowcount`` sequence a FOUND_ROWS connection would report.
``delete_keys`` returns the ``executemany`` total affected rowcount (rows
actually deleted, clamped to 0 when the server reports no tag count), matching
the psycopg driver's rows-actually-deleted hardening.
"""
from __future__ import annotations

import pytest

from any2heliosdb.target.base import TargetDsn
from any2heliosdb.target.mysql_driver import MySQLTargetDriver, mysql_ident


class _FakeCursor:
    def __init__(self, rowcounts=None):
        self.sql = []
        self.params = []
        self.many = []      # (sql, seq) batches from executemany
        self._rc = list(rowcounts or [])
        self._i = 0
        self.rowcount = 0
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        self.sql.append(query)
        self.params.append(params)
        self.rowcount = self._rc[self._i] if self._i < len(self._rc) else 1
        self._i += 1

    def executemany(self, query, seq):
        batch = list(seq)
        self.sql.append(query)
        self.many.append(batch)
        # PyMySQL's executemany exposes the TOTAL affected rowcount for the
        # batch; default to one-row-per-statement unless the test scripted a
        # sequence (a shortfall, or -1/None for "no tag count").
        self.rowcount = self._rc[self._i] if self._i < len(self._rc) else len(batch)
        self._i += 1

    def fetchall(self):
        return []


class _FakeConn:
    def __init__(self, rowcounts=None):
        self._cur = _FakeCursor(rowcounts)
        self.begun = 0
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cur

    def begin(self):
        self.begun += 1

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _driver(rowcounts=None):
    d = MySQLTargetDriver(TargetDsn())
    d._conn = _FakeConn(rowcounts)
    return d


def _cur(d):
    return d._conn._cur


# --- identifier quoting ------------------------------------------------------
def test_mysql_ident_backtick_quotes_and_escapes():
    assert mysql_ident("emp") == "`emp`"
    assert mysql_ident("we`ird") == "`we``ird`"  # embedded backtick doubled


# --- insert_rows -------------------------------------------------------------
def test_insert_rows_array_insert_backtick_quoted():
    d = _driver()
    n = d.insert_rows("db.emp", ["id", "name"], [(1, "a"), (2, "b")])
    assert n == 2
    # schema-qualified table quoted per-part; single array-INSERT via executemany.
    assert _cur(d).sql == ["INSERT INTO `db`.`emp` (`id`, `name`) VALUES (%s, %s)"]
    assert _cur(d).many == [[(1, "a"), (2, "b")]]


def test_insert_rows_on_conflict_uses_insert_ignore():
    d = _driver()
    d.insert_rows("emp", ["id"], [(1,)], on_conflict_do_nothing=True)
    assert _cur(d).sql == ["INSERT IGNORE INTO `emp` (`id`) VALUES (%s)"]


def test_insert_rows_empty_is_noop():
    d = _driver()
    assert d.insert_rows("emp", ["id"], []) == 0
    assert _cur(d).sql == []


def test_copy_rows_not_supported_on_mysql():
    d = _driver()
    with pytest.raises(NotImplementedError):
        d.copy_rows("emp", ["id"], [(1,)])


# --- load_range --------------------------------------------------------------
def test_load_range_deletes_then_array_inserts():
    d = _driver()
    n = d.load_range("emp", ["id", "v"], [(1, "x")], where="id < 26")
    assert n == 1
    assert _cur(d).sql == [
        "DELETE FROM `emp` WHERE id < 26",
        "INSERT INTO `emp` (`id`, `v`) VALUES (%s, %s)",
    ]
    assert _cur(d).many == [[(1, "x")]]


def test_load_range_empty_only_deletes():
    d = _driver()
    n = d.load_range("emp", ["id"], [], where="id < 26")
    assert n == 0
    assert _cur(d).sql == ["DELETE FROM `emp` WHERE id < 26"]  # no INSERT emitted


def test_load_range_failure_rolls_back_once_and_reraises():
    # The chunk transaction's exception branch: a failure mid-chunk must roll
    # back exactly once (never commit) and re-raise, so the loader records the
    # chunk as FAILED for the retry pass instead of counting it as loaded.
    d = _driver()

    def _boom(query, seq):
        _cur(d).sql.append(query)
        raise RuntimeError("insert exploded mid-chunk")

    _cur(d).executemany = _boom
    with pytest.raises(RuntimeError, match="mid-chunk"):
        d.load_range("emp", ["id"], [(1,)], where="id < 26")
    assert d._conn.begun == 1
    assert d._conn.rollbacks == 1
    assert d._conn.commits == 0
    # the range DELETE ran inside the (rolled-back) txn before the failing INSERT
    assert _cur(d).sql[0] == "DELETE FROM `emp` WHERE id < 26"


# --- upsert (ON DUPLICATE KEY UPDATE) ----------------------------------------
def test_upsert_on_duplicate_key_update_last_wins():
    d = _driver()
    # two records for key (1,) — the last after-image wins the merge.
    n = d.upsert("emp", ["id"], ["id", "name", "age"],
                 [(1, "a", 9), (1, "b", 10)])
    assert n == 1
    assert _cur(d).sql == [
        "INSERT INTO `emp` (`id`, `name`, `age`) VALUES (%s, %s, %s) "
        "ON DUPLICATE KEY UPDATE `name` = VALUES(`name`), `age` = VALUES(`age`)"
    ]
    assert _cur(d).many == [[(1, "b", 10)]]  # deduped, last-per-key


def test_upsert_all_key_columns_uses_insert_ignore():
    d = _driver()
    d.upsert("emp", ["id", "sub"], ["id", "sub"], [(1, 2)])
    # nothing to update -> just ignore duplicates
    assert _cur(d).sql == ["INSERT IGNORE INTO `emp` (`id`, `sub`) VALUES (%s, %s)"]


def test_upsert_empty_is_noop():
    d = _driver()
    assert d.upsert("emp", ["id"], ["id", "v"], []) == 0
    assert _cur(d).sql == []


# --- update_columns (matched-count contract) ---------------------------------
def test_update_columns_binds_setvals_then_keyvals_and_counts_matches():
    # rowcounts [1, 0]: FOUND_ROWS reports the first row matched, the second not.
    d = _driver([1, 0])
    n = d.update_columns("emp", ["id"], ["name", "age"],
                         [("Al", 30, 1), ("Bo", 40, 2)])
    assert n == 1  # summed rows matched, not rows attempted
    stmt = "UPDATE `emp` SET `name` = %s, `age` = %s WHERE `id` = %s"
    assert _cur(d).sql == [stmt, stmt]      # one execute per row
    # each row binds set-values first, then key-values, in SQL order
    assert _cur(d).params == [["Al", 30, 1], ["Bo", 40, 2]]


def test_update_columns_empty_or_no_setcols_is_noop():
    d = _driver()
    assert d.update_columns("emp", ["id"], ["name"], []) == 0
    assert d.update_columns("emp", ["id"], [], [(1,)]) == 0
    assert _cur(d).sql == []


# --- delete_keys (rows actually deleted, not keys attempted) ------------------
def test_delete_keys_composite_where_and_returns_rows_deleted():
    # rowcount 2: both keys matched -> the executemany affected rowcount is
    # returned (aligned with the psycopg driver's rows-actually-deleted
    # hardening); assert the rendered composite-key DELETE and the batched keys.
    d = _driver([2])
    n = d.delete_keys("emp", ["a", "b"], [(1, 2), (3, 4)])
    assert n == 2
    assert _cur(d).sql == ["DELETE FROM `emp` WHERE `a` = %s AND `b` = %s"]
    assert _cur(d).many == [[(1, 2), (3, 4)]]


def test_delete_keys_silent_noop_surfaces_as_count_shortfall():
    # A key absent on the target (silent no-op delete) must surface as
    # rowcount < len(keys) so CDC delete reconciliation can catch the shortfall
    # — not be reported as "deleted N" from keys attempted.
    d = _driver([1])
    n = d.delete_keys("emp", ["id"], [(1,), (2,), (3,)])
    assert n == 1                       # 3 keys attempted, 1 row actually deleted


@pytest.mark.parametrize("rc", [-1, None])
def test_delete_keys_missing_rowcount_reports_zero(rc):
    # -1/None means "the server reported no tag count" — report 0 rather than
    # inventing a count (and never propagate -1 into reconciliation arithmetic).
    d = _driver([rc])
    assert d.delete_keys("emp", ["id"], [(1,), (2,)]) == 0


def test_delete_keys_empty_is_noop():
    d = _driver()
    assert d.delete_keys("emp", ["id"], []) == 0
    assert _cur(d).sql == []


# --- ping / truncate ---------------------------------------------------------
def test_ping_issues_select_one():
    d = _driver()
    d.ping()
    assert _cur(d).sql == ["SELECT 1"]


def test_truncate_renders_backtick_quoted_truncate():
    d = _driver()
    d.truncate("db.emp")
    assert _cur(d).sql == ["TRUNCATE TABLE `db`.`emp`"]
