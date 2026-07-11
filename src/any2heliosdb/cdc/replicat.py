"""Replicat: apply captured change records to the target, idempotently.

Records are bucketed by table and applied through the target driver's upsert
(insert/update) and delete-by-key seams, so re-applying the same trail slice is
a no-op on row state. Tables without a primary key can't be keyed and are
skipped with a warning.

Within a table the trail is an *ordered* stream of explicit I/U/D change
records, so order is load-bearing: ``DELETE id=7`` then ``INSERT id=7`` must
leave the row present. The apply therefore walks each table's records in
arrival order, batching only maximal contiguous runs of the same **op kind** so
the relative ordering is preserved while still amortizing the round-trips. The
four kinds are:

* ``delete`` — an explicit ``D`` record; a run is one batched ``delete_keys``.
* ``full`` — an insert/update carrying the whole after-image; a run is one
  batched ``upsert`` (the fast path, byte-identical to before this split).
* ``partial`` — an update whose after-image *omits* columns (a PostgreSQL
  unchanged-TOAST datum). Applied as a keyed ``update_columns`` of only the
  present non-key columns, so the omitted column keeps its stored value on
  *every* driver — not just the ON-CONFLICT ones. If the row is absent the
  provided columns are inserted (nothing to preserve).
* ``keymove`` — a primary-key-changing update (carries ``before_key``). Applied
  as replay-idempotent, collision-free steps that use only ``update_columns`` /
  ``delete_keys`` / an insert-of-provided (never a merge-upsert assumption): the
  old-key row's non-key columns are refreshed in place (preserving an omitted
  TOAST datum), then — if that row was present — the *new* key is cleared of any
  stale replay leftover and the row is moved by updating its key columns; if the
  old-key row was absent (a replay past the move) the already-moved row is
  refreshed at the new key instead, inserting the provided columns only when the
  row is absent everywhere. This preserves an omitted TOAST column across the
  move on every driver, never deletes the *parent* (old-key) row (FK-safe on
  immediate-checking targets), and re-applying the same slice — once, twice, or
  resumed mid-slice — always converges to the same state with no unique
  violation. See :meth:`Replicat._apply_keymove`.

(This is distinct from :meth:`reconcile_deletes`, the snapshot-source key-set
diff that has no per-record order to honor.)
"""
from __future__ import annotations

from typing import Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from ..core.change_record import DELETE, ChangeRecord
from ..errors import Any2HeliosError


def _classify(r: ChangeRecord, pk: Sequence[str], cols: Sequence[str]) -> str:
    """Bucket a record into one op kind: ``delete``/``full``/``partial``/``keymove``.

    ``keymove`` takes precedence: a record whose ``before_key`` differs from its
    (new) key moved the row and is handled as an in-place key-changing UPDATE,
    regardless of whether its after-image is full or TOAST-partial. Otherwise a
    non-delete record is ``full`` when its after-image covers every table column,
    else ``partial`` (some column omitted, e.g. an unchanged-TOAST datum).
    """
    if r.op == DELETE:
        return "delete"
    if r.before_key:
        old = tuple(r.before_key.get(c) for c in pk)
        new = tuple(r.key.get(c) for c in pk)
        if old != new:
            return "keymove"
    if all(c in r.after for c in cols):
        return "full"
    return "partial"


def _runs_by_kind(
    recs: List[ChangeRecord], pk: Sequence[str], cols: Sequence[str]
) -> Iterator[Tuple[str, List[ChangeRecord]]]:
    """Split records into maximal contiguous runs of one op kind, in order.

    Splitting only at kind boundaries preserves the relative ordering that
    distinguishes ``D`` then ``I`` (row ends present) from ``I`` then ``D`` (row
    ends absent) — and keeps a key-move ahead of a later re-insert of the old
    key — while letting same-kind neighbors batch into one driver round-trip.
    """
    run: List[ChangeRecord] = []
    cur_kind = ""
    for r in recs:
        kind = _classify(r, pk, cols)
        if run and kind != cur_kind:
            yield cur_kind, run
            run = []
        cur_kind = kind
        run.append(r)
    if run:
        yield cur_kind, run


