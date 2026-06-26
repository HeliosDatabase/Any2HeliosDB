"""Unit tests for the v1.0.0 CODEX-hardening fixes #54/#55/#56/#57 (no DB).

(The #58 checksum, #59 native-atomicity, and #60 copy_codec fixes have their own
test files: test_checksum_framing.py, test_native_driver.py, test_copy_codec_binary.py.)
"""
import pytest

from any2heliosdb.emit import ddl
from any2heliosdb.sources.base import SourceDsn
from any2heliosdb.sources.oracle.adapter import (
    OracleAdapter, _GENERATED_NOTNULL, _oid, quote_oracle,
)


# --- #55: Oracle [schema.]seq.NEXTVAL default -> nextval('seq') -------------
@pytest.mark.parametrize("default,expected", [
    ("EMP_SEQ.NEXTVAL", "nextval('emp_seq')"),
    ("emp_seq.nextval", "nextval('emp_seq')"),
    ("HR.EMP_SEQ.NEXTVAL", "nextval('emp_seq')"),
    ('"HR"."EMP_SEQ".NEXTVAL', "nextval('emp_seq')"),
    ('"HR"."EMP_SEQ"."NEXTVAL"', "nextval('emp_seq')"),   # Oracle's stored form
    ('"EMP_SEQ"."NEXTVAL"', "nextval('emp_seq')"),
    ("EMP_SEQ . NEXTVAL", "nextval('emp_seq')"),
])
def test_translate_oracle_nextval_default(default, expected):
    assert ddl._translate_default(default) == expected


def test_translate_default_preserves_non_nextval():
    assert ddl._translate_default("SYSDATE") == "CURRENT_TIMESTAMP"
    assert ddl._translate_default("42") == "42"
    assert ddl._translate_default("'x'") == "'x'"
    # a column merely named like a sequence must not be mistaken for NEXTVAL
    assert ddl._translate_default("'NEXTVAL'") == "'NEXTVAL'"


# --- #56: Oracle identifier quoting doubles an embedded double-quote --------
def test_oid_doubles_embedded_quote():
    assert _oid("abc") == '"abc"'
    assert _oid('a"b') == '"a""b"'


def test_quote_oracle_doubles_in_both_parts():
    assert quote_oracle("HR", "EMP") == '"HR"."EMP"'
    assert quote_oracle("H\"R", 'EMP"S') == '"H""R"."EMP""S"'


# --- #57: only a single-column generated NOT NULL check is dropped ----------
def test_generated_notnull_matches_single_column_form():
    assert _GENERATED_NOTNULL.match('"EMAIL" IS NOT NULL')
    assert _GENERATED_NOTNULL.match("EMAIL IS NOT NULL")


def test_generated_notnull_preserves_real_checks():
    # A real multi-term CHECK that merely ENDS with IS NOT NULL must be kept.
    assert not _GENERATED_NOTNULL.match("EMAIL IS NULL OR PHONE IS NOT NULL")
    assert not _GENERATED_NOTNULL.match("LENGTH(NAME) > 0")


# --- #54: AS OF SCN snapshot on the Oracle adapter -------------------------
def _ora() -> OracleAdapter:
    return OracleAdapter(SourceDsn(host="h", port=1521, service_name="X",
                                   user="u", schema="HR"))


def test_as_of_scn_set_reuse_and_clear():
    a = _ora()
    assert a._as_of() == ""                       # nothing pinned
    a.use_snapshot("12345")
    assert a._as_of() == " AS OF SCN 12345"
    a.use_snapshot(None)                          # clear
    assert a._as_of() == ""
    a.use_snapshot("not-a-number")               # malformed -> ignored, no crash
    assert a._as_of() == ""


def test_base_adapter_snapshot_is_noop():
    # A non-Oracle source defaults to "no snapshot" so reads stay 'current'.
    from any2heliosdb.sources.postgres.adapter import PostgresAdapter
    p = PostgresAdapter(SourceDsn(host="h", port=5432, database="d", user="u"))
    assert p.capture_snapshot() is None
    p.use_snapshot("anything")  # no-op, must not raise
