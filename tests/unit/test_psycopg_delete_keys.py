"""delete_keys must report rows ACTUALLY deleted, not keys attempted — so a
target that silently no-ops a delete surfaces as a count shortfall."""
from __future__ import annotations

from any2heliosdb.target.base import TargetDsn
from any2heliosdb.target.psycopg_driver import PsycopgDriver


class _FakeCursor:
    def __init__(self, rowcounts):
        self._rc = list(rowcounts)
        self._i = 0
        self.rowcount = 0
        self.executed = 0

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, _query):
        self.rowcount = self._rc[self._i] if self._i < len(self._rc) else 0
        self._i += 1
        self.executed += 1


class _FakeTxn:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _FakeConn:
    def __init__(self, rowcounts):
        self._cur = _FakeCursor(rowcounts)

    def transaction(self): return _FakeTxn()
    def cursor(self): return self._cur


def _driver(rowcounts):
    d = PsycopgDriver(TargetDsn())
    d._conn = _FakeConn(rowcounts)  # inject fake; skip real connect()
    return d


def test_delete_keys_returns_rows_deleted_not_attempts():
    # 3 keys attempted; the middle DELETE silently matched nothing (rowcount 0).
    d = _driver([1, 0, 1])
    assert d.delete_keys("t", ["id"], [(1,), (2,), (3,)]) == 2
    assert d._conn._cur.executed == 3  # one DELETE per key


def test_delete_keys_all_silent_noops_returns_zero():
    # The silent-target-no-op case (e.g. a build whose DELETE predicate never
    # matches): a2h must report 0, not a false 3.
    d = _driver([0, 0, 0])
    assert d.delete_keys("t", ["id"], [(1,), (2,), (3,)]) == 0


def test_delete_keys_empty_is_noop():
    d = _driver([])
    assert d.delete_keys("t", ["id"], []) == 0
    assert d._conn._cur.executed == 0
