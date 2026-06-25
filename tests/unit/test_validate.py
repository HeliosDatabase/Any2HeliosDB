"""Unit tests for the VALIDATION layer (TEST / TEST_COUNT / TEST_DATA).

No live database: a tiny in-memory FakeSource (fixed counts + fixed rows) and a
FakeTarget (answers count/describe/SELECT from dicts) stand in for the real
SourceAdapter / TargetDriver. Target relations are keyed by the *unqualified,
lowercased* name the migration actually loads into (``Table.target_name()``) —
the same single source of truth the loader and validators use, so the fakes
cannot drift from production behavior.
"""
from any2heliosdb.constants import Severity
from any2heliosdb.core.catalog_model import (
    Column, DataType, ForeignKey, PrimaryKey, Schema, Table)
from any2heliosdb.validate import (
    ValidationResult,
    ValidationType,
    row_checksum,
    run_test,
    run_test_count,
    run_test_data,
)


# --- IR fixtures ------------------------------------------------------------
def _emp_table() -> Table:
    return Table(
        name="EMP",
        schema="HR",
        columns=[
            Column("ID", DataType.decimal(10, 0), nullable=False),
            Column("NAME", DataType.varchar(50)),
        ],
        primary_key=PrimaryKey(columns=["ID"]),
    )


def _nopk_table() -> Table:
    return Table(name="LOG", schema="HR", columns=[Column("MSG", DataType.varchar(50))])


# --- Fakes (no DB) ----------------------------------------------------------
class FakeSource:
    """Stand-in SourceAdapter: fixed row counts and fixed streamed rows."""

    def __init__(self, counts=None, rows=None):
        self._counts = counts or {}
        self._rows = rows or {}

    def exact_row_count(self, table):
        return self._counts[table.name]

    def stream_rows(self, table, columns, where=None, arraysize=1000):
        for r in self._rows.get(table.name, []):
            yield r


class FakeTarget:
    """Stand-in TargetDriver keyed by the unqualified lowercased target name.

    ``counts`` -> count(*) per relation; ``columns`` -> the column-name list per
    relation (absence => the relation does not exist, like a real driver);
    ``select_rows`` -> rows returned for the TEST_DATA SELECT.
    """

    def __init__(self, counts=None, columns=None, select_rows=None):
        self._counts = counts or {}
        self._columns = columns or {}
        self._select_rows = select_rows or {}

    def query(self, sql, params=None):
        s = sql.lower()
        if s.startswith("select count(*)"):
            rel = s.split(" from ")[1].strip()
            return [(self._counts.get(rel, 0),)]
        rel = s.split(" from ")[1].split(" order by ")[0].strip()
        return list(self._select_rows.get(rel, []))

    def describe_columns(self, target_table):
        if target_table not in self._columns:
            raise RuntimeError("relation {!r} does not exist".format(target_table))
        return list(self._columns[target_table])


# --- ValidationResult.passed logic ------------------------------------------
def test_passed_is_true_with_no_errors():
    r = ValidationResult(validation_type=ValidationType.TEST_COUNT)
    assert r.passed is True


def test_passed_true_with_only_nonblocker_findings():
    r = ValidationResult(validation_type=ValidationType.TEST_DATA)
    r.add_error(Severity.COSMETIC, "hr.log", "no primary key; skipped")
    r.add_error(Severity.DEGRADED, "hr.log", "heuristic only")
    assert r.passed is True
    assert len(r.errors) == 2


# --- TEST_INDEX (FK-index sanity) -------------------------------------------
def _fk_table() -> Table:
    return Table(
        name="FC", schema="HR",
        columns=[Column("FILM_ID", DataType.decimal(10, 0)),
                 Column("CAT_ID", DataType.decimal(10, 0))],
        primary_key=PrimaryKey(columns=["FILM_ID", "CAT_ID"]),
        foreign_keys=[ForeignKey(columns=["CAT_ID"], references_table="CAT",
                                 references_columns=["ID"])],
    )


class _IndexProbeTarget:
    """Fake target for the GROUP-BY-ground-truth FK-index check.

    ``truth`` = the GROUP BY result rows ``[(value, count), ...]``;
    ``indexed`` = the count the index-eligible equality lookup returns.
    """

    def __init__(self, truth, indexed=0):
        self._truth = truth
        self._indexed = indexed
        self.queries = []

    def query(self, sql, params=None):
        self.queries.append(sql)
        if "group by" in sql.lower():
            return list(self._truth)
        return [(self._indexed,)]


