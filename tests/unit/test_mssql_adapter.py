"""Hermetic tests for the SQL Server source adapter's introspection + SQL.

A fake pyodbc connection routes each query to canned ``sys.*`` rows (keyed off a
distinctive substring of the SQL), so we can assert the adapter builds the right
IR (tables/columns/PK/FK/types) and emits the right SQL strings (``[bracket]``
quoting, the streaming SELECT, COUNT_BIG, MIN/MAX bounds) without a live SQL
Server. The end-to-end MSSQL->HeliosDB battle test runs separately.
"""
from any2heliosdb.core.catalog_model import DataTypeKind as K
from any2heliosdb.sources.base import SourceDsn
from any2heliosdb.sources.mssql.adapter import (
    MSSQLAdapter,
    _default,
    _reconstruct_source_type,
    quote_mssql,
    quote_mssql_table,
)

# --- canned sys.* result sets for one (dbo) schema --------------------------
# departments(dept_id PK, dept_name); employees(emp_id PK, full_name, email,
# salary, hired, active, notes, photo, dept_id FK->departments.dept_id) + index
# emp_dept_idx(dept_id), and a view active_employees.

_SCHEMAS = [("dbo",), ("sys",), ("db_owner",)]
_TABLES = [("departments",), ("employees",)]

# sys.columns rows: (name, type_name, max_length, precision, scale, is_nullable, default)
_COLUMNS = {
    "departments": [
        ("dept_id", "int", 4, 10, 0, 0, None),
        ("dept_name", "varchar", 50, 0, 0, 0, None),
    ],
    "employees": [
        ("emp_id", "int", 4, 10, 0, 0, None),
        ("full_name", "nvarchar", 200, 0, 0, 0, None),   # NVARCHAR(100): 200 bytes
        ("email", "nvarchar", 240, 0, 0, 1, None),       # NVARCHAR(120)
        ("salary", "decimal", 9, 10, 2, 1, None),
        ("hired", "datetime2", 8, 27, 7, 1, None),
        ("active", "bit", 1, 1, 0, 1, "((1))"),
        ("notes", "nvarchar", -1, 0, 0, 1, None),         # NVARCHAR(MAX)
        ("photo", "varbinary", -1, 0, 0, 1, None),        # VARBINARY(MAX)
        ("dept_id", "int", 4, 10, 0, 1, None),
    ],
}

_PK = {"departments": [("dept_id",)], "employees": [("emp_id",)]}

# sys.foreign_key_columns rows: (fk_name, parent_col, ref_table, ref_col)
_FK = {
    "departments": [],
    "employees": [("emp_dept_fk", "dept_id", "departments", "dept_id")],
}

# sys.indexes (non-PK) rows: (index_name, col, is_unique)
_IDX = {
    "departments": [],
    "employees": [("emp_dept_idx", "dept_id", 0)],
}

_VIEWS = [(
    "active_employees",
    "CREATE VIEW active_employees AS SELECT emp_id, full_name FROM employees WHERE active = 1",
)]


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.arraysize = 1
        self._rows: list = []

    def execute(self, sql, *params):
        self.conn.log.append((sql, [list(params)] if params else []))
        self._rows = list(self.conn.route(sql, params))
        return self

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def fetchmany(self, n):
        batch, self._rows = self._rows[:n], self._rows[n:]
        return batch

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.log = []
        self.stream_rows = []  # rows the next streamed SELECT returns

    def cursor(self):
        return FakeCursor(self)

    def route(self, sql, params):
        """Return canned rows for the query, keyed on a distinctive substring."""
        s = " ".join(sql.split())  # normalize whitespace
        p = list(params)
        tbl = p[-1] if p else None
        if "@@VERSION" in s:
            return [("Microsoft SQL Server 2022 (RTM-CU12)",)]
        if "SCHEMA_NAME()" in s:
            return [("dbo",)]
        if "FROM sys.schemas" in s and "ORDER BY name" in s:
            return _SCHEMAS
        if "FROM sys.tables t" in s and "ORDER BY t.name" in s:
            return _TABLES
        if "FROM sys.columns c" in s and "sys.default_constraints" in s:
            return _COLUMNS.get(tbl, [])
        if "is_primary_key = 1" in s:
            return _PK.get(tbl, [])
        if "sys.foreign_key_columns" in s:
            return _FK.get(tbl, [])
        if "sys.indexes" in s and "is_primary_key = 0" in s:
            return _IDX.get(tbl, [])
        if "FROM sys.views" in s:
            return _VIEWS
        if "COUNT_BIG(*)" in s:
            return [(5,)]
        if s.startswith("SELECT MIN("):
            return [(1, 5)]
        if s.startswith("SELECT [") and " FROM [" in s:  # streamed data SELECT
            return list(self.stream_rows)
        return []


