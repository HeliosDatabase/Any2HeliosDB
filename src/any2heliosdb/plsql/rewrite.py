"""Thin, capability-gated Oracle SQL -> HeliosDB SQL rewriter.

Two kinds of pass run over a statement:

* **KEEP passes** — always applied because the construct is Oracle-only syntax
  that no HeliosDB edition accepts on the PG-wire path (``seq.NEXTVAL``,
  ``SYS_GUID()``, ``FROM DUAL``, ``ROWNUM`` row-limiting). Each pass that fires
  records its name in the returned ``applied`` list.

* **DELEGATE passes** — gated on the target's :class:`CapabilityMatrix`. These
  cover functions HeliosDB *might* implement natively (``NVL``, ``DECODE``,
  ``SYSDATE``, ...). Following the project's "fix in HeliosDB, not the tool"
  principle: if the target advertises the function (``capabilities.accepts[fn]``)
  or it is in our small known-native allow-list, we pass the text through
  untouched. Otherwise we translate it to portable SQL *and* record a
  :class:`TargetGap` recommending the function be added to the target so a future
  run can stop translating.

The regexes are deliberately simple (single-statement, well-commented) — this is
a pragmatic rewriter for the common cases, not a PL/SQL parser. Anything it
cannot safely rewrite (e.g. Oracle ``(+)`` outer joins) is left in place and
reported as a gap.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from ..constants import Edition, Severity
from ..core.identifiers import quote_ident
from .gap import TargetGap

# Functions we treat as "the target already has it" even if the probe did not
# explicitly flag them — these are standard SQL and present on every edition.
_KNOWN_NATIVE = frozenset({"coalesce", "case", "current_timestamp"})

# Delegate-able Oracle functions this rewriter knows how to translate. Anything
# not here is never touched by a delegate pass.
_DELEGATABLE = ("nvl", "nvl2", "decode", "sysdate", "systimestamp", "to_char", "to_date")


def _edition(capabilities) -> Edition:
    """Best-effort edition off the capability matrix (for gap attribution)."""
    return getattr(capabilities, "edition", Edition.UNKNOWN) or Edition.UNKNOWN


def _native(capabilities, fn: str) -> bool:
    """True if the target accepts ``fn`` natively (probe flag or known-native)."""
    if fn in _KNOWN_NATIVE:
        return True
    accepts = getattr(capabilities, "accepts", {}) or {}
    return bool(accepts.get(fn, False))


# --------------------------------------------------------------------------- #
# KEEP passes — always applied (Oracle-only syntax)                           #
# --------------------------------------------------------------------------- #

# seq.NEXTVAL / seq.CURRVAL -> nextval('seq') / currval('seq'). The sequence
# name is a (possibly schema-qualified) identifier; we keep it verbatim inside
# the quotes so PG resolves it the same way.
_RE_SEQVAL = re.compile(
    r"\b([A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)?)\.(NEXTVAL|CURRVAL)\b",
    re.IGNORECASE,
)

# SYS_GUID() -> gen_random_uuid()
_RE_SYS_GUID = re.compile(r"\bSYS_GUID\s*\(\s*\)", re.IGNORECASE)

# FROM DUAL — Oracle's one-row dummy table; PG has no DUAL, so strip it.
# Matches "FROM DUAL" optionally followed by end / clause keyword / ';' / ')'.
_RE_FROM_DUAL = re.compile(r"\s+FROM\s+DUAL\b", re.IGNORECASE)

# Simple ROWNUM row-limiting in a WHERE clause: "ROWNUM <= n" / "ROWNUM < n".
# We capture the operator and n so "< n" becomes LIMIT n-1 (Oracle semantics).
_RE_ROWNUM = re.compile(
    r"\bROWNUM\s*(<=|<)\s*(\d+)\b",
    re.IGNORECASE,
)

# Oracle (+) outer-join marker, e.g. "a.id = b.id (+)". Detected, not rewritten.
_RE_OUTER_JOIN = re.compile(r"\(\s*\+\s*\)")


def _keep_seqval(sql: str) -> Tuple[str, bool]:
    new, n = _RE_SEQVAL.subn(
        lambda m: "{}('{}')".format(
            "nextval" if m.group(2).lower() == "nextval" else "currval", m.group(1)
        ),
        sql,
    )
    return new, n > 0


def _keep_sys_guid(sql: str) -> Tuple[str, bool]:
    new, n = _RE_SYS_GUID.subn("gen_random_uuid()", sql)
    return new, n > 0


def _keep_from_dual(sql: str) -> Tuple[str, bool]:
    new, n = _RE_FROM_DUAL.subn("", sql)
    return new, n > 0


def _keep_rownum_limit(sql: str) -> Tuple[str, bool]:
    """Turn a single ``ROWNUM <|<= n`` predicate into a trailing LIMIT.

    Handles the predicate joined by AND on either side and a bare
    ``WHERE ROWNUM <= n``. Only the first such predicate is converted (the
    common pattern); anything more exotic is left for a human.
    """
    m = _RE_ROWNUM.search(sql)
    if not m:
        return sql, False
    op, num = m.group(1), int(m.group(2))
    limit = num if op == "<=" else num - 1

    # Remove the rownum predicate, including a neighbouring AND if present, so we
    # don't leave a dangling "WHERE AND" / "AND AND".
    start, end = m.span()
    before = sql[:start]
    after = sql[end:]

    # Drop an AND immediately before (with its whitespace) ...
    before_stripped = re.sub(r"\s+AND\s*$", " ", before, flags=re.IGNORECASE)
    if before_stripped != before:
        before = before_stripped
    else:
        # ... otherwise drop an AND immediately after.
        after = re.sub(r"^\s*AND\s+", " ", after, flags=re.IGNORECASE)

    rewritten = before + after
    # If removing the predicate left an empty WHERE (e.g. "WHERE  LIMIT"), drop it.
    rewritten = re.sub(r"\bWHERE\s+(?=(ORDER\b|GROUP\b|LIMIT\b|$|;|\)))", "", rewritten, flags=re.IGNORECASE)
    rewritten = rewritten.rstrip()

    # Append LIMIT before a trailing ';' if there is one.
    if rewritten.endswith(";"):
        rewritten = "{} LIMIT {};".format(rewritten[:-1].rstrip(), limit)
    else:
        rewritten = "{} LIMIT {}".format(rewritten, limit)
    return rewritten, True


# --------------------------------------------------------------------------- #
# KEEP passes — MySQL source-dialect normalization (no PG-family equivalent)  #
# --------------------------------------------------------------------------- #

# MySQL backtick-quoted identifier. MySQL is the only source dialect that
# backtick-quotes identifiers; Oracle/PG view bodies never contain backticks,
# so this pass is a no-op for them.
_RE_BACKTICK = re.compile(r"`([^`]+)`")

# MySQL scalar IF(cond, a, b) — PostgreSQL has no scalar IF() function.
_RE_IF_CALL = re.compile(r"\bIF\s*\(", re.IGNORECASE)


def _keep_mysql_backtick_idents(sql: str) -> Tuple[str, bool]:
    """``\\`ident\\``` -> a PostgreSQL identifier via the shared quoter.

    A plain ``lower_snake`` name renders bare; a name with spaces, a reserved
    word, or mixed case is double-quoted (so ``AS \\`zip code\\``` becomes
    ``AS "zip code"``). Without this, a MySQL view alias containing a space
    reaches a PG-wire target unquoted and the parser rejects the statement.
    """
    new, n = _RE_BACKTICK.subn(lambda m: quote_ident(m.group(1)), sql)
    return new, n > 0


def _matching_paren(sql: str, open_idx: int) -> int:
    """Index of the ``)`` matching the ``(`` at *open_idx*, or -1 if unbalanced.

    Parens inside single/double-quoted string literals are ignored.
    """
    depth = 0
    quote: Optional[str] = None
    i = open_idx
    while i < len(sql):
        ch = sql[i]
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _keep_mysql_if(sql: str) -> Tuple[str, bool]:
    """``IF(cond, a, b)`` -> ``CASE WHEN cond THEN a ELSE b END``.

    PostgreSQL (and HeliosDB over PG-wire) has no scalar ``IF()``; MySQL view
    bodies use it (e.g. Sakila ``staff_list`` / ``customer_list``). Balanced-paren
    extraction plus a top-level comma split handle nested calls and commas inside
    the arguments; a nested ``IF`` is rewritten by re-scanning from the same
    offset. Only the canonical 3-argument form is translated; any other arity is
    left in place.
    """
    fired = False
    pos = 0
    while True:
        m = _RE_IF_CALL.search(sql, pos)
        if not m:
            break
        open_idx = m.end() - 1
        close_idx = _matching_paren(sql, open_idx)
        if close_idx == -1:
            break
        args = _split_top_level(sql[open_idx + 1:close_idx])
        if len(args) != 3:
            pos = m.end()  # not a translatable IF(); keep scanning past it
            continue
        cond, then_val, else_val = (a.strip() for a in args)
        rep = "CASE WHEN {} THEN {} ELSE {} END".format(cond, then_val, else_val)
        sql = sql[:m.start()] + rep + sql[close_idx + 1:]
        fired = True
        pos = m.start()  # re-scan from here so a nested IF inside rep is caught
    return sql, fired


# MySQL GROUP_CONCAT([DISTINCT] expr [ORDER BY ...] [SEPARATOR sep]).
_RE_GROUP_CONCAT = re.compile(r"\bGROUP_CONCAT\s*\(", re.IGNORECASE)
_RE_GC_SEPARATOR = re.compile(r"SEPARATOR\b", re.IGNORECASE)
_RE_GC_ORDER_BY = re.compile(r"ORDER\s+BY\b", re.IGNORECASE)
_RE_GC_DISTINCT = re.compile(r"^\s*DISTINCT\b", re.IGNORECASE)


def _find_top_level_kw(s: str, kw: "re.Pattern[str]") -> int:
    """Index of the first match of *kw* at paren-depth 0 and outside string
    literals, or -1. Lets a GROUP_CONCAT find its own ORDER BY / SEPARATOR
    without matching one that belongs to a nested GROUP_CONCAT in a subquery.
    """
    depth = 0
    quote: Optional[str] = None
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and kw.match(s, i):
            return i
        i += 1
    return -1


def _keep_mysql_group_concat(sql: str) -> Tuple[str, bool]:
    """``GROUP_CONCAT([DISTINCT] expr [ORDER BY ...] [SEPARATOR s])`` ->
    ``string_agg([DISTINCT] (expr)::text, s [ORDER BY ...])``.

    PostgreSQL has no GROUP_CONCAT; ``string_agg`` is the equivalent but *requires*
    a delimiter (MySQL defaults to ``,``) and places ORDER BY after it. The value
    is cast to text so non-text columns aggregate too. Balanced-paren extraction +
    top-level clause detection means a nested GROUP_CONCAT in a subquery (with its
    own ORDER BY / SEPARATOR) is rewritten by re-scanning from the same offset.
    """
    fired = False
    pos = 0
    while True:
        m = _RE_GROUP_CONCAT.search(sql, pos)
        if not m:
            break
        open_idx = m.end() - 1
        close_idx = _matching_paren(sql, open_idx)
        if close_idx == -1:
            break
        inner = sql[open_idx + 1:close_idx]
        si = _find_top_level_kw(inner, _RE_GC_SEPARATOR)
        if si >= 0:
            sep = inner[si + len("SEPARATOR"):].strip()
            head = inner[:si]
        else:
            sep = "','"            # MySQL's default GROUP_CONCAT separator
            head = inner
        oi = _find_top_level_kw(head, _RE_GC_ORDER_BY)
        if oi >= 0:
            order_by = " " + head[oi:].strip()
            head = head[:oi]
        else:
            order_by = ""
        distinct = ""
        dm = _RE_GC_DISTINCT.match(head)
        if dm:
            distinct = "DISTINCT "
            head = head[dm.end():]
        value = head.strip()
        # MySQL GROUP_CONCAT(a, b, ...) concatenates its args; PG takes one expr.
        if len(_split_top_level(value)) > 1:
            value = "concat({})".format(value)
        cast_value = "({})::text".format(value)
        if distinct and order_by:
            # PostgreSQL requires the ORDER BY of a DISTINCT aggregate to be over
            # the DISTINCT argument; MySQL allows ordering by an unrelated column.
            # Order by the aggregated value instead -> valid and deterministic.
            order_by = " ORDER BY {}".format(cast_value)
        rep = "string_agg({}{}, {}{})".format(distinct, cast_value, sep, order_by)
        sql = sql[:m.start()] + rep + sql[close_idx + 1:]
        fired = True
        pos = m.start()  # re-scan so a nested GROUP_CONCAT in rep is caught
    return sql, fired


# Loose MySQL GROUP BY -> strict PostgreSQL GROUP BY.
_RE_SELECT = re.compile(r"\bSELECT\b", re.IGNORECASE)
_RE_FROM = re.compile(r"\bFROM\b", re.IGNORECASE)
_RE_GROUP_BY = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
_RE_GB_END = re.compile(r"\b(?:ORDER\s+BY|HAVING|LIMIT|OFFSET|WINDOW|UNION|INTERSECT|EXCEPT)\b",
                        re.IGNORECASE)
_RE_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_AGG_FNS = frozenset({
    "sum", "count", "avg", "min", "max", "string_agg", "group_concat", "array_agg",
    "json_agg", "jsonb_agg", "bool_and", "bool_or", "every", "bit_and", "bit_or",
    "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop", "var_samp",
})


def _nonagg_qualified_refs(s: str) -> List[str]:
    """Ordered, unique qualified column refs (``alias.col``) in *s* that are not
    arguments of an aggregate function and are not themselves function calls."""
    refs: List[str] = []
    seen = set()
    stack: List[bool] = []      # one flag per open paren: is it an aggregate's?
    quote: Optional[str] = None
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if quote is not None:
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            continue
        if ch == "(":
            j = i - 1
            while j >= 0 and s[j].isspace():
                j -= 1
            k = j
            while k >= 0 and (s[k].isalnum() or s[k] == "_"):
                k -= 1
            stack.append(s[k + 1:j + 1].lower() in _AGG_FNS)
            i += 1
            continue
        if ch == ")":
            if stack:
                stack.pop()
            i += 1
            continue
        m = _RE_WORD.match(s, i)
        if not m:
            i += 1
            continue
        end = m.end()
        if end < n and s[end] == ".":
            m2 = _RE_WORD.match(s, end + 1)
            if m2:
                ref = "{}.{}".format(m.group(0), m2.group(0))
                end = m2.end()
                e2 = end
                while e2 < n and s[e2].isspace():
                    e2 += 1
                is_call = e2 < n and s[e2] == "("
                if not any(stack) and not is_call and ref.lower() not in seen:
                    seen.add(ref.lower())
                    refs.append(ref)
        i = end
    return refs


def _keep_group_by_nonaggregates(sql: str) -> Tuple[str, bool]:
    """Add a grouped query's non-aggregated SELECT columns to its GROUP BY.

    MySQL (with ``ONLY_FULL_GROUP_BY`` off) allows selecting columns that are
    neither aggregated nor grouped; PostgreSQL rejects them ("column ... must
    appear in the GROUP BY clause"). We append every qualified, non-aggregate
    column reference from the outer SELECT list that isn't already grouped. Only
    the outermost query is touched (a subquery's SELECT/GROUP BY sit at paren
    depth > 0, so the top-level scan skips them).
    """
    gb = _find_top_level_kw(sql, _RE_GROUP_BY)
    if gb < 0:
        return sql, False
    sel = _find_top_level_kw(sql, _RE_SELECT)
    frm = _find_top_level_kw(sql, _RE_FROM)
    if sel < 0 or frm < 0 or not (sel < frm < gb):
        return sql, False
    # sel/gb are positions where these exact patterns already matched (via
    # _find_top_level_kw), so re-matching at them can never be None.
    sel_m = _RE_SELECT.match(sql, sel)
    gb_m = _RE_GROUP_BY.match(sql, gb)
    assert sel_m is not None and gb_m is not None
    select_list = sql[sel_m.end():frm]
    gb_kw_end = gb_m.end()
    rest = sql[gb_kw_end:]
    cut = len(rest)
    kw_at = _find_top_level_kw(rest, _RE_GB_END)
    if kw_at >= 0:
        cut = min(cut, kw_at)
    semi = rest.find(";")
    if semi >= 0:
        cut = min(cut, semi)
    existing = [x.strip() for x in _split_top_level(rest[:cut]) if x.strip()]
    existing_lower = {e.lower() for e in existing}
    additions = [r for r in _nonagg_qualified_refs(select_list)
                 if r.lower() not in existing_lower]
    if not additions:
        return sql, False
    new_clause = " " + ", ".join(existing + additions) + " "
    return sql[:gb_kw_end] + new_clause + rest[cut:], True


# --------------------------------------------------------------------------- #
# DELEGATE passes — capability-gated function translation                     #
# --------------------------------------------------------------------------- #

# NVL(a, b) -> COALESCE(a, b). Two top-level args, no nested-paren handling
# needed for the simple/common case.
_RE_NVL = re.compile(r"\bNVL\s*\(\s*([^,()]+?)\s*,\s*([^()]+?)\s*\)", re.IGNORECASE)

# NVL2(a, if_not_null, if_null) -> CASE WHEN a IS NOT NULL THEN .. ELSE .. END
_RE_NVL2 = re.compile(
    r"\bNVL2\s*\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*,\s*([^()]+?)\s*\)", re.IGNORECASE
)

# DECODE(expr, s1, r1, s2, r2, ..., [default]) -> searched CASE.
_RE_DECODE = re.compile(r"\bDECODE\s*\((.*?)\)", re.IGNORECASE | re.DOTALL)

# Bare keyword functions (no parens): SYSDATE / SYSTIMESTAMP.
_RE_SYSDATE = re.compile(r"\bSYSDATE\b", re.IGNORECASE)
_RE_SYSTIMESTAMP = re.compile(r"\bSYSTIMESTAMP\b", re.IGNORECASE)


def _split_top_level(arg_str: str) -> List[str]:
    """Split an argument list on top-level commas.

    Commas inside nested parens *and* inside single/double-quoted string literals
    are not split points (so ``if(x, 'a,b', 'c')`` keeps three args). Used by both
    the DECODE and IF translators.
    """
    parts: List[str] = []
    depth = 0
    quote: Optional[str] = None
    cur: List[str] = []
    for ch in arg_str:
        if quote is not None:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == "(":
            depth += 1
            cur.append(ch)
        elif ch == ")":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def _decode_to_case(arg_str: str) -> str:
    """DECODE(expr, s1, r1, ..., [default]) -> CASE expr WHEN s1 THEN r1 ... END."""
    args = _split_top_level(arg_str)
    if len(args) < 3:
        # Not a shape we understand; hand it back unchanged.
        return "DECODE({})".format(arg_str)
    expr = args[0]
    rest = args[1:]
    pieces = ["CASE {}".format(expr)]
    i = 0
    while i + 1 < len(rest):
        pieces.append("WHEN {} THEN {}".format(rest[i], rest[i + 1]))
        i += 2
    if i < len(rest):  # trailing odd arg = default
        pieces.append("ELSE {}".format(rest[i]))
    pieces.append("END")
    return " ".join(pieces)


def _gap(fn: str, capabilities, occurrences: int) -> TargetGap:
    """Build the standard 'add this function to HeliosDB' gap for ``fn``."""
    return TargetGap(
        feature="oracle-function:{}".format(fn),
        edition=_edition(capabilities),
        object_ref=None,
        occurrences=occurrences,
        severity=Severity.DEGRADED,
        workaround="Tool translated {}() to portable SQL for this run.".format(fn.upper()),
        recommendation=(
            "Add native {}() support to the HeliosDB target so the migration "
            "can pass it through unchanged.".format(fn.upper())
        ),
    )


def _delegate_fn(
    sql: str,
    fn: str,
    pattern: "re.Pattern[str]",
    translate,
    capabilities,
    applied: List[str],
    gaps: List[TargetGap],
) -> str:
    """Run one delegate pass for ``fn``.

    If the target is native for ``fn`` we leave matches alone (passthrough);
    otherwise we translate every match and record one summed gap.
    """
    matches = pattern.findall(sql)
    if not matches:
        return sql
    if _native(capabilities, fn):
        applied.append("passthrough:{}".format(fn))
        return sql
    count = len(matches)
    new = pattern.sub(translate, sql)
    applied.append("translate:{}".format(fn))
    gaps.append(_gap(fn, capabilities, count))
    return new


# --------------------------------------------------------------------------- #
# Public entry point                                                          #
# --------------------------------------------------------------------------- #

def rewrite_sql(sql: str, capabilities) -> Tuple[str, List[str], List[TargetGap]]:
    """Rewrite one Oracle SQL statement for the given HeliosDB target.

    Returns ``(rewritten_sql, applied_passes, gaps)``.
    """
    applied: List[str] = []
    gaps: List[TargetGap] = []
    if not sql:
        return sql, applied, gaps
    out = sql

    # --- KEEP passes (order matters: ROWNUM after DUAL so LIMIT lands cleanly).
    out, fired = _keep_seqval(out)
    if fired:
        applied.append("keep:nextval")
    out, fired = _keep_sys_guid(out)
    if fired:
        applied.append("keep:sys_guid")
    out, fired = _keep_from_dual(out)
    if fired:
        applied.append("keep:from_dual")
    out, fired = _keep_rownum_limit(out)
    if fired:
        applied.append("keep:rownum_limit")

    # MySQL source-dialect normalization (no-op for Oracle/PG bodies): convert
    # backtick identifiers to PG quoting and IF() to CASE so a MySQL view body
    # is valid SQL on every PG-wire target.
    out, fired = _keep_mysql_backtick_idents(out)
    if fired:
        applied.append("keep:mysql_backtick_ident")
    out, fired = _keep_mysql_if(out)
    if fired:
        applied.append("keep:mysql_if")
    out, fired = _keep_mysql_group_concat(out)
    if fired:
        applied.append("keep:mysql_group_concat")
    out, fired = _keep_group_by_nonaggregates(out)
    if fired:
        applied.append("keep:group_by_nonaggregates")

    # Oracle (+) outer join — detected only, never silently rewritten.
    oj = _RE_OUTER_JOIN.findall(out)
    if oj:
        applied.append("note:oracle_outer_join")
        gaps.append(
            TargetGap(
                feature="oracle-outer-join:(+)",
                edition=_edition(capabilities),
                object_ref=None,
                occurrences=len(oj),
                severity=Severity.BLOCKER,
                workaround="Rewrite the (+) join as an explicit LEFT/RIGHT OUTER JOIN by hand.",
                recommendation=(
                    "Convert Oracle (+) outer-join syntax to ANSI OUTER JOIN; "
                    "the tool does not auto-rewrite it."
                ),
            )
        )

    # --- DELEGATE passes (capability-gated).
    out = _delegate_fn(
        out, "nvl", _RE_NVL,
        lambda m: "COALESCE({}, {})".format(m.group(1).strip(), m.group(2).strip()),
        capabilities, applied, gaps,
    )
    out = _delegate_fn(
        out, "nvl2", _RE_NVL2,
        lambda m: "CASE WHEN {} IS NOT NULL THEN {} ELSE {} END".format(
            m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        ),
        capabilities, applied, gaps,
    )
    out = _delegate_fn(
        out, "decode", _RE_DECODE,
        lambda m: _decode_to_case(m.group(1)),
        capabilities, applied, gaps,
    )
    out = _delegate_fn(
        out, "sysdate", _RE_SYSDATE,
        lambda m: "CURRENT_TIMESTAMP",
        capabilities, applied, gaps,
    )
    out = _delegate_fn(
        out, "systimestamp", _RE_SYSTIMESTAMP,
        lambda m: "CURRENT_TIMESTAMP",
        capabilities, applied, gaps,
    )
    # TO_CHAR / TO_DATE: format models differ between Oracle and PG, so we never
    # silently translate them. When the target is not native we record a gap and
    # leave the call in place for human review.
    for fn, pat in (("to_char", re.compile(r"\bTO_CHAR\s*\(", re.IGNORECASE)),
                    ("to_date", re.compile(r"\bTO_DATE\s*\(", re.IGNORECASE))):
        hits = pat.findall(out)
        if not hits:
            continue
        if _native(capabilities, fn):
            applied.append("passthrough:{}".format(fn))
        else:
            applied.append("flag:{}".format(fn))
            g = _gap(fn, capabilities, len(hits))
            # Override workaround: we did NOT translate these.
            gaps.append(
                TargetGap(
                    feature=g.feature,
                    edition=g.edition,
                    object_ref=None,
                    occurrences=len(hits),
                    severity=Severity.DEGRADED,
                    workaround="Left {}() in place; verify the format model on the target.".format(fn.upper()),
                    recommendation=g.recommendation,
                )
            )

    return out, applied, gaps