def test_test_index_passes_when_index_matches_group_by_truth():
    from any2heliosdb.validate.data import run_test_index

    tgt = _IndexProbeTarget(truth=[(10, 3), (20, 2)], indexed=3)   # matches the most-common value
    res = run_test_index(tgt, _fk_table())
    assert res.passed
    assert res.metrics["fk_columns_checked"] == 1
    assert res.metrics["mismatches"] == 0
    # ground truth comes from a GROUP BY (portable; no cast / nested subquery)
    assert any("group by" in q.lower() and "cat_id" in q.lower() for q in tgt.queries)


def test_test_index_fails_when_indexed_lookup_short_of_truth():
    from any2heliosdb.validate.data import run_test_index

    tgt = _IndexProbeTarget(truth=[(10, 3)], indexed=0)            # stale index: 0 vs true 3
    res = run_test_index(tgt, _fk_table())
    assert not res.passed                                          # BLOCKER finding
    assert res.metrics["mismatches"] == 1
    assert "FK column 'CAT_ID'" in res.errors[0].message


def test_test_index_noop_without_fks_or_values():
    from any2heliosdb.validate.data import run_test_index

    res1 = run_test_index(_IndexProbeTarget(truth=[]), _emp_table())   # no FK columns
    assert res1.passed and res1.metrics["fk_columns_checked"] == 0
    res2 = run_test_index(_IndexProbeTarget(truth=[]), _fk_table())    # FK present, no rows
    assert res2.passed and res2.metrics["fk_columns_checked"] == 0


def test_test_index_failsafe_skips_on_probe_error():
    # The Nano regression: a target that can't evaluate the probe must DEGRADE
    # (skip), never produce a false BLOCKER.
    from any2heliosdb.validate.data import run_test_index

    class _Err:
        def query(self, sql, params=None):
            raise RuntimeError("scalar subquery reached the evaluator without materialisation")

    res = run_test_index(_Err(), _fk_table())
    assert res.passed                                              # no false failure
    assert res.metrics["fk_columns_checked"] == 0
    assert res.metrics["mismatches"] == 0


def test_passed_false_when_a_blocker_is_present():
    r = ValidationResult(validation_type=ValidationType.TEST_COUNT)
    r.add_error(Severity.COSMETIC, "hr.emp", "minor")
    r.add_error(Severity.BLOCKER, "hr.emp", "DataMismatch")
    assert r.passed is False


# --- row_checksum determinism -----------------------------------------------
def test_row_checksum_is_deterministic_and_value_sensitive():
    a = row_checksum([1, "alice", None])
    b = row_checksum([1, "alice", None])
    assert a == b
    assert len(a) == 64 and int(a, 16) >= 0
    assert row_checksum([1, "alice", None]) != row_checksum([1, "alicE", None])
    assert row_checksum([1, "alice", None]) != row_checksum([2, "alice", None])
    assert row_checksum([None]) != row_checksum([""])
    assert row_checksum([None]) != row_checksum(["None"])
    assert row_checksum(["a", "b"]) != row_checksum(["ab"])
    # bytes and memoryview with identical content hash equal (driver representation).
    assert row_checksum([b"\x89\x00\x01"]) == row_checksum([memoryview(b"\x89\x00\x01")])
    assert row_checksum([b"\x01"]) != row_checksum([b"\x02"])
    # A target returning BYTEA as a PG hex string ('\xDEADBEEF') compares equal
    # to the source's raw bytes for the same content.
    assert row_checksum([b"\x89\x00\x01"]) == row_checksum(["\\x890001"])


# --- run_test_count with fakes ----------------------------------------------
def test_run_test_count_passes_when_counts_match():
    source = FakeSource(counts={"EMP": 42})
    target = FakeTarget(counts={"emp": 42})  # unqualified, lowercased
    res = run_test_count(source, target, [_emp_table()])
    assert res.validation_type is ValidationType.TEST_COUNT
    assert res.passed is True
    assert res.errors == []
    assert res.metrics["tables_matched"] == 1


def test_run_test_count_flags_mismatch_as_blocker():
    source = FakeSource(counts={"EMP": 42})
    target = FakeTarget(counts={"emp": 40})
    res = run_test_count(source, target, [_emp_table()])
    assert res.passed is False
    err = res.errors[0]
    assert err.severity is Severity.BLOCKER
    assert err.table == "HR.EMP"
    assert "source=42" in err.message and "target=40" in err.message
    assert res.metrics["tables_mismatched"] == 1


