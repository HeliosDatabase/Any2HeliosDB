"""Unit tests for PostgreSQL-source sequence handling (no database required).

Covers the pure helpers that close the PG-source sequence gap: nextval-default
preservation/normalization in the source adapter, the CACHE clause in the
PG-family sequence emitter, and the Oracle/MySQL emitters skipping a PG
nextval() default so migrate-back stays valid.
"""
from any2heliosdb.core.catalog_model import (
    Column, DataType, PrimaryKey, Sequence, Table,
)
from any2heliosdb.sources.postgres.adapter import _clean_default, _normalize_nextval
from any2heliosdb.emit import ddl, oracle_ddl, mysql_ddl


# --- _normalize_nextval ------------------------------------------------------
def test_normalize_nextval_strips_regclass_cast():
    assert _normalize_nextval("nextval('actor_actor_id_seq'::regclass)") \
        == "nextval('actor_actor_id_seq')"


def test_normalize_nextval_strips_schema_qualifier():
    assert _normalize_nextval("nextval('public.actor_actor_id_seq'::regclass)") \
        == "nextval('actor_actor_id_seq')"


def test_normalize_nextval_plain_form():
    assert _normalize_nextval("nextval('s')") == "nextval('s')"


def test_normalize_nextval_unparseable_returns_none():
    assert _normalize_nextval("nextval(somethingweird)") is None


# --- _clean_default ----------------------------------------------------------
def test_clean_default_preserves_nextval():
    assert _clean_default("nextval('public.s'::regclass)") == "nextval('s')"


def test_clean_default_still_handles_others():
    assert _clean_default("now()") == "CURRENT_TIMESTAMP"
    assert _clean_default("42") == "42"
    assert _clean_default("'x'::text") is None      # non-numeric literal dropped
    assert _clean_default(None) is None


# --- render_sequence CACHE (PG-family emitter) -------------------------------
def test_render_sequence_emits_cache_when_gt_one():
    sql = ddl.render_sequence(Sequence(name="s", start=201, increment=1, cache=32))
    assert "CREATE SEQUENCE s" in sql
    assert "START WITH 201" in sql
    assert "CACHE 32" in sql


def test_render_sequence_omits_cache_when_one():
    sql = ddl.render_sequence(Sequence(name="s", start=1, increment=1, cache=1))
    assert "CACHE" not in sql


# --- nextval default skipped by non-PG emitters ------------------------------
def _serial_table() -> Table:
    return Table(
        name="actor", schema="public",
        columns=[
            Column("actor_id", DataType.decimal(10, 0), nullable=False,
                   default="nextval('actor_actor_id_seq')"),
            Column("name", DataType.varchar(50), nullable=True),
        ],
        primary_key=PrimaryKey(columns=["actor_id"]),
    )


def test_pg_emitter_keeps_nextval_default():
    sql = ddl.render_create_table(_serial_table())
    assert "DEFAULT nextval('actor_actor_id_seq')" in sql


def test_oracle_emitter_skips_nextval_default():
    sql = oracle_ddl.render_create_table_oracle(_serial_table())
    assert "nextval" not in sql.lower()


def test_mysql_emitter_skips_nextval_default():
    sql = mysql_ddl.render_create_table(_serial_table())
    assert "nextval" not in sql.lower()
