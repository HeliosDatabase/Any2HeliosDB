"""Unit tests for the pure-logic core: IR DDL rendering, COPY codec, type map.

These run without any database driver or live server.
"""
from any2heliosdb.core.catalog_model import DataType, DataTypeKind as K
from any2heliosdb.constants import SourceDialect
from any2heliosdb.target import copy_codec
from any2heliosdb.typemap.registry import TypeRegistry, Provenance


# --- IR DDL rendering -------------------------------------------------------
def test_datatype_sql_rendering():
    assert DataType.varchar(50).sql() == "VARCHAR(50)"
    assert DataType.char(2).sql() == "CHAR(2)"
    assert DataType.decimal(10, 2).sql() == "DECIMAL(10, 2)"
    assert DataType.numeric(38, 0).sql() == "NUMERIC(38, 0)"
    assert DataType.of(K.TIMESTAMPTZ).sql() == "TIMESTAMP WITH TIME ZONE"
    assert DataType.array_of(DataType.of(K.INTEGER)).sql() == "INTEGER[]"
    assert DataType(K.CUSTOM, custom="SDO_GEOMETRY").sql() == "SDO_GEOMETRY"


# --- COPY codec: the empty-string vs NULL distinction -----------------------
def test_copy_codec_null_vs_empty():
    # None -> \N ; "" -> empty field (the load-side half of Oracle ''==NULL)
    row = copy_codec.encode_row([1, None, ""])
    assert row == "1\t\\N\t\n"
    decoded = copy_codec.decode_row(row)
    assert decoded == ["1", None, ""]


def test_copy_codec_escaping_roundtrip():
    original = "a\tb\nc\\d\re"
    enc = copy_codec.encode_field(original)
    assert enc == "a\\tb\\nc\\\\d\\re"
    assert copy_codec.unescape_copy_text(enc) == original


# --- Type map: Oracle defaults + overrides ----------------------------------
def test_oracle_default_mappings():
    reg = TypeRegistry(SourceDialect.ORACLE)
    assert reg.resolve("NUMBER(10,2)").data_type.sql() == "DECIMAL(10, 2)"
    # Bare NUMBER (no precision/scale) -> unconstrained NUMERIC so fractional
    # values aren't silently truncated by a pinned scale-0 DECIMAL(38,0).
    assert reg.resolve("NUMBER").data_type.sql() == "NUMERIC"
    assert reg.resolve("VARCHAR2(255)").data_type.sql() == "VARCHAR(255)"
    assert reg.resolve("CLOB").data_type.sql() == "TEXT"
    assert reg.resolve("BLOB").data_type.sql() == "BYTEA"
    assert reg.resolve("DATE").data_type.sql() == "TIMESTAMP"
    assert reg.resolve("TIMESTAMP(6) WITH TIME ZONE").data_type.sql() == "TIMESTAMP WITH TIME ZONE"
    assert reg.resolve("ROWID").data_type.sql() == "TEXT"
    # Unmapped -> CUSTOM passthrough
    assert reg.resolve("SDO_GEOMETRY").data_type.kind is K.CUSTOM


def test_data_type_and_modify_type_overrides():
    reg = TypeRegistry(SourceDialect.ORACLE)
    reg.apply_data_type({"NUMBER": "bigint"})
    r = reg.resolve("NUMBER(10,0)")
    assert r.data_type.sql() == "BIGINT"
    assert r.provenance is Provenance.DATA_TYPE

    reg.apply_modify_type({"hr.emp.salary": "numeric(12,2)"})
    r2 = reg.resolve("NUMBER(8,0)", table="emp", column="salary", schema="hr")
    assert r2.data_type.sql() == "NUMERIC(12, 2)"
    assert r2.provenance is Provenance.MODIFY_TYPE


def test_mysql_and_mssql_signature_types():
    my = TypeRegistry(SourceDialect.MYSQL)
    assert my.resolve("TINYINT(1)").data_type.sql() == "BOOLEAN"
    assert my.resolve("DATETIME").data_type.sql() == "TIMESTAMP"
    ms = TypeRegistry(SourceDialect.MSSQL)
    assert ms.resolve("UNIQUEIDENTIFIER").data_type.sql() == "UUID"
    assert ms.resolve("NVARCHAR(MAX)").data_type.sql() == "TEXT"
    assert ms.resolve("MONEY").data_type.sql() == "DECIMAL(19, 4)"


def test_postgres_timestamp_tz_mapping():
    from any2heliosdb.typemap.defaults import map_postgresql_type as m
    # "without time zone" must NOT be promoted to tz-aware (it contains the
    # substring "TIME ZONE"); only a full "WITH TIME ZONE" is timestamptz.
    assert m("timestamp without time zone").sql() == "TIMESTAMP"
    assert m("timestamp with time zone").sql() == "TIMESTAMP WITH TIME ZONE"
    assert m("timestamp(6) with time zone").sql() == "TIMESTAMP WITH TIME ZONE"
    assert m("timestamptz").sql() == "TIMESTAMP WITH TIME ZONE"
    assert m("timestamp").sql() == "TIMESTAMP"


