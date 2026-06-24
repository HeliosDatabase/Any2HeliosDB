"""Unit tests for the Oracle-dialect (native path) DDL emitter."""
from any2heliosdb.core.catalog_model import (
    Column,
    Constraint,
    ConstraintKind,
    DataType,
    DataTypeKind,
    ForeignKey,
    Index,
    PrimaryKey,
    Sequence,
    Table,
)
from any2heliosdb.emit.oracle_ddl import (
    render_create_table_oracle,
    render_foreign_keys_oracle,
    render_index_oracle,
    render_sequence_oracle,
)


def _emp():
    return Table(
        name="EMPLOYEES", schema="HR",
        columns=[
            Column("EMP_ID", DataType.decimal(10, 0), nullable=False, source_type="NUMBER(10)"),
            Column("FULL_NAME", DataType.varchar(100), nullable=False, source_type="VARCHAR2(100)"),
            Column("SALARY", DataType.decimal(10, 2), nullable=True, source_type="NUMBER(10,2)"),
            Column("HIRED", DataType.of(DataTypeKind.TIMESTAMP), nullable=True, source_type="DATE"),
            Column("ACTIVE", DataType.decimal(1, 0), nullable=True, default="1", source_type="NUMBER(1)"),
        ],
        primary_key=PrimaryKey(columns=["EMP_ID"]),
        constraints=[Constraint(name="EMP_SAL_CHK", constraint_type=ConstraintKind.CHECK,
                                expression="SALARY >= 0")],
    )


def test_create_table_uses_source_types_and_quoted_uppercase_idents():
    ddl = render_create_table_oracle(_emp())
    assert ddl.startswith('CREATE TABLE "EMPLOYEES" (')
    # near-passthrough: original Oracle types, not the PG-mapped DECIMAL/VARCHAR
    assert '"EMP_ID" NUMBER(10) NOT NULL' in ddl
    assert '"FULL_NAME" VARCHAR2(100) NOT NULL' in ddl
    assert '"SALARY" NUMBER(10,2)' in ddl
    assert '"HIRED" DATE' in ddl
    assert '"ACTIVE" NUMBER(1) DEFAULT 1' in ddl
    assert 'PRIMARY KEY ("EMP_ID")' in ddl
    assert 'CONSTRAINT "EMP_SAL_CHK" CHECK (SALARY >= 0)' in ddl
    # no PG lowercasing / DECIMAL mapping leaked in
    assert "DECIMAL" not in ddl and "decimal" not in ddl


def test_sequence_oracle_syntax():
    sql = render_sequence_oracle(Sequence(name="EMP_SEQ", start=100, increment=1))
    assert sql == 'CREATE SEQUENCE "EMP_SEQ" START WITH 100 INCREMENT BY 1'


def test_index_skips_pk_backing_and_quotes():
    t = _emp()
    # a non-PK index is emitted
    idx = Index(name="EMP_NAME_IDX", columns=[Column("FULL_NAME", DataType.varchar(100))], unique=False)
    assert render_index_oracle(t, idx) == 'CREATE INDEX "EMP_NAME_IDX" ON "EMPLOYEES" ("FULL_NAME")'
    # a unique index that merely backs the PK is skipped
    pk_idx = Index(name="EMP_PK", columns=[Column("EMP_ID", DataType.decimal(10, 0))], unique=True)
    assert render_index_oracle(t, pk_idx) is None


def test_foreign_keys_oracle():
    t = Table(
        name="EMPLOYEES", schema="HR",
        columns=[Column("DEPT_ID", DataType.decimal(6, 0), source_type="NUMBER(6)")],
        foreign_keys=[ForeignKey(name="EMP_DEPT_FK", columns=["DEPT_ID"],
                                 references_table="DEPARTMENTS", references_columns=["DEPT_ID"])],
    )
    fks = render_foreign_keys_oracle(t)
    assert fks == ['ALTER TABLE "EMPLOYEES" ADD CONSTRAINT "EMP_DEPT_FK" '
                   'FOREIGN KEY ("DEPT_ID") REFERENCES "DEPARTMENTS" ("DEPT_ID")']
