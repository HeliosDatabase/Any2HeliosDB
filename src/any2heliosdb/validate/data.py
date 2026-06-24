"""TEST_DATA: content-level parity via per-row SHA256 comparison.

Mirrors Ora2Pg's ``-t TEST_DATA``: sample up to ``sample_rows`` rows from both
sides ordered by the primary key, hash each row's values identically on both
sides, and report rows that differ (stopping after ``max_errors`` so a badly
diverged table fails fast). A table with no primary key cannot be aligned
row-for-row, so it is validated by comparing the order-independent MULTISET of
per-row checksums (sorted) on both sides — a keyless table is still checked, not
skipped.

Row hashing is value-by-value with a field separator, normalizing ``None`` and
rendering everything through ``str`` so the source tuple and the target tuple
produce the same digest for equal data regardless of driver representation.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
from decimal import Decimal, InvalidOperation
from typing import Iterable, List, Sequence, Tuple

from ..constants import Severity
from ..core.catalog_model import Table
from ..core.identifiers import quote_table as _quote_table, render_ident as _ident
from ..sources.base import SourceAdapter
from ..target.base import TargetDriver
from .model import ValidationResult, ValidationType

# Separators chosen to be vanishingly unlikely inside rendered field values, so
# distinct field boundaries cannot collide (e.g. ["a", "b"] vs ["a|b"]).
_NULL_SENTINEL = "\x00\\N\x00"
_FIELD_SEP = "\x01"


def _pg_array_literal(seq: object) -> str:
    """Serialize a Python list/tuple to the PostgreSQL array literal text form.

    A PG ``text[]`` column (which a2h maps to TEXT on the target) is loaded by
    psycopg's COPY as the array literal ``{a,"b c",NULL}``; the source side hands
    the same column back as a native Python list. This reproduces psycopg/PG's
    ``array_out`` so the two render identically: an element is double-quoted iff
    it is empty, equals NULL, or contains a brace/comma/quote/backslash/whitespace
    (with ``"`` and ``\\`` backslash-escaped); NULL elements render bare as NULL.
    """
    parts = []
    for el in seq:  # type: ignore[union-attr]
        if el is None:
            parts.append("NULL")
        elif isinstance(el, (list, tuple)):
            parts.append(_pg_array_literal(el))
        else:
            s = "1" if el is True else "0" if el is False else str(el)
            if (s == "" or s.upper() == "NULL"
                    or any(ch in s for ch in '{},"\\') or any(ch.isspace() for ch in s)):
                s = '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
            parts.append(s)
    return "{" + ",".join(parts) + "}"


def _render(v: object) -> str:
    """Canonical rendering of one field value, robust to driver representation.

    Binary is normalized to a hex string so an Oracle ``bytes`` BLOB and a psycopg
    ``memoryview`` bytea with identical content hash equal (they don't under bare
    ``str()``). Everything else renders through ``str`` (numbers/dates/text agree
    across the two drivers under ``str``)."""
    if v is None:
        return _NULL_SENTINEL
    if isinstance(v, bool):
        # A boolean-ish source column (e.g. MySQL TINYINT(1) -> BOOLEAN) comes
        # back as 1/0 on one side and True/False on the other; canonicalize so
        # they hash equal. (bool must be checked before int, being a subclass.)
        return "1" if v else "0"
    if isinstance(v, (Decimal, float)):
        # Numeric representation varies by driver/target: a NUMBER(10,2) salary
        # comes back from Oracle as float 125000.5, from MySQL as Decimal with
        # the column scale's trailing zeros (125000.50), and from HeliosDB as
        # Decimal('125000.5'). Canonicalize to a trailing-zero-free decimal string
        # so semantically-equal numbers hash equal regardless of representation
        # (different *values* still differ — this never masks a real mismatch).
        try:
            d = Decimal(str(v)).normalize()
            # normalize() can yield scientific notation (e.g. 1E+2); expand it,
            # and collapse a negative-zero to "0".
            s = format(d, "f")
            return s if s != "-0" else "0"
        except (InvalidOperation, ValueError):
            return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        b = v.tobytes() if isinstance(v, memoryview) else bytes(v)
        # Some targets return BYTEA as the PostgreSQL hex *string* '\xDEADBEEF'
        # (a bytea type-OID quirk) — and may hand it back typed as bytes, so the
        # client gets the literal b'\\x...' rather than the decoded value.
        # Decode it to the same hex the source's raw bytes produce.
        if len(b) >= 2 and b[:2] == b"\\x":
            body = b[2:]
            if body and len(body) % 2 == 0 and all(chr(c) in "0123456789abcdefABCDEF" for c in body):
                return "\x00BIN\x00" + body.decode("ascii").lower()
        return "\x00BIN\x00" + b.hex()
    if isinstance(v, str) and len(v) >= 2 and v[0] == "\\" and v[1] == "x" and len(v) % 2 == 0:
        # Same quirk, but the target typed it as text so psycopg returns a str.
        body = v[2:]
        if body and all(c in "0123456789abcdefABCDEF" for c in body):
            return "\x00BIN\x00" + body.lower()
    if isinstance(v, _dt.datetime):
        # A timestamptz round-trips tz-aware from one side (source PostgreSQL hands
        # it back in the session tz) and naive from another (HeliosDB returns
        # timestamptz over the wire without the offset). The stored *instant* is
        # the same, so compare on that: convert an aware value to UTC and drop the
        # tzinfo; treat a naive value as already-UTC. Microseconds are preserved.
        if v.tzinfo is not None:
            v = v.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return "\x00TS\x00" + v.isoformat(sep=" ")
    if isinstance(v, (list, tuple)):
        # An ARRAY source column (a2h maps it to TEXT on the target, where it is
        # stored as the PG array literal). Render the native list the same way so
        # the source list and the target text hash equal.
        return _pg_array_literal(v)
    return str(v)


def row_checksum(values: Iterable[object]) -> str:
    """SHA256 hex digest of a row's values (order-sensitive, NULL-aware)."""
    h = hashlib.sha256()
    first = True
    for v in values:
        if not first:
            h.update(_FIELD_SEP.encode("utf-8"))
        first = False
        h.update(_render(v).encode("utf-8"))
    return h.hexdigest()


def _column_names(table: Table) -> List[str]:
    return [c.name for c in table.columns]


def _table(table: Table, preserve_case: bool) -> str:
    """Quote the table's (already case-folded) target name for direct SQL.

    ``target_name`` applies the case policy; this adds quoting only when needed
    (reserved word / mixed case), matching exactly what the loader and the DDL
    emitter produce so the validators query the same name that was created.
    """
    return _quote_table(table.target_name(preserve_case))


def _source_rows(
    source: SourceAdapter, table: Table, columns: Sequence[str], pk_cols: Sequence[str], limit: int
) -> List[Tuple]:
    """Pull rows from the source and order/limit by PK client-side.

    ``stream_rows`` is order-agnostic, so we sort by the PK-column positions here
    to guarantee both sides are aligned identically before hashing.
    """
    pk_idx = [columns.index(c) for c in pk_cols]
    rows = list(source.stream_rows(table, columns))
    rows.sort(key=lambda r: tuple(r[i] for i in pk_idx))
    return rows[:limit]


def _target_rows(
    target: TargetDriver, table: Table, columns: Sequence[str], pk_cols: Sequence[str],
    limit: int, preserve_case: bool,
) -> List[Tuple]:
    cols = ", ".join(_ident(c, preserve_case) for c in columns)
    order = ", ".join(_ident(c, preserve_case) for c in pk_cols)
    sql = "SELECT {} FROM {} ORDER BY {} LIMIT {}".format(
        cols, _table(table, preserve_case), order, int(limit)
    )
    return target.query(sql)


def run_test_data(
    source: SourceAdapter,
    target: TargetDriver,
    table: Table,
    sample_rows: int = 1000,
    max_errors: int = 10,
    preserve_case: bool = False,
) -> ValidationResult:
    """Compare up to *sample_rows* rows of *table* by per-row SHA256.

    ``sample_rows <= 0`` compares every row.
    """
    result = ValidationResult(validation_type=ValidationType.TEST_DATA)
    columns = _column_names(table)

    pk = table.primary_key
    if pk is None or not pk.columns:
        # No PK: rows can't be aligned 1:1, so compare the order-independent
        # MULTISET of per-row checksums (sorted) instead of declaring success.
        # This actually validates a keyless audit/log table rather than skipping
        # it (a skipped table could be fully corrupt and still "pass").
        src_hashes = sorted(row_checksum(r) for r in source.stream_rows(table, columns))
        tcols = ", ".join(_ident(c, preserve_case) for c in columns)
        tgt_rows = target.query("SELECT {} FROM {}".format(tcols, _table(table, preserve_case)))
        tgt_hashes = sorted(row_checksum(r) for r in tgt_rows)
        result.metrics["rows_compared"] = len(src_hashes)
        result.metrics["no_pk_multiset"] = True
        if len(src_hashes) != len(tgt_hashes):
            result.add_error(Severity.BLOCKER, table.fqn,
                             "row count differs (source={}, target={})".format(
                                 len(src_hashes), len(tgt_hashes)))
            result.metrics["mismatches"] = abs(len(src_hashes) - len(tgt_hashes))
            return result
        mism = sum(1 for a, b in zip(src_hashes, tgt_hashes) if a != b)
        if mism:
            result.add_error(Severity.BLOCKER, table.fqn,
                             "{} row(s) differ (no-PK multiset compare)".format(mism))
        result.metrics["mismatches"] = mism
        return result

    limit = sample_rows if sample_rows > 0 else 1_000_000_000
    src_rows = _source_rows(source, table, columns, pk.columns, limit)
    tgt_rows = _target_rows(target, table, columns, pk.columns, limit, preserve_case)

    compared = min(len(src_rows), len(tgt_rows))
    mismatches = 0

    if len(src_rows) != len(tgt_rows):
        result.add_error(
            Severity.BLOCKER,
            table.fqn,
            "sampled row count differs (source={}, target={})".format(
                len(src_rows), len(tgt_rows)
            ),
        )

    for i in range(compared):
        if row_checksum(src_rows[i]) != row_checksum(tgt_rows[i]):
            mismatches += 1
            result.add_error(
                Severity.BLOCKER,
                table.fqn,
                "row {} checksum mismatch".format(i),
            )
            if mismatches >= max_errors:
                result.metrics["stopped_early"] = True
                break

    result.metrics["rows_compared"] = compared
    result.metrics["mismatches"] = mismatches
    return result