def test_detect_edition_classifies_helios_and_stock_postgres():
    from any2heliosdb.constants import Edition
    from any2heliosdb.target.base import detect_edition
    assert detect_edition("17.0 (HeliosDB-Lite 2.0)") is Edition.LITE
    assert detect_edition("3.58.2 (HeliosDB-Nano)") is Edition.NANO
    assert detect_edition("14.0 (HeliosDB)") is Edition.FULL
    # A real PostgreSQL server (no HeliosDB marker) is a first-class target.
    assert detect_edition("PostgreSQL 16.13 on x86_64-pc-linux-musl") is Edition.POSTGRES
    assert detect_edition("") is Edition.UNKNOWN


def test_concurrent_writes_gated_by_edition():
    # The Apache editions (Nano/Lite) can't service concurrent write transactions,
    # so the loader must serialize there; Full and stock PostgreSQL can run parallel.
    from any2heliosdb.constants import Edition
    from any2heliosdb.target.base import supports_concurrent_writes
    assert supports_concurrent_writes(Edition.NANO) is False        # version unknown -> serialize
    assert supports_concurrent_writes(Edition.LITE) is False
    assert supports_concurrent_writes(Edition.FULL) is True
    assert supports_concurrent_writes(Edition.POSTGRES) is True
    # Unknown targets are treated optimistically (the serial-retry pass still
    # recovers any chunk that fails under contention).
    assert supports_concurrent_writes(Edition.UNKNOWN) is True
    # Nano gained concurrent write txns in 3.60.7; older Nano still serializes.
    assert supports_concurrent_writes(Edition.NANO, "16.0 (HeliosDB Nano 3.60.7)") is True
    assert supports_concurrent_writes(Edition.NANO, "16.0 (HeliosDB Nano 3.61.0)") is True
    assert supports_concurrent_writes(Edition.NANO, "16.0 (HeliosDB Nano 3.60.6)") is False
    assert supports_concurrent_writes(Edition.NANO, "16.0 (HeliosDB Nano 3.60.4)") is False


def test_loader_serializes_when_target_lacks_concurrent_writes(tmp_path):
    # Regression for the Nano "parallel load hangs" issue: with concurrent_writes
    # False the loader must NOT run the parallel pass (which would block forever on
    # Nano's second concurrent writer) — it loads serially and records a note.
    from any2heliosdb.core.loader import ResumableLoader
    calls = []
    loader = ResumableLoader(cfg=None, schema=None,
                             manifest_path=str(tmp_path / "m.sqlite"), run_id="r1",
                             parallelism=8, concurrent_writes=False)
    loader._chunks = {"c0": object()}        # non-empty => run() skips plan()
    loader._load_pending = lambda parallel: calls.append(parallel)  # type: ignore
    stats = loader.run()
    assert calls == [False], "expected a single serial pass, got {}".format(calls)
    assert any("concurrent write" in w for w in stats.warnings)


def test_loader_parallel_then_serial_mopup_when_supported(tmp_path):
    # Stock PG / Full: parallel pass first, then the serial mop-up retry.
    from any2heliosdb.core.loader import ResumableLoader
    calls = []
    loader = ResumableLoader(cfg=None, schema=None,
                             manifest_path=str(tmp_path / "m.sqlite"), run_id="r1",
                             parallelism=8, concurrent_writes=True)
    loader._chunks = {"c0": object()}
    loader._load_pending = lambda parallel: calls.append(parallel)  # type: ignore
    loader.run()
    assert calls == [True, False], "expected parallel then serial mop-up, got {}".format(calls)


def test_portable_view_translates_only_on_pg_wire_path():
    from any2heliosdb.core.catalog_model import View
    from any2heliosdb.core.orchestrator import _portable_view
    v = View(name="emp_v",
             definition="SELECT id, NVL(name,'x') AS nm, SYSDATE AS dt FROM employees")
    # PG-wire target: Oracle constructs in the view body are translated to PG,
    # and the delegated constructs surface a target-gap note.
    pv, notes = _portable_view(v, "postgres", None)
    assert "COALESCE(name, 'x')" in pv.definition
    assert "CURRENT_TIMESTAMP" in pv.definition
    assert "NVL(" not in pv.definition and "SYSDATE" not in pv.definition
    assert notes
    # native Oracle / MySQL targets keep the source body verbatim, no notes.
    for d in ("oracle", "mysql"):
        same, n = _portable_view(v, d, None)
        assert same.definition == v.definition and n == []


def test_dedup_index_name_makes_schema_unique():
    from any2heliosdb.core.orchestrator import _dedup_index_name
    seen: set = set()
    assert _dedup_index_name("idx_fk", "orders", seen) == "idx_fk"  # first use passes through
    seen.add("idx_fk")
    assert _dedup_index_name("idx_fk", "items", seen) == "items_idx_fk"  # collision -> prefixed
    seen.add("items_idx_fk")
    assert _dedup_index_name("idx_fk", "items", seen) == "items_idx_fk_2"  # again -> counter


def test_translate_bool_comparisons():
    from any2heliosdb.core.orchestrator import _translate_bool_comparisons
    cols = {"active"}
    assert _translate_bool_comparisons("WHERE employees.active = 1", cols) == \
        "WHERE employees.active = true"
    assert _translate_bool_comparisons("where active = 0", cols) == "where active = false"
    assert _translate_bool_comparisons("where active = '1'", cols) == "where active = true"
    # a same-named non-boolean column is left alone; `= 10` is not `= 1`
    assert _translate_bool_comparisons("where status = 1", cols) == "where status = 1"
    assert _translate_bool_comparisons("where active = 10", cols) == "where active = 10"
