"""Unit tests for DDL emission from the IR (no database required)."""
from any2heliosdb.core.catalog_model import (
    Column, DataType, DataTypeKind as K, ForeignKey, Index, IndexColumn,
    PrimaryKey, Sequence, Table,
)
from any2heliosdb.emit import ddl


def _emp_table() -> Table:
    return Table(
        name="EMPLOYEES", schema="HR",
        columns=[
            Column("EMP_ID", DataType.decimal(10, 0), nullable=False),
            Column("FULL_NAME", DataType.varchar(100), nullable=False),
            Column("SALARY", DataType.decimal(10, 2), nullable=True),
            Column("ACTIVE", DataType.decimal(1, 0), nullable=True, default="1"),
        ],
        primary_key=PrimaryKey(columns=["EMP_ID"]),
        foreign_keys=[ForeignKey(name="EMP_DEPT_FK", columns=["DEPT_ID"],
                                 references_table="DEPARTMENTS", references_columns=["DEPT_ID"])],
        indexes=[Index(name="EMP_DEPT_IDX", columns=[IndexColumn(name="DEPT_ID")])],
    )


def test_create_table_lowercases_and_emits_pk():
    sql = ddl.render_create_table(_emp_table())
    assert sql.startswith("CREATE TABLE employees (")
    assert "emp_id DECIMAL(10, 0) NOT NULL" in sql
    assert "full_name VARCHAR(100) NOT NULL" in sql
    assert "salary DECIMAL(10, 2)" in sql
    assert "active DECIMAL(1, 0) DEFAULT 1" in sql
    assert "PRIMARY KEY (emp_id)" in sql
    # FK is NOT inline (emitted separately so data loads first)
    assert "FOREIGN KEY" not in sql


def test_foreign_keys_emitted_separately():
    stmts = ddl.render_foreign_keys(_emp_table())
    assert len(stmts) == 1
    assert stmts[0] == (
        "ALTER TABLE employees ADD CONSTRAINT emp_dept_fk "
        "FOREIGN KEY (dept_id) REFERENCES departments (dept_id);"
    )


def test_index_skips_pk_backing_and_emits_others():
    t = _emp_table()
    # A unique index identical to the PK is skipped...
    pk_idx = Index(name="EMP_PK", columns=[IndexColumn(name="EMP_ID")], unique=True)
    assert ddl.render_index(t, pk_idx) is None
    # ...a normal secondary index is emitted.
    assert ddl.render_index(t, t.indexes[0]) == "CREATE INDEX emp_dept_idx ON employees (dept_id);"


def test_sequence_and_default_translation():
    seq = Sequence(name="EMP_SEQ", start=100, increment=1)
    assert ddl.render_sequence(seq) == "CREATE SEQUENCE emp_seq START WITH 100 INCREMENT BY 1;"
    # SYSDATE / SYS_GUID() defaults get the always-safe rewrites.
    t = Table(name="t", columns=[
        Column("created", DataType.of(K.TIMESTAMP), default="SYSDATE"),
        Column("id", DataType.of(K.UUID), default="SYS_GUID()"),
    ])
    sql = ddl.render_create_table(t)
    assert "created TIMESTAMP DEFAULT CURRENT_TIMESTAMP" in sql
    assert "id UUID DEFAULT gen_random_uuid()" in sql


def test_boolean_default_is_translated_to_a_boolean_literal():
    # A MySQL TINYINT(1)->BOOLEAN column with DEFAULT 1/0 must become a boolean
    # literal — strict PostgreSQL rejects `boolean DEFAULT 1` (HeliosDB accepts it).
    t = Table(name="t", schema="s", columns=[
        Column("active", DataType.of(K.BOOLEAN), default="1"),
        Column("deleted", DataType.of(K.BOOLEAN), default="0"),
        Column("flag", DataType.of(K.BOOLEAN), default="'1'"),
        Column("n", DataType.decimal(3, 0), default="1"),  # non-boolean keeps the numeric default
    ])
    sql = ddl.render_create_table(t)
    assert "active BOOLEAN DEFAULT true" in sql
    assert "deleted BOOLEAN DEFAULT false" in sql
    assert "flag BOOLEAN DEFAULT true" in sql
    assert "n DECIMAL(3, 0) DEFAULT 1" in sql
