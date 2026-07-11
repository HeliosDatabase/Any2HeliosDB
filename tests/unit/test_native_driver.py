"""Hermetic tests for the native (Oracle-wire) driver's SQL generation.

A fake connection records execute/executemany calls so we can assert the Oracle
dialect (quoted upper-case identifiers, :N binds, DELETE-then-INSERT) without a
live Oracle listener. The end-to-end parity battle-test runs separately against
an Oracle-listener HeliosDB build."""
import pytest

from any2heliosdb.target.base import TargetDsn
from any2heliosdb.target.native_driver import NativeOracleDriver


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = [("EMP_ID",), ("FULL_NAME",)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.log.append(("execute", sql, list(params) if params else []))

    def executemany(self, sql, rows):
        self.conn.log.append(("executemany", sql, [tuple(r) for r in rows]))

    def fetchall(self):
        return []


class FakeConn:
    version = ""

    def __init__(self):
        self.log = []
        self.autocommit = True
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _drv():
    d = NativeOracleDriver(TargetDsn(host="h", port=1521, dbname="XEPDB1", user="hr"))
    d._conn = FakeConn()
    return d


def test_insert_rows_uses_oracle_binds_and_quoting():
    d = _drv()
    n = d.insert_rows("EMPLOYEES", ["EMP_ID", "FULL_NAME"], [(1, "Ada"), (2, "Alan")])
    assert n == 2
    kind, sql, rows = d._conn.log[-1]
    assert kind == "executemany"
    assert sql == 'INSERT INTO "EMPLOYEES" ("EMP_ID", "FULL_NAME") VALUES (:1, :2)'
    assert rows == [(1, "Ada"), (2, "Alan")]


def test_load_range_deletes_then_inserts():
    d = _drv()
    d.load_range("EMPLOYEES", ["EMP_ID", "FULL_NAME"], [(1, "Ada")],
                 where='"EMP_ID" >= 1 AND "EMP_ID" < 3')
    ops = [(k, s) for k, s, _ in d._conn.log]
    assert ops[0] == ("execute", 'DELETE FROM "EMPLOYEES" WHERE "EMP_ID" >= 1 AND "EMP_ID" < 3')
    assert ops[1][0] == "executemany" and ops[1][1].startswith('INSERT INTO "EMPLOYEES"')


def test_upsert_dedups_by_key_last_wins():
    d = _drv()
    n = d.upsert("EMPLOYEES", ["EMP_ID"], ["EMP_ID", "FULL_NAME"],
                 [(1, "old"), (1, "new"), (2, "x")])
    assert n == 2  # deduped by key
    log = d._conn.log
    dk = [e for e in log if e[0] == "executemany" and "DELETE" in e[1]][0]
    assert dk[1] == 'DELETE FROM "EMPLOYEES" WHERE "EMP_ID" = :1'
    assert dk[2] == [(1,), (2,)]
    ins = [e for e in log if e[0] == "executemany" and "INSERT" in e[1]][0]
    assert ins[2] == [(1, "new"), (2, "x")]  # last write for key 1 wins


class _RowcountCursor(FakeCursor):
    """FakeCursor that exposes a settable rowcount and a no-op setinputsizes, so
    update_columns (which reads cur.rowcount after each single-row execute) works
    without a live Oracle listener."""

    def __init__(self, conn, rc=1):
        super().__init__(conn)
        self.rowcount = rc

    def setinputsizes(self, *a, **k):
        pass


def test_update_columns_sets_only_provided_columns_oracle_style():
    # F1: a partial (unchanged-TOAST) image applies as UPDATE SET <provided> WHERE
    # <key>. The omitted column (BODY) is never named, so the DELETE+INSERT upsert
    # that would NULL it is avoided — the stored value survives.
    d = _drv()
    d._conn.cursor = lambda: _RowcountCursor(d._conn, rc=1)
    n = d.update_columns("DOC", ["ID"], ["V"], [("new-v", 5)])
    assert n == 1
    kind, sql, params = d._conn.log[-1]
    assert kind == "execute"
    assert sql == 'UPDATE "DOC" SET "V" = :1 WHERE "ID" = :2'
    assert params == ["new-v", 5]
    assert '"BODY"' not in sql
    assert d._conn.commits == 1


def test_update_columns_can_move_primary_key_in_one_statement():
    # F2: a PK-changing UPDATE moves the row in place — SET the new key + provided
    # columns WHERE the OLD key — instead of deleting the parent row first.
    d = _drv()
    d._conn.cursor = lambda: _RowcountCursor(d._conn, rc=1)
    n = d.update_columns("DOC", ["ID"], ["ID", "V"], [(12, "moved", 11)])
    assert n == 1
    _, sql, params = d._conn.log[-1]
    assert sql == 'UPDATE "DOC" SET "ID" = :1, "V" = :2 WHERE "ID" = :3'
    assert params == [12, "moved", 11]


def test_update_columns_returns_zero_when_no_row_matches():
    # rowcount==0 signals the replicat to fall back to an insert.
    d = _drv()
    d._conn.cursor = lambda: _RowcountCursor(d._conn, rc=0)
    assert d.update_columns("DOC", ["ID"], ["V"], [("x", 99)]) == 0


def test_delete_keys_oracle_binds():
    d = _drv()
    n = d.delete_keys("EMPLOYEES", ["EMP_ID"], [(1,), (2,)])
    assert n == 2
    kind, sql, rows = d._conn.log[-1]
    assert sql == 'DELETE FROM "EMPLOYEES" WHERE "EMP_ID" = :1' and rows == [(1,), (2,)]


def test_load_range_atomic_commits_once_and_restores_autocommit():
    d = _drv()
    assert d._conn.autocommit is True
    d.load_range("EMPLOYEES", ["EMP_ID", "FULL_NAME"], [(1, "Ada")],
                 where='"EMP_ID" >= 1 AND "EMP_ID" < 3')
    # DELETE+INSERT committed exactly once, never rolled back, autocommit restored.
    assert d._conn.commits == 1
    assert d._conn.rollbacks == 0
    assert d._conn.autocommit is True


def test_load_range_rolls_back_and_does_not_commit_on_insert_failure():
    d = _drv()

    class BoomCursor(FakeCursor):
        def executemany(self, sql, rows):  # INSERT blows up after the DELETE
            raise RuntimeError("insert failed")

    d._conn.cursor = lambda: BoomCursor(d._conn)
    with pytest.raises(RuntimeError, match="insert failed"):
        d.load_range("EMPLOYEES", ["EMP_ID", "FULL_NAME"], [(1, "Ada")], where='"EMP_ID" >= 1')
    # No commit (DELETE must not survive), the transaction was rolled back,
    # and the connection's autocommit state is restored for the next op.
    assert d._conn.commits == 0
    assert d._conn.rollbacks == 1
    assert d._conn.autocommit is True


def test_upsert_atomic_commits_once_and_restores_autocommit():
    d = _drv()
    d.upsert("EMPLOYEES", ["EMP_ID"], ["EMP_ID", "FULL_NAME"], [(1, "new"), (2, "x")])
    assert d._conn.commits == 1
    assert d._conn.rollbacks == 0
    assert d._conn.autocommit is True


def test_upsert_rolls_back_on_insert_failure():
    d = _drv()
    calls = {"n": 0}

    class BoomOnInsertCursor(FakeCursor):
        def executemany(self, sql, rows):
            calls["n"] += 1
            if "INSERT" in sql:  # let the DELETE through, fail the re-insert
                raise RuntimeError("insert failed")
            super().executemany(sql, rows)

    d._conn.cursor = lambda: BoomOnInsertCursor(d._conn)
    with pytest.raises(RuntimeError, match="insert failed"):
        d.upsert("EMPLOYEES", ["EMP_ID"], ["EMP_ID", "FULL_NAME"], [(1, "new")])
    assert d._conn.commits == 0
    assert d._conn.rollbacks == 1
    assert d._conn.autocommit is True


def test_describe_columns_from_cursor_description():
    d = _drv()
    assert d.describe_columns("EMPLOYEES") == ["EMP_ID", "FULL_NAME"]


def test_copy_rows_unsupported_on_native():
    d = _drv()
    with pytest.raises(NotImplementedError):
        d.copy_rows("T", ["a"], [(1,)])


def test_probe_capabilities_reflects_oracle_surface():
    d = _drv()
    d._conn.version = "HeliosDB 14.0"
    caps = d.probe_capabilities()
    assert caps.copy_from_stdin is False
    assert caps.merge is True and caps.returning is True
