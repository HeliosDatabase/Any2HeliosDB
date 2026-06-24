"""Unit tests for the PL/SQL rewrite + gap + cost module (no database).

A tiny ``FakeCapabilities`` stands in for the real ``CapabilityMatrix`` — it only
needs an ``.accepts`` dict and an ``edition`` for gap attribution.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict

from any2heliosdb.constants import Edition, Severity
from any2heliosdb.plsql.cost import score_routine, to_person_days
from any2heliosdb.plsql.gap import GapReport, TargetGap
from any2heliosdb.plsql.rewrite import rewrite_sql


@dataclass
class FakeCapabilities:
    """Minimal stand-in for CapabilityMatrix used by the rewriter."""

    accepts: Dict[str, bool] = field(default_factory=dict)
    edition: Edition = Edition.LITE


# --------------------------------------------------------------------------- #
# KEEP passes (always applied)                                                #
# --------------------------------------------------------------------------- #

def test_nextval_currval_rewrite():
    sql, applied, gaps = rewrite_sql(
        "INSERT INTO t (id) VALUES (emp_seq.NEXTVAL);", FakeCapabilities()
    )
    assert "nextval('emp_seq')" in sql
    assert "NEXTVAL" not in sql.upper().replace("NEXTVAL('", "")  # only inside func name
    assert "keep:nextval" in applied
    # CURRVAL too, schema-qualified.
    sql2, _, _ = rewrite_sql("SELECT hr.emp_seq.CURRVAL;", FakeCapabilities())
    assert "currval('hr.emp_seq')" in sql2


def test_sys_guid_rewrite():
    sql, applied, _ = rewrite_sql(
        "INSERT INTO t (id) VALUES (SYS_GUID());", FakeCapabilities()
    )
    assert "gen_random_uuid()" in sql
    assert "SYS_GUID" not in sql.upper()
    assert "keep:sys_guid" in applied


def test_from_dual_stripped():
    sql, applied, _ = rewrite_sql("SELECT 1 FROM DUAL;", FakeCapabilities())
    assert "DUAL" not in sql.upper()
    assert sql.strip() == "SELECT 1;"
    assert "keep:from_dual" in applied


def test_mysql_backtick_alias_with_space_is_quoted():
    # Bug: a MySQL backtick alias containing a space was emitted UNQUOTED, which
    # strict PostgreSQL / the PG-wire parser rejects ("found: code").
    sql, applied, _ = rewrite_sql(
        "SELECT `a`.`postal_code` AS `zip code` FROM staff a", FakeCapabilities()
    )
    assert 'AS "zip code"' in sql            # space -> double-quoted
    assert "a.postal_code" in sql            # bare lower_snake stays unquoted
    assert "`" not in sql                    # no MySQL backticks survive
    assert "keep:mysql_backtick_ident" in applied


def test_mysql_backtick_reserved_and_mixed_case_quoted():
    sql, _, _ = rewrite_sql(
        "SELECT `order`, `MixedCol`, `plain_col` FROM t", FakeCapabilities()
    )
    assert '"order"' in sql and '"MixedCol"' in sql      # reserved / mixed-case quoted
    assert "plain_col" in sql and '"plain_col"' not in sql  # plain token stays bare


def test_mysql_if_becomes_case():
    # Bug: MySQL scalar IF() passed through untranslated; PostgreSQL has no IF().
    sql, applied, _ = rewrite_sql(
        "SELECT if(cu.active, 'active', '') AS act FROM customer cu", FakeCapabilities()
    )
    assert "CASE WHEN cu.active THEN 'active' ELSE '' END" in sql
    assert "if(" not in sql.lower()
    assert "keep:mysql_if" in applied


def test_mysql_if_nested_and_arg_commas():
    # A nested IF and commas inside arguments (both in a string literal and inside
    # a function call) must all be handled by the balanced/quote-aware split.
    sql, _, _ = rewrite_sql(
        "SELECT if(a, if(b, 1, 2), concat(x, ',', y)) AS v FROM t", FakeCapabilities()
    )
    assert sql == (
        "SELECT CASE WHEN a THEN CASE WHEN b THEN 1 ELSE 2 END "
        "ELSE concat(x, ',', y) END AS v FROM t"
    )
    sql2, _, _ = rewrite_sql("SELECT if(active, 'a,b', 'c') FROM t", FakeCapabilities())
    assert sql2 == "SELECT CASE WHEN active THEN 'a,b' ELSE 'c' END FROM t"


def test_mysql_passes_are_noops_for_oracle_body():
    # No backticks / no IF() in Oracle SQL -> the MySQL passes must not fire.
    _, applied, _ = rewrite_sql("SELECT NVL(a, b) FROM dual", FakeCapabilities())
    assert "keep:mysql_backtick_ident" not in applied
    assert "keep:mysql_if" not in applied


def test_rownum_le_becomes_limit():
    sql, applied, _ = rewrite_sql(
        "SELECT * FROM t WHERE ROWNUM <= 10", FakeCapabilities()
    )
    assert "LIMIT 10" in sql
    assert "ROWNUM" not in sql.upper()
    # The now-empty WHERE is removed.
    assert "WHERE" not in sql.upper()
    assert "keep:rownum_limit" in applied


def test_rownum_lt_becomes_limit_minus_one():
    sql, _, _ = rewrite_sql("SELECT * FROM t WHERE ROWNUM < 5", FakeCapabilities())
    assert "LIMIT 4" in sql


def test_rownum_with_other_predicate_keeps_where():
    sql, _, _ = rewrite_sql(
        "SELECT * FROM t WHERE active = 1 AND ROWNUM <= 3", FakeCapabilities()
    )
    assert "active = 1" in sql
    assert "LIMIT 3" in sql
    assert "ROWNUM" not in sql.upper()
    # leftover "AND AND" / dangling AND must not appear
    assert "AND  " not in sql.replace("AND  ", "")  # no double-space dangling


def test_oracle_outer_join_noted_not_rewritten():
    sql, applied, gaps = rewrite_sql(
        "SELECT * FROM a, b WHERE a.id = b.id (+)", FakeCapabilities()
    )
    assert "(+)" in sql  # left in place
    assert "note:oracle_outer_join" in applied
    assert any(g.feature.startswith("oracle-outer-join") for g in gaps)
    assert any(g.severity is Severity.BLOCKER for g in gaps)


# --------------------------------------------------------------------------- #
# DELEGATE passes (capability-gated)                                          #
# --------------------------------------------------------------------------- #

def test_nvl_passthrough_when_accepted():
    caps = FakeCapabilities(accepts={"nvl": True})
    sql, applied, gaps = rewrite_sql("SELECT NVL(a, 0) FROM t;", caps)
    assert "NVL(a, 0)" in sql  # untouched
    assert "COALESCE" not in sql.upper()
    assert "passthrough:nvl" in applied
    assert gaps == []


def test_nvl_translates_and_gaps_when_not_accepted():
    caps = FakeCapabilities(accepts={"nvl": False})
    sql, applied, gaps = rewrite_sql("SELECT NVL(a, 0) FROM t;", caps)
    assert "COALESCE(a, 0)" in sql
    assert "NVL(" not in sql.upper()
    assert "translate:nvl" in applied
    assert len(gaps) == 1
    assert gaps[0].feature == "oracle-function:nvl"
    assert gaps[0].edition is Edition.LITE
    assert gaps[0].severity is Severity.DEGRADED


def test_nvl_defaults_to_translate_when_key_absent():
    # No 'nvl' key and not known-native -> translate path.
    sql, applied, gaps = rewrite_sql("SELECT NVL(x, y) FROM t;", FakeCapabilities())
    assert "COALESCE(x, y)" in sql
    assert "translate:nvl" in applied
    assert len(gaps) == 1


def test_decode_translates_to_case():
    sql, applied, gaps = rewrite_sql(
        "SELECT DECODE(status, 1, 'A', 2, 'B', 'X') FROM t;", FakeCapabilities()
    )
    assert "CASE status WHEN 1 THEN 'A' WHEN 2 THEN 'B' ELSE 'X' END" in sql
    assert "translate:decode" in applied
    assert any(g.feature == "oracle-function:decode" for g in gaps)


def test_sysdate_translates_when_not_native():
    sql, applied, gaps = rewrite_sql("SELECT SYSDATE FROM dual;", FakeCapabilities())
    assert "CURRENT_TIMESTAMP" in sql
    assert "SYSDATE" not in sql.upper()
    assert "translate:sysdate" in applied
    # FROM DUAL also stripped on the same pass.
    assert "DUAL" not in sql.upper()


def test_sysdate_passthrough_when_native():
    caps = FakeCapabilities(accepts={"sysdate": True})
    sql, applied, _ = rewrite_sql("SELECT SYSDATE;", caps)
    assert "SYSDATE" in sql.upper()
    assert "passthrough:sysdate" in applied


def test_to_char_flagged_not_translated():
    sql, applied, gaps = rewrite_sql(
        "SELECT TO_CHAR(d, 'YYYY') FROM t;", FakeCapabilities()
    )
    # Left in place (format models differ), but a gap is recorded.
    assert "TO_CHAR(" in sql.upper()
    assert "flag:to_char" in applied
    assert any(g.feature == "oracle-function:to_char" for g in gaps)


# --------------------------------------------------------------------------- #
# Cost scoring                                                                #
# --------------------------------------------------------------------------- #

def test_score_routine_counts_constructs():
    body = """
    DECLARE CURSOR c1 IS SELECT * FROM t;
            CURSOR c2 IS SELECT * FROM u;
    BEGIN
        FORALL i IN 1..10 INSERT INTO x VALUES (i);
        SELECT col BULK COLLECT INTO arr FROM y;
        EXECUTE IMMEDIATE 'truncate table z';
        DBMS_OUTPUT.PUT_LINE('hi');
        SELECT * FROM emp CONNECT BY PRIOR id = mgr;
        PRAGMA AUTONOMOUS_TRANSACTION;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
    """
    # 2 cursors(2*2=4) + exception(2) + dbms_(3) + connect by(5) +
    # bulk collect(4) + forall(4) + pragma autonomous(5) + execute immediate(3)
    assert score_routine(body) == 4 + 2 + 3 + 5 + 4 + 4 + 5 + 3


def test_score_routine_case_insensitive_and_empty():
    assert score_routine("cursor x; CURSOR y;") == 4  # two cursors regardless of case
    assert score_routine("") == 0
    assert score_routine(None) == 0


def test_to_person_days():
    assert to_person_days(0) == 0.0
    assert to_person_days(30) == 1.5  # 30 * 0.05
    assert to_person_days(7) == 0.35
    assert to_person_days(10, factor=0.1) == 1.0


# --------------------------------------------------------------------------- #
# GapReport                                                                   #
# --------------------------------------------------------------------------- #

def _gap(feature="oracle-function:nvl", obj=None, occ=1, sev=Severity.DEGRADED):
    return TargetGap(
        feature=feature,
        edition=Edition.LITE,
        object_ref=obj,
        occurrences=occ,
        severity=sev,
        workaround=None,
        recommendation="add it",
    )


def test_gap_report_dedups_and_sums_occurrences():
    r = GapReport()
    r.add(_gap(occ=1))
    r.add(_gap(occ=2))  # same (feature, object_ref) -> merged
    assert len(r) == 1
    assert r.gaps[0].occurrences == 3


def test_gap_report_distinct_keys_kept_separate():
    r = GapReport()
    r.add(_gap(feature="oracle-function:nvl"))
    r.add(_gap(feature="oracle-function:decode"))
    r.add(_gap(feature="oracle-function:nvl", obj="PKG.FN"))  # diff object_ref
    assert len(r) == 3


def test_gap_report_render_text_and_json():
    r = GapReport()
    r.add(_gap(occ=2, obj="PKG.FN"))
    text = r.render_text()
    assert "oracle-function:nvl" in text
    assert "x2" in text
    assert "PKG.FN" in text

    payload = json.loads(r.render_json())
    assert isinstance(payload, list)
    assert payload[0]["feature"] == "oracle-function:nvl"
    assert payload[0]["occurrences"] == 2
    assert payload[0]["severity"] == "degraded"
    assert payload[0]["edition"] == "lite"


def test_gap_report_empty_render():
    r = GapReport()
    assert not r
    assert r.render_text() == "No target gaps found."
    assert json.loads(r.render_json()) == []