class Replicat:
    def __init__(self, target, schema_ir, preserve_case: bool = False) -> None:
        self.target = target
        self.preserve_case = preserve_case
        self._by_name = {t.name.upper(): t for t in schema_ir.tables}

    def _ident(self, name: str) -> str:
        return name if self.preserve_case else name.lower()

    def _is_keymove(self, r: ChangeRecord) -> bool:
        """Whether *r* is a primary-key-changing UPDATE (a ``keymove``).

        Mirrors :func:`_classify`'s keymove test using the record's own table
        metadata, so the engine can segment a trail slice at keymove boundaries
        without re-bucketing. A record for an unknown or PK-less table is never a
        keymove (it is warned-and-skipped by :meth:`apply`).
        """
        t = self._by_name.get(r.table.upper())
        if t is None or not (t.primary_key and t.primary_key.columns):
            return False
        pk = list(t.primary_key.columns)
        cols = [c.name for c in t.columns]
        return _classify(r, pk, cols) == "keymove"

    def apply_barriered(
        self,
        records: List[ChangeRecord],
        on_flush: Optional[Callable[[int], None]] = None,
        poison_retries: int = 0,
        on_poison: Optional[Callable[[ChangeRecord, str, int], bool]] = None,
    ) -> Tuple[int, List[str]]:
        """Apply *records* with a durability barrier isolating every keymove.

        A keymove is the one op whose replay is NOT target-state-idempotent:
        whether the old-key row is "not yet moved" or "a different logical row
        that later reused the key" is undecidable from target state, so replaying
        a keymove together with a neighbouring record can converge to the wrong
        row (the impostor). This method therefore splits the trail-ordered records
        so that each keymove is applied ALONE, invoking ``on_flush(consumed)``
        after every segment — where ``consumed`` is the cumulative number of
        records durably applied so far. The engine uses ``on_flush`` to persist
        the apply cursor, so a crash can only ever force a keymove to REPLAY by
        itself (proven convergent), never batched with the record before or after
        it. Non-keymove records between keymoves batch into one segment, so a slice
        with no keymove flushes exactly once at the end — today's per-slice
        behaviour, byte-identical.

        **Poison policy (tier-2).** When ``poison_retries > 0`` and ``on_poison``
        is supplied, a non-keymove batch that fails to apply is re-tried
        record-by-record; a single record that fails ``poison_retries`` times is
        passed to ``on_poison(record, reason, offset)`` (which dead-letters it and
        returns whether it was newly recorded) and the apply then advances PAST it,
        so one poison record can never wedge the cursor forever. ``offset`` is the
        record's line index within this call (1-based), letting the caller record
        the failing record's trail cursor. Before parking a record the target is
        ``ping()``ed: if it is unreachable the failure is a transient OUTAGE, not
        poison, so this **raises** (cursor unmoved, next run retries) rather than
        dead-lettering a healthy record on a sick target — a dead target must never
        dead-letter the whole backlog. ``consumed`` still counts a genuinely-parked
        record (the cursor moves past a dead-lettered record). **Keymoves are never
        dead-lettered** — a keymove that fails still raises (fail closed), because
        skipping it would diverge key state. With the defaults (``poison_retries``
        0 / no callback) a failing record raises exactly as before.

        Returns the same ``(applied, warnings)`` pair as :meth:`apply`.
        """
        poison_on = poison_retries > 0 and on_poison is not None
        total_applied = 0
        warnings: List[str] = []
        consumed = 0
        batch: List[ChangeRecord] = []

        def flush_batch() -> None:
            nonlocal total_applied, consumed
            if not batch:
                return
            if poison_on:
                try:
                    a, w = self.apply(batch)
                except Exception:  # noqa: BLE001 - isolate the poison record(s)
                    a, w = self._apply_isolating_poison(
                        list(batch), poison_retries, on_poison,  # type: ignore[arg-type]
                        consumed)
            else:
                a, w = self.apply(batch)
            total_applied += a
            warnings.extend(w)
            consumed += len(batch)
            batch.clear()
            if on_flush is not None:
                on_flush(consumed)

        for r in records:
            if self._is_keymove(r):
                flush_batch()  # persist the cursor up to (but not incl.) the keymove
                a, w = self.apply([r])  # keymove: fail closed on error (never dead-letter)
                total_applied += a
                warnings.extend(w)
                consumed += 1
                if on_flush is not None:
                    on_flush(consumed)  # persist past the keymove — it replayed alone
            else:
                batch.append(r)
        flush_batch()
        return total_applied, warnings

    def _apply_isolating_poison(
        self,
        batch: List[ChangeRecord],
        poison_retries: int,
        on_poison: Callable[[ChangeRecord, str, int], bool],
        base_consumed: int = 0,
    ) -> Tuple[int, List[str]]:
        """Re-apply a failed non-keymove batch one record at a time, dead-lettering
        any record that fails ``poison_retries`` times so the cursor can advance.

        The batch never contains a keymove (the barrier isolates those), so every
        record here is safe to skip after exhausting retries. Order within the
        batch is preserved (records applied in arrival order), matching the batched
        path's semantics. ``base_consumed`` is how many records this chunk consumed
        before the batch, so record *j* reports offset ``base_consumed + j + 1``.

        A record that exhausts its retries is only dead-lettered once the target
        answers a ``ping()`` — if the ping fails the whole batch was failing because
        the TARGET is down (a transient outage), not because the record is poison,
        so this raises (cursor unmoved) rather than parking a healthy record. That is
        what stops a dead target from dead-lettering the entire backlog.
        """
        applied = 0
        warnings: List[str] = []
        attempts = max(1, poison_retries)
        for j, r in enumerate(batch):
            ok: Optional[int] = None
            last_err: Optional[BaseException] = None
            for _ in range(attempts):
                try:
                    a, w = self.apply([r])
                    ok = a
                    warnings.extend(w)
                    break
                except Exception as e:  # noqa: BLE001
                    last_err = e
            if ok is not None:
                applied += ok
                continue
            # The record exhausted its retries. Distinguish poison from a sick
            # target: ping before parking. A failing ping means the target is
            # unreachable, so re-raise (cursor stays; the next run retries the
            # whole chunk) instead of dead-lettering a record a healthy target
            # would accept.
            try:
                self.target.ping()
            except Exception as pe:  # noqa: BLE001
                raise Any2HeliosError(
                    "CDC replicat: target became unreachable while isolating a failed "
                    "record ({}.{} key={}); treating this as a transient outage, not "
                    "poison — leaving the apply cursor unmoved so the next run retries "
                    "rather than dead-lettering the backlog against a dead target. "
                    "Underlying apply error: {}".format(
                        r.schema, r.table, r.key, last_err)) from pe
            reason = "{}: {}".format(type(last_err).__name__, last_err)
            newly = on_poison(r, reason, base_consumed + j + 1)
            if newly:
                warnings.append(
                    "dead-lettered {}.{} key={} after {} attempt(s): {}".format(
                        r.schema, r.table, r.key, attempts, last_err))
            else:
                warnings.append(
                    "skipped already-dead-lettered {}.{} key={}".format(
                        r.schema, r.table, r.key))
        return applied, warnings

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
            key_cols = [self._ident(c) for c in t.primary_key.columns]
            pk = list(t.primary_key.columns)

            # Apply in arrival order, one op kind at a time so an earlier DELETE is
            # never resurrected by a later batched upsert (and vice versa) and a
            # key-move stays ahead of a re-insert of the old key. Contiguous
            # same-kind records coalesce into a single driver call where possible.
            for kind, run in _runs_by_kind(recs, pk, cols):
                if kind == "delete":
                    keys = [[r.key.get(c) for c in pk] for r in run]
                    applied += self.target.delete_keys(target_table, key_cols, keys)
                elif kind == "full":
                    # Full after-images batch into one upsert (the fast path).
                    full_idents = [self._ident(c) for c in cols]
                    rows = [[r.after.get(c) for c in cols] for r in run]
                    applied += self.target.upsert(target_table, key_cols, full_idents, rows)
                elif kind == "partial":
                    applied += self._apply_partial(target_table, key_cols, pk, cols, run)
                else:  # keymove
                    applied += self._apply_keymove(target_table, key_cols, pk, cols, run)
        return applied, warnings

    def _apply_partial(
        self,
        target_table: str,
        key_cols: List[str],
        pk: Sequence[str],
        cols: Sequence[str],
        run: List[ChangeRecord],
    ) -> int:
        """Apply TOAST-partial UPDATEs as keyed column-subset UPDATEs.

        Each record's after-image omits some columns (an unchanged-TOAST datum),
        so a full-row upsert would NULL them on the DELETE+INSERT drivers. Instead
        update only the present non-key columns WHERE the key matches; if the row
        is absent (e.g. a replay that starts mid-stream), insert the columns we
        have — there is no prior value to preserve. Per-record so each row's
        matched/absent decision is independent (partial images are rare).
        """
        pk_set = set(pk)
        applied = 0
        for r in run:
            present = [c for c in cols if c in r.after]
            non_key = [c for c in present if c not in pk_set]
            if not non_key:
                continue  # only key columns present: a no-op UPDATE, nothing to do
            set_idents = [self._ident(c) for c in non_key]
            row = [r.after.get(c) for c in non_key] + [r.key.get(c) for c in pk]
            matched = self.target.update_columns(target_table, key_cols, set_idents, [row])
            if matched:
                applied += matched
            else:
                present_idents = [self._ident(c) for c in present]
                prow = [r.after.get(c) for c in present]
                applied += self.target.upsert(target_table, key_cols, present_idents, [prow])
        return applied

    def _refresh_or_probe(
        self,
        target_table: str,
        key_cols: List[str],
        non_key: Sequence[str],
        r: ChangeRecord,
        key_vals: Sequence[object],
    ) -> int:
        """UPDATE the after-image's non-key columns at *key_vals*; return rows matched.

        ``update_columns`` writes only the named columns, so an omitted (unchanged-
        TOAST) column keeps its stored value on every driver and the matched row is
        never DELETE+INSERT-clobbered. Its return is the rows *matched* — the base
        contract, honoured by all three drivers (the native/psycopg UPDATE counts a
        matched row even when values are unchanged; the MySQL driver connects with
        ``CLIENT_FOUND_ROWS`` so a value-unchanged UPDATE also reports the match) —
        which the caller uses as an existence probe. When the record carries *no*
        non-key column (a key-only image, e.g. a table whose only non-key column is
        an omitted TOAST datum), fall back to a matched-count no-op — set the first
        key column to its own value at *key_vals* — which probes existence without
        changing the row or risking a collision.
        """
        if non_key:
            set_idents = [self._ident(c) for c in non_key]
            row = [r.after.get(c) for c in non_key] + list(key_vals)
            return self.target.update_columns(target_table, key_cols, set_idents, [row])
        probe_row = [key_vals[0]] + list(key_vals)
        return self.target.update_columns(target_table, key_cols, [key_cols[0]], [probe_row])

    def _apply_keymove(
        self,
        target_table: str,
        key_cols: List[str],
        pk: Sequence[str],
        cols: Sequence[str],
        run: List[ChangeRecord],
    ) -> int:
        """Apply primary-key-changing UPDATEs as replay-idempotent, collision-free steps.

        A keymove carries the row's OLD key (``before_key``) and the new after-image
        (new key + provided columns ``P``, which may OMIT an unchanged-TOAST column).
        We move the row using only ``update_columns`` / ``delete_keys`` / an
        insert-of-provided so that applying the same trail slice once, twice, or
        resumed mid-slice all converge to the identical target state — with no unique
        violation and no lost TOAST datum — on BOTH the merge-upsert drivers
        (psycopg/MySQL) and the DELETE+INSERT driver (native):

          1. Refresh the not-yet-moved OLD-key row's non-key columns of ``P`` in
             place (:meth:`_refresh_or_probe`). This preserves an omitted TOAST
             column and can never collide; its matched count is our existence probe.
          2. If the old-key row was present, clear the NEW key of any stale row left
             by a previous partial/complete replay of THIS move (the source cannot
             hold a live row at the new key at this stream point, so the delete is
             convergent — and it is never the parent/old-key row), then move the row
             by updating its key columns WHERE the old key. With the new-key slot
             cleared first, the move can never raise a unique violation on replay.
          3. Otherwise the row was already moved (a replay past step 2): refresh it
             at the NEW key (again preserving an omitted TOAST datum — an ``UPDATE``
             never deletes). Only if that matches nothing is the row absent
             everywhere, in which case the provided columns are inserted (an omitted
             TOAST value is then genuinely unrecoverable — see docs/cdc.md).
             ``upsert`` is used for that insert purely as an insert into a
             known-empty slot; correctness does not rely on its merge behaviour, and
             using it (rather than a raw INSERT) keeps the step idempotent if the row
             reappears under replay.
        """
        pk_set = set(pk)
        key_idents = [self._ident(c) for c in pk]
        applied = 0
        for r in run:
            present = [c for c in cols if c in r.after]
            non_key = [c for c in present if c not in pk_set]
            old_key_vals = [r.before_key.get(c) for c in pk]
            new_key_vals = [r.key.get(c) for c in pk]
            n_old = self._refresh_or_probe(target_table, key_cols, non_key, r, old_key_vals)
            if n_old >= 1:
                # Old-key row present: clear a stale new-key row, then move it over.
                self.target.delete_keys(target_table, key_cols, [new_key_vals])
                self.target.update_columns(
                    target_table, key_cols, key_idents, [new_key_vals + old_key_vals])
            else:
                # Old-key row absent -> already moved (replay). Refresh at the new
                # key, preserving any omitted column; insert only if absent everywhere.
                n_new = self._refresh_or_probe(target_table, key_cols, non_key, r, new_key_vals)
                if not n_new:
                    present_idents = [self._ident(c) for c in present]
                    prow = [r.after.get(c) for c in present]
                    self.target.upsert(target_table, key_cols, present_idents, [prow])
            applied += 1
        return applied

    def reconcile_deletes(self, source_adapter, chunk_size: int = 0) -> Tuple[int, List[str]]:
        """Delete target rows whose PK is absent from the source's current keys.

        v1 SCN-watermark capture cannot observe DELETEs (the rows are already
        gone), so the replicat reconciles them with a key-set diff: keys on the
        target but not in the source are removed.

        **Bounded (tier-2).** Rather than materialize BOTH full key-sets and their
        difference, this streams the target keys and deletes surplus keys in
        batches of ``chunk_size`` (``0`` = one final batch, the pre-tier-2
        behaviour), so the surplus/delete buffer never grows without bound. *One*
        side — the **source** key-set — is still held in memory to test membership
        (a chunked set-diff; a true sorted-merge would need both sides to emit
        ordered keys, which the drivers do not guarantee). That is the documented
        memory note: peak reconcile memory is O(source keys) per table, not
        O(source + target). Cost is still a full pass; incremental delete capture
        is the log-based roadmap.
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
                surplus: List[List[object]] = []
                for k in self.target.select_keys(target_table, key_idents):
                    tk = tuple(k)
                    if tk not in src_keys:
                        surplus.append(list(tk))
                        if chunk_size and len(surplus) >= chunk_size:
                            deleted += self.target.delete_keys(target_table, key_idents, surplus)
                            surplus = []
                if surplus:
                    deleted += self.target.delete_keys(target_table, key_idents, surplus)
            except Exception as e:  # noqa: BLE001
                warnings.append("delete reconcile {}: {}".format(t.name, e))
        return deleted, warnings