def _adapter() -> MSSQLAdapter:
    a = MSSQLAdapter(SourceDsn(host="h", port=1433, database="hr", schema="dbo", user="sa"))
    a._conn = FakeConn()
    return a


# --- pure helpers -----------------------------------------------------------
def test_quote_mssql_brackets_and_escaping():
    assert quote_mssql("emp_id") == "[emp_id]"
    assert quote_mssql("weird]name") == "[weird]]name]"
    assert quote_mssql_table("dbo", "employees") == "[dbo].[employees]"


def test_reconstruct_source_type():
    assert _reconstruct_source_type("varchar", 50, 0, 0) == "VARCHAR(50)"
    assert _reconstruct_source_type("nvarchar", 100, 0, 0) == "NVARCHAR(100)"
    assert _reconstruct_source_type("nvarchar", -1, 0, 0) == "NVARCHAR(MAX)"
    assert _reconstruct_source_type("varbinary", -1, 0, 0) == "VARBINARY(MAX)"
    assert _reconstruct_source_type("decimal", None, 10, 2) == "DECIMAL(10,2)"
    assert _reconstruct_source_type("datetime2", 8, 27, 7) == "DATETIME2"
    assert _reconstruct_source_type("bit", 1, 1, 0) == "BIT"


def test_default_normalization():
    assert _default("((1))") == "1"
    assert _default("((0))") == "0"
    assert _default("(getdate())") == "CURRENT_TIMESTAMP"
    assert _default("(CURRENT_TIMESTAMP)") == "CURRENT_TIMESTAMP"
    assert _default("('hello')") is None  # string default dropped
    assert _default(None) is None


# --- introspection -> IR ----------------------------------------------------
def test_introspect_builds_tables_columns_types():
    schema = _adapter().introspect_schema("dbo")
    assert schema.name == "dbo"
    assert [t.name for t in schema.tables] == ["departments", "employees"]

    emp = next(t for t in schema.tables if t.name == "employees")
    by = {c.name: c for c in emp.columns}
    # column order preserved
    assert [c.name for c in emp.columns] == [
        "emp_id", "full_name", "email", "salary", "hired", "active", "notes", "photo", "dept_id"]
    # source_type reconstruction (incl. NVARCHAR byte->char halving and MAX)
    assert by["emp_id"].source_type == "INT"
    assert by["full_name"].source_type == "NVARCHAR(100)"
    assert by["email"].source_type == "NVARCHAR(120)"
    assert by["salary"].source_type == "DECIMAL(10,2)"
    assert by["hired"].source_type == "DATETIME2"
    assert by["notes"].source_type == "NVARCHAR(MAX)"
    assert by["photo"].source_type == "VARBINARY(MAX)"
    # mapped target types
    assert by["emp_id"].data_type.kind is K.INTEGER
    assert by["full_name"].data_type.kind is K.VARCHAR and by["full_name"].data_type.length == 100
    assert by["salary"].data_type.kind is K.NUMERIC
    assert (by["salary"].data_type.precision, by["salary"].data_type.scale) == (10, 2)
    assert by["hired"].data_type.kind is K.TIMESTAMP
    assert by["active"].data_type.kind is K.BOOLEAN
    assert by["notes"].data_type.kind is K.TEXT       # NVARCHAR(MAX) -> TEXT
    assert by["photo"].data_type.kind is K.BYTEA
    # nullability
    assert by["emp_id"].nullable is False
    assert by["email"].nullable is True
    # bit default normalized
    assert by["active"].default == "1"


