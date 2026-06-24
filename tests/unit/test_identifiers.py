"""Identifier quoting is shared by the DDL emitter, the loader/driver, and the
validators. These tests lock in the quoting rules AND, crucially, that the three
layers produce the *same* string for tricky names — otherwise the DDL would
create one identifier and the loader/validator would address another.
"""
from any2heliosdb.core.catalog_model import (
    Column, DataType, PrimaryKey, Table,
)
from any2heliosdb.core.identifiers import (
    fold, quote_ident, quote_table, render_ident, render_table,
)
from any2heliosdb.core.loader import _ident as loader_fold
from any2heliosdb.emit import ddl
from any2heliosdb.target.psycopg_driver import quote_ident as drv_quote_ident
from any2heliosdb.target.psycopg_driver import quote_table as drv_quote_table
from any2heliosdb.validate.data import _ident as validate_ident
from any2heliosdb.validate.data import _table as validate_table


def test_quote_ident_bare_vs_quoted():
    # Simple lowercase identifiers stay bare.
    assert quote_ident("foo") == "foo"
    assert quote_ident("foo_bar2") == "foo_bar2"
    # Reserved words must be quoted even though they look "simple".
    assert quote_ident("order") == '"order"'
    assert quote_ident("user") == '"user"'
    assert quote_ident("select") == '"select"'
    assert quote_ident("desc") == '"desc"'
    # Mixed case / specials get quoted; embedded quotes are doubled.
    assert quote_ident("MixedCase") == '"MixedCase"'
    assert quote_ident("has space") == '"has space"'
    assert quote_ident('we"ird') == '"we""ird"'


def test_fold_case_policy():
    assert fold("MixedCase", preserve_case=False) == "mixedcase"
    assert fold("MixedCase", preserve_case=True) == "MixedCase"


def test_render_ident_folds_then_quotes():
    # Reserved word survives folding -> quoted.
    assert render_ident("ORDER", preserve_case=False) == '"order"'
    assert render_ident("user", preserve_case=False) == '"user"'
    # Ordinary name -> bare lowercase.
    assert render_ident("Foo", preserve_case=False) == "foo"
    # preserve_case keeps the source case, which then must be quoted.
    assert render_ident("Foo", preserve_case=True) == '"Foo"'


def test_render_table_qualified():
    assert render_table("HR.ORDER", preserve_case=False) == 'hr."order"'
    assert render_table("HR.Emp", preserve_case=True) == '"HR"."Emp"'


def test_psycopg_driver_reexports_the_shared_quoter():
    # The driver must use the same quoter (same reserved set) as everyone else.
    assert drv_quote_ident is quote_ident
    assert drv_quote_table is quote_table


def test_all_layers_agree_on_reserved_and_case():
    # DDL emitter, loader(+driver), and validator must render identically.
    for name in ("order", "user", "ORDER", "MixedCase", "normal_col", "desc"):
        for preserve_case in (False, True):
            emitted = ddl.ident(name, preserve_case)                     # DDL
            loaded = drv_quote_ident(loader_fold(name, preserve_case))   # load path
            validated = validate_ident(name, preserve_case)             # validator
            assert emitted == loaded == validated, (
                name, preserve_case, emitted, loaded, validated)


def test_create_table_quotes_reserved_and_mixed_case():
    t = Table(
        name="ORDER",  # reserved table name
        schema="SALES",
        columns=[
            Column("user", DataType.decimal(10, 0), nullable=False),    # reserved col
            Column("Amount", DataType.decimal(10, 2), nullable=True),   # mixed case
            Column("note", DataType.varchar(100), nullable=True),       # ordinary
        ],
        primary_key=PrimaryKey(columns=["user"]),
    )
    sql = ddl.render_create_table(t)
    assert sql.startswith('CREATE TABLE "order" (')   # reserved -> folded + quoted
    assert '"user" DECIMAL(10, 0) NOT NULL' in sql    # reserved col quoted
    assert "amount DECIMAL(10, 2)" in sql             # mixed case folds to bare lowercase
    assert "note VARCHAR(100)" in sql                 # ordinary stays bare
    assert 'PRIMARY KEY ("user")' in sql


def test_validator_addresses_the_emitted_table_name():
    # The validator's FROM clause must name exactly what CREATE TABLE created.
    t = Table(name="ORDER", schema="SALES",
              columns=[Column("id", DataType.decimal(10, 0))])
    assert validate_table(t, False) == '"order"'
    assert ddl.render_create_table(t).startswith('CREATE TABLE "order" (')
