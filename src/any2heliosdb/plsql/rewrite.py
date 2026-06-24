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
    """Split a DECODE arg list on top-level commas (respecting parens)."""
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    for ch in arg_str:
        if ch == "(":
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
