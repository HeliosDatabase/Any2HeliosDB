"""Unit tests for the MySQL-dialect (MySQL target path) DDL emitter."""
from any2heliosdb.core.catalog_model import (
    Column,
    Constraint,
    ConstraintKind,
    DataType,
    DataTypeKind as K,
    ForeignKey,
    Index,
    IndexColumn,
    PrimaryKey,
    Table,
)
from any2heliosdb.emit import mysql_ddl


def _emp() -> Table:
    return Table(
        name="employees", schema="hr",
        columns=[
            Column("emp_id", DataType.of(K.INTEGER), nullable=False),
            Column("full_name", DataType.varchar(100), nullable=False),
            Column("salary", DataType.numeric(10, 2), nullable=True),
            Column("hired", DataType.of(K.TIMESTAMP), nullable=True),
            Column("active", DataType.of(K.BOOLEAN), nullable=True, default="1"),
            Column("notes", DataType.of(K.TEXT), nullable=True),
            Column("photo", DataType.of(K.BYTEA), nullable=True),
        ],
        primary_key=PrimaryKey(columns=["emp_id"]),
        foreign_keys=[ForeignKey(name="emp_dept_fk", columns=["dept_id"],
                                 references_table="departments", references_columns=["dept_id"])],
        indexes=[Index(name="emp_dept_idx", columns=[IndexColumn(name="dept_id")])],
        constraints=[Constraint(name="emp_sal_chk", constraint_type=ConstraintKind.CHECK,
                                expression="salary >= 0")],
    )


def test_type_mapping_to_mysql():
    assert mysql_ddl.mysql_type(DataType.of(K.INTEGER)) == "INT"
    assert mysql_ddl.mysql_type(DataType.of(K.BIGINT)) == "BIGINT"
    assert mysql_ddl.mysql_type(DataType.numeric(10, 2)) == "DECIMAL(10, 2)"
    assert mysql_ddl.mysql_type(DataType.decimal(8, 3)) == "DECIMAL(8, 3)"
    assert mysql_ddl.mysql_type(DataType.varchar(120)) == "VARCHAR(120)"
    assert mysql_ddl.mysql_type(DataType.of(K.TEXT)) == "LONGTEXT"
    assert mysql_ddl.mysql_type(DataType.of(K.BYTEA)) == "LONGBLOB"
    assert mysql_ddl.mysql_type(DataType.of(K.TIMESTAMP)) == "DATETIME"
    assert mysql_ddl.mysql_type(DataType.of(K.TIMESTAMPTZ)) == "TIMESTAMP"
    assert mysql_ddl.mysql_type(DataType.of(K.DATE)) == "DATE"
    assert mysql_ddl.mysql_type(DataType.of(K.TIME)) == "TIME"
    assert mysql_ddl.mysql_type(DataType.of(K.BOOLEAN)) == "TINYINT(1)"
    assert mysql_ddl.mysql_type(DataType.of(K.JSONB)) == "JSON"
    assert mysql_ddl.mysql_type(DataType.of(K.JSON)) == "JSON"


def test_create_table_backticks_and_types():
    ddl = mysql_ddl.render_create_table(_emp())
    assert ddl.startswith("CREATE TABLE `employees` (")
    assert "`emp_id` INT NOT NULL" in ddl
    assert "`full_name` VARCHAR(100) NOT NULL" in ddl
    assert "`salary` DECIMAL(10, 2)" in ddl
    assert "`hired` DATETIME" in ddl
    assert "`active` TINYINT(1) DEFAULT 1" in ddl
    assert "`notes` LONGTEXT" in ddl
    assert "`photo` LONGBLOB" in ddl
    assert "PRIMARY KEY (`emp_id`)" in ddl
    assert "CONSTRAINT `emp_sal_chk` CHECK (salary >= 0)" in ddl
    # FK is NOT inline (emitted separately so data loads first)
    assert "FOREIGN KEY" not in ddl


def test_current_timestamp_default_only_on_datetime():
    # A non-temporal column with a CURRENT_TIMESTAMP default must not carry it.
    t = Table(name="t", columns=[
        Column("created", DataType.of(K.TIMESTAMP), default="CURRENT_TIMESTAMP"),
        Column("name", DataType.varchar(10), default="CURRENT_TIMESTAMP"),
    ])
    ddl = mysql_ddl.render_create_table(t)
    assert "`created` DATETIME DEFAULT CURRENT_TIMESTAMP" in ddl
    # the bogus string default is dropped on the VARCHAR column
    assert "`name` VARCHAR(10)\n" in ddl + "\n" or "`name` VARCHAR(10)," in ddl


def test_index_skips_pk_backing_and_emits_others():
    t = _emp()
    pk_idx = Index(name="emp_pk", columns=[IndexColumn(name="emp_id")], unique=True)
    assert mysql_ddl.render_index(t, pk_idx) is None
    assert mysql_ddl.render_index(t, t.indexes[0]) == (
        "CREATE INDEX `emp_dept_idx` ON `employees` (`dept_id`)")


def test_foreign_keys_emitted_separately():
    stmts = mysql_ddl.render_foreign_keys(_emp())
    assert stmts == [
        "ALTER TABLE `employees` ADD CONSTRAINT `emp_dept_fk` "
        "FOREIGN KEY (`dept_id`) REFERENCES `departments` (`dept_id`)"]


def test_drop_foreign_keys():
    stmts = mysql_ddl.render_drop_foreign_keys(_emp())
    assert stmts == ["ALTER TABLE `employees` DROP FOREIGN KEY `emp_dept_fk`"]


def test_decimal_precision_capped():
    # MySQL caps DECIMAL precision at 65 and scale at 30; an Oracle NUMBER(38,0)
    # mapped through is fine, but an over-wide spec must be clamped.
    assert mysql_ddl.mysql_type(DataType.numeric(38, 0)) == "DECIMAL(38, 0)"
    assert mysql_ddl.mysql_type(DataType.numeric(80, 40)) == "DECIMAL(65, 30)"
