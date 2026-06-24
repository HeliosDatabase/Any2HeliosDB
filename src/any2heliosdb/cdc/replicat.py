"""Replicat: apply captured change records to the target, idempotently.

Records are bucketed by table and applied through the target driver's upsert
(insert/update) and delete-by-key seams, so re-applying the same trail slice is
a no-op on row state. Tables without a primary key can't be keyed and are
skipped with a warning.

Within a table the trail is an *ordered* stream of explicit I/U/D change
records, so order is load-bearing: ``DELETE id=7`` then ``INSERT id=7`` must
leave the row present. The apply therefore walks each table's records in
arrival order, batching only maximal contiguous runs of the same op class
(upsert vs delete) so the relative delete/upsert order is preserved while still
amortizing the round-trips. (This is distinct from :meth:`reconcile_deletes`,
the snapshot-source key-set diff that has no per-record order to honor.)
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Tuple

from ..core.change_record import DELETE, ChangeRecord


def _runs_by_op_class(recs: List[ChangeRecord]) -> Iterator[Tuple[bool, List[ChangeRecord]]]:
    """Split records into maximal contiguous runs of one op class, in order.

    Yields ``(is_delete, run)``: ``is_delete`` True for a run of DELETEs, False
    for a run of INSERT/UPDATE upserts. Splitting only at the upsert<->delete
    boundary preserves the relative ordering that distinguishes ``D`` then ``I``
    (row ends present) from ``I`` then ``D`` (row ends absent), while letting
    same-class neighbors batch into one driver round-trip.
    """
    run: List[ChangeRecord] = []
    cur_is_delete = False
    for r in recs:
        is_delete = r.op == DELETE
        if run and is_delete != cur_is_delete:
            yield cur_is_delete, run
            run = []
        cur_is_delete = is_delete
        run.append(r)
    if run:
        yield cur_is_delete, run


class Replicat:
    def __init__(self, target, schema_ir, preserve_case: bool = False) -> None:  # type: ignore[no-untyped-def]
        self.target = target
        self.preserve_case = preserve_case
        self._by_name = {t.name.upper(): t for t in schema_ir.tables}

    def _ident(self, name: str) -> str:
        return name if self.preserve_case else name.lower()

    def apply(self, records: List[ChangeRecord]) -> Tuple[int, List[str]]:
        buckets: Dict[str, List[ChangeRecord]] = {}
        for r in records:
            buckets.setdefault(r.table, []).append(r)

        applied = 0
        warnings: List[str] = []
        for tname, recs in buckets.items():
            t = self._by_name.get(tname.upper())
            if t is None or not (t.primary_key and t.primary_key.columns):
                warnings.append("{}: no primary key; skipped {} change(s)".format(tname, len(recs)))
                continue
            target_table = t.target_name(self.preserve_case)
            cols = [c.name for c in t.columns]
            tcols = [self._ident(c) for c in cols]
            key_cols = [self._ident(c) for c in t.primary_key.columns]
            pk = t.primary_key.columns

            # Apply in arrival order, flushing one op class at a time so that an
            # earlier DELETE is never resurrected by a later batched upsert (and
            # vice versa). Contiguous same-class records are coalesced into a
            # single driver call to keep the round-trip count low.
            for is_delete, run in _runs_by_op_class(recs):
                if is_delete:
                    keys = [[r.key.get(c) for c in pk] for r in run]
                    applied += self.target.delete_keys(target_table, key_cols, keys)
                else:
                    rows = [[r.after.get(c) for c in cols] for r in run]
                    applied += self.target.upsert(target_table, key_cols, tcols, rows)
        return applied, warnings

    def reconcile_deletes(self, source_adapter) -> Tuple[int, List[str]]:  # type: ignore[no-untyped-def]
        """Delete target rows whose PK is absent from the source's current keys.

        v1 SCN-watermark capture cannot observe DELETEs (the rows are already
        gone), so the replicat reconciles them with a full key-set diff: keys on
        the target but not in the source are removed. This is a full pass
        (cost O(keys)); incremental delete capture is the log-based roadmap.
        """
        deleted = 0
        warnings: List[str] = []
        for t in self._by_name.values():
            if not (t.primary_key and t.primary_key.columns):
                continue
            pk = t.primary_key.columns
            try:
                src_keys = {tuple(r) for r in source_adapter.stream_rows(t, pk)}
                target_table = t.target_name(self.preserve_case)
                key_idents = [self._ident(c) for c in pk]
                tgt_keys = {tuple(r) for r in self.target.select_keys(target_table, key_idents)}
                extra = [list(k) for k in (tgt_keys - src_keys)]
                if extra:
                    deleted += self.target.delete_keys(target_table, key_idents, extra)
            except Exception as e:  # noqa: BLE001
                warnings.append("delete reconcile {}: {}".format(t.name, e))
        return deleted, warnings
