"""Split a table into key-range chunks for parallel + resumable load.

A chunk is a half-open integer-PK range ``[lo, hi)``. Chunks are deterministic
for a given source state (derived from ``MIN``/``MAX`` of the PK), so a resumed
run regenerates the identical ``chunk_id``s and can skip the ones the manifest
already recorded as loaded. Tables with no single integer PK fall back to one
whole-table chunk.

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

    def source_where(self) -> Optional[str]:
        if self.pk_col is None:
            return None
        # Quote the source PK column, doubling any embedded " (the source keeps its
        # own case, e.g. Oracle UPPER), so a column name containing a quote can't
        # break or alter the chunked read. This predicate is appended verbatim to
        # the source SELECT, so it must be self-safe.
        c = '"{}"'.format(self.pk_col.replace('"', '""'))
        return "{c} >= {lo} AND {c} < {hi}".format(c=c, lo=self.lo, hi=self.hi)

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
