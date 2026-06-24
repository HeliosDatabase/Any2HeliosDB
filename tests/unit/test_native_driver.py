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

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1


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


def test_delete_keys_oracle_binds():
    d = _drv()
    n = d.delete_keys("EMPLOYEES", ["EMP_ID"], [(1,), (2,)])
    assert n == 2
    kind, sql, rows = d._conn.log[-1]
    assert sql == 'DELETE FROM "EMPLOYEES" WHERE "EMP_ID" = :1' and rows == [(1,), (2,)]


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