# --- run_test (structure) with fakes ----------------------------------------
def test_run_test_detects_missing_table_and_col_mismatch():
    schema = Schema(name="HR", tables=[_emp_table()])
    # Table missing entirely (describe_columns raises).
    res_missing = run_test(schema, FakeTarget(columns={}))
    assert res_missing.passed is False
    assert "missing table" in res_missing.errors[0].message

    # Present but wrong column count (source 2, target 3).
    res_cols = run_test(schema, FakeTarget(columns={"emp": ["id", "name", "extra"]}))
    assert res_cols.passed is False
    assert "column count differs" in res_cols.errors[0].message

    # Present with matching column count -> pass.
    res_ok = run_test(schema, FakeTarget(columns={"emp": ["id", "name"]}))
    assert res_ok.passed is True
    assert res_ok.metrics["tables_ok"] == 1


# --- run_test_data with fakes -----------------------------------------------
def test_run_test_data_matches_identical_rows():
    rows = [(1, "alice"), (2, "bob")]
    source = FakeSource(rows={"EMP": list(rows)})
    target = FakeTarget(select_rows={"emp": list(rows)})
    res = run_test_data(source, target, _emp_table())
    assert res.passed is True
    assert res.metrics["rows_compared"] == 2
    assert res.metrics["mismatches"] == 0


def test_run_test_data_flags_row_mismatch():
    source = FakeSource(rows={"EMP": [(1, "alice"), (2, "bob")]})
    target = FakeTarget(select_rows={"emp": [(1, "alice"), (2, "BOB")]})
    res = run_test_data(source, target, _emp_table())
    assert res.passed is False
    assert res.metrics["mismatches"] == 1
    assert any("row 1 checksum mismatch" in e.message for e in res.errors)


def test_run_test_data_no_pk_compares_multiset():
    # No PK -> compare the order-independent MULTISET of row checksums, not skip.
    src = FakeSource(rows={"LOG": [("a",), ("b",), ("b",)]})
    tgt = FakeTarget(select_rows={"log": [("b",), ("a",), ("b",)]})  # same multiset, reordered
    res = run_test_data(src, tgt, _nopk_table())
    assert res.passed is True
    assert res.metrics.get("no_pk_multiset") is True
    assert res.metrics["mismatches"] == 0


def test_run_test_data_no_pk_flags_difference():
    # A keyless table that lost/changed a row must FAIL (no more false pass).
    src = FakeSource(rows={"LOG": [("a",), ("b",), ("b",)]})
    tgt = FakeTarget(select_rows={"log": [("a",), ("b",)]})  # missing a row
    res = run_test_data(src, tgt, _nopk_table())
    assert res.passed is False
    assert any(e.severity is Severity.BLOCKER for e in res.errors)


def test_render_datetime_normalizes_tz_to_utc_instant():
    import datetime as dt

    from any2heliosdb.validate.data import _render
    # A timestamptz comes back tz-aware from PostgreSQL but naive from HeliosDB;
    # the same instant must render (and thus hash) equal, and a non-UTC offset
    # must normalize to the UTC instant rather than the wall-clock.
    naive = dt.datetime(2022, 2, 15, 9, 57, 12)
    assert _render(dt.datetime(2022, 2, 15, 9, 57, 12, tzinfo=dt.timezone.utc)) == _render(naive)
    assert _render(dt.datetime(2022, 2, 15, 11, 57, 12,
                               tzinfo=dt.timezone(dt.timedelta(hours=2)))) == _render(naive)
    # microseconds preserved (so a real sub-second difference still mismatches)
    assert _render(dt.datetime(2022, 2, 15, 9, 57, 12, 5)) != _render(naive)


def test_render_array_matches_pg_literal():
    from any2heliosdb.validate.data import _render
    # An ARRAY source column maps to TEXT on the target, stored as a PG literal;
    # the native source list must render to exactly the same literal.
    assert _render(["Trailers", "Commentaries"]) == "{Trailers,Commentaries}"
    assert _render(["Deleted Scenes", "Behind the Scenes"]) == \
        '{"Deleted Scenes","Behind the Scenes"}'
    assert _render([1, 2, 3]) == "{1,2,3}"
    assert _render(["a,b", 'q"x', None]) == '{"a,b","q\\"x",NULL}'
    assert _render([]) == "{}"