def test_introspect_primary_key():
    schema = _adapter().introspect_schema("dbo")
    emp = next(t for t in schema.tables if t.name == "employees")
    assert emp.primary_key is not None
    assert emp.primary_key.columns == ["emp_id"]


def test_introspect_foreign_key_uses_referenced_columns():
    schema = _adapter().introspect_schema("dbo")
    emp = next(t for t in schema.tables if t.name == "employees")
    assert len(emp.foreign_keys) == 1
    fk = emp.foreign_keys[0]
    assert fk.name == "emp_dept_fk"
    assert fk.columns == ["dept_id"]
    assert fk.references_table == "departments"
    assert fk.references_columns == ["dept_id"]  # actual referenced col, not assumed


def test_introspect_indexes_skip_pk():
    schema = _adapter().introspect_schema("dbo")
    emp = next(t for t in schema.tables if t.name == "employees")
    assert len(emp.indexes) == 1
    assert emp.indexes[0].name == "emp_dept_idx"
    assert [ic.name for ic in emp.indexes[0].columns] == ["dept_id"]
    deps = next(t for t in schema.tables if t.name == "departments")
    assert deps.indexes == []


def test_introspect_views():
    schema = _adapter().introspect_schema("dbo")
    assert len(schema.views) == 1
    assert schema.views[0].name == "active_employees"
    assert "CREATE VIEW" in schema.views[0].definition


def test_list_schemas_filters_system():
    assert _adapter().list_schemas() == ["dbo"]


def test_default_schema_prefers_dsn():
    assert _adapter().default_schema() == "dbo"


def test_server_version():
    assert "SQL Server 2022" in _adapter().server_version()


# --- SQL string assertions (the data path) ----------------------------------
def test_stream_rows_brackets_and_select():
    a = _adapter()
    a._conn.stream_rows = [(1, "Ada"), (2, "Alan")]
    schema = a.introspect_schema("dbo")
    emp = next(t for t in schema.tables if t.name == "employees")
    a._conn.log.clear()
    rows = list(a.stream_rows(emp, ["emp_id", "full_name"], arraysize=500))
    assert rows == [(1, "Ada"), (2, "Alan")]
    sql = a._conn.log[-1][0]
    assert sql == "SELECT [emp_id], [full_name] FROM [dbo].[employees]"


def test_stream_rows_applies_where():
    a = _adapter()
    a._conn.stream_rows = []
    emp = next(t for t in a.introspect_schema("dbo").tables if t.name == "employees")
    a._conn.log.clear()
    list(a.stream_rows(emp, ["emp_id"], where="[emp_id] >= 1 AND [emp_id] < 3"))
    sql = a._conn.log[-1][0]
    assert sql == "SELECT [emp_id] FROM [dbo].[employees] WHERE [emp_id] >= 1 AND [emp_id] < 3"


def test_exact_row_count_uses_count_big_and_brackets():
    a = _adapter()
    emp = next(t for t in a.introspect_schema("dbo").tables if t.name == "employees")
    a._conn.log.clear()
    assert a.exact_row_count(emp) == 5
    assert a._conn.log[-1][0] == "SELECT COUNT_BIG(*) FROM [dbo].[employees]"


def test_numeric_pk_bounds_integer_pk():
    a = _adapter()
    emp = next(t for t in a.introspect_schema("dbo").tables if t.name == "employees")
    a._conn.log.clear()
    assert a.numeric_pk_bounds(emp, "emp_id") == (1, 5)
    assert a._conn.log[-1][0] == "SELECT MIN([emp_id]), MAX([emp_id]) FROM [dbo].[employees]"


def test_numeric_pk_bounds_skips_non_integer_pk():
    # A DECIMAL/NVARCHAR PK is not range-chunkable: returns None without querying.
    a = _adapter()
    emp = next(t for t in a.introspect_schema("dbo").tables if t.name == "employees")
    a._conn.log.clear()
    assert a.numeric_pk_bounds(emp, "salary") is None   # salary is DECIMAL
    assert a._conn.log == []  # no MIN/MAX query issued
