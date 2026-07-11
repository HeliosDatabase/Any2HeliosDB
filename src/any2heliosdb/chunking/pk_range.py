"""Split a table into key-range chunks for parallel + resumable load.

A chunk is a half-open integer-PK range ``[lo, hi)``, computed from ``MIN``/
``MAX`` of the PK on a FRESH plan only. A resumed run does NOT recompute: it
replays the chunk rows recorded in the manifest (``Chunk.from_recorded`` /
``ResumableLoader._replay_plan``), recorded predicates included byte-for-byte,
so drifted live bounds can never skip or overlap the ranges already bookkept.
Tables with no single integer PK fall back to one whole-table chunk.

The same range is rendered for the source (Oracle, quoted/uppercase columns) and
the target (lowercased unless ``preserve_case``), so the loader can stream the
chunk from the source and idempotently DELETE the same range on the target.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from ..core.catalog_model import Table
from ..core.identifiers import render_ident


@dataclass
class Chunk:
    table: Table
    chunk_id: str
    pk_col: Optional[str] = None  # None => whole-table chunk
    lo: Optional[int] = None      # inclusive
    hi: Optional[int] = None      # exclusive
    # Explicit source-WHERE replayed verbatim on resume. When set (a chunk rebuilt
    # from its recorded manifest row) it is returned byte-for-byte, so a resumed
    # load reads the RECORDED range even if the live source bounds have drifted;
    # left None on a fresh plan, where source_where() derives from pk_col/lo/hi.
    predicate: Optional[str] = None

    def source_where(self) -> Optional[str]:
        # A replayed chunk returns its recorded predicate verbatim (byte-identical
        # to what the ledger stored), so a resume can never substitute a
        # recomputed live-bounds range for the one already partially loaded.
        if self.predicate is not None:
            return self.predicate
        if self.pk_col is None:
            return None
        # Quote the source PK column, doubling any embedded " (the source keeps its
        # own case, e.g. Oracle UPPER), so a column name containing a quote can't
        # break or alter the chunked read. This predicate is appended verbatim to
        # the source SELECT, so it must be self-safe.
        c = '"{}"'.format(self.pk_col.replace('"', '""'))
        return "{c} >= {lo} AND {c} < {hi}".format(c=c, lo=self.lo, hi=self.hi)

    @classmethod
    def from_recorded(cls, table: Table, chunk_id: str, predicate: Optional[str],
                      bounds_lo: Optional[str], bounds_hi: Optional[str],
                      pk_col: Optional[str]) -> "Chunk":
        """Rebuild a chunk from its RECORDED manifest row for a resume.

        The stored ``predicate`` is replayed verbatim as the source WHERE; the
        integer bounds + the current table's ``pk_col`` drive the target-side
        idempotent range DELETE, which must cover the SAME range that was read. A
        recorded whole-table chunk stored a NULL predicate/bounds (``pk_col`` is
        then unused). Bounds are parsed back to ``int`` so ``target_where()``
        renders identically to the value first recorded (``str(int)`` round-trips).
        """
        if predicate is None:
            return cls(table, chunk_id)
        lo = None if bounds_lo is None else int(bounds_lo)
        hi = None if bounds_hi is None else int(bounds_hi)
        return cls(table, chunk_id, pk_col=pk_col, lo=lo, hi=hi, predicate=predicate)

    def target_where(self, preserve_case: bool = False) -> Optional[str]:
        if self.pk_col is None:
            return None
        # Render through the shared quoter so a reserved/mixed-case PK column
        # (e.g. "order", "User") in the idempotent range DELETE matches the name
        # the DDL/loader created, instead of a bare token that errors or folds.
        c = render_ident(self.pk_col, preserve_case)
        return "{c} >= {lo} AND {c} < {hi}".format(c=c, lo=self.lo, hi=self.hi)


def compute_chunks(source, table: Table, target_chunks: int = 4) -> List[Chunk]:
    """Return the chunk list for *table*, aiming for ~*target_chunks* pieces."""
    pk = table.primary_key
    if pk and len(pk.columns) == 1 and target_chunks > 1:
        col = pk.columns[0]
        bounds = source.numeric_pk_bounds(table, col)
        if bounds is not None:
            lo, hi = bounds
            span = hi - lo + 1
            n = max(1, min(target_chunks, span))
            step = (span + n - 1) // n  # ceil
            chunks: List[Chunk] = []
            start = lo
            ordinal = 0
            while start <= hi:
                end = min(start + step, hi + 1)  # exclusive; last covers hi
                chunks.append(Chunk(table, "{}:{}".format(table.name, ordinal), col, start, end))
                start = end
                ordinal += 1
            return chunks
    return [Chunk(table, "{}:0".format(table.name))]
