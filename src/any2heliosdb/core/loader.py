"""Resumable, parallel data loader.

Splits each table into key-range chunks, loads them concurrently, and records
every chunk's state in a durable SQLite manifest. Each chunk is **idempotent**:
before loading it deletes any rows already in its key range, so re-running a
half-finished chunk (after a crash) can never duplicate data. Resume is just
"recover in_progress -> pending, then load the pending chunks".

Threads (not processes) are used: the DB drivers release the GIL on socket I/O,
and each worker opens its own source + target connections and its own manifest
handle (SQLite WAL serializes the small state writes), so nothing is shared
mutably across threads.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List

from ..chunking.pk_range import Chunk, compute_chunks
from . import manifest as M
from .identifiers import fold as _ident
from .manifest import Manifest


@dataclass
class LoadStats:
    rows: Dict[str, int] = field(default_factory=dict)
    chunks_total: int = 0
    chunks_loaded: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())


class ResumableLoader:
    def __init__(self, cfg, schema, manifest_path, run_id, parallelism=4,
                 use_copy=True, preserve_case=False, fresh=False):  # type: ignore[no-untyped-def]
        self.cfg = cfg
        self.schema = schema
        self.manifest_path = manifest_path
        self.run_id = run_id
        self.parallelism = max(1, int(parallelism))
        self.use_copy = use_copy
        self.preserve_case = preserve_case
        # fresh=True (a drop_existing migrate) clears prior chunk state so the
        # recreated tables are reloaded instead of skipped as already-LOADED.
        self.fresh = fresh
        self._chunks: Dict[str, Chunk] = {}
        self._table_by_fqn: Dict[str, object] = {}

    # --- planning (deterministic; safe to call again on resume) ----------
    def plan(self) -> None:
        from ..config.store import build_source_adapter

        probe = build_source_adapter(self.cfg)
        probe.connect()
        man = Manifest(self.manifest_path)
        try:
            man.start_run(self.run_id)
            if self.fresh:
                man.reset_run(self.run_id)
            for t in self.schema.tables:
                self._table_by_fqn[t.fqn] = t
                # Record a source row estimate so the live monitor can show
                # "volume left" and an ETA. One count(*) per table at plan time
                # is negligible next to streaming every row, but a count failure
                # must never abort planning.
                try:
                    rows_est = int(probe.exact_row_count(t))
                except Exception:  # noqa: BLE001
                    rows_est = 0
                man.add_table(self.run_id, t.fqn, t.target_name(self.preserve_case),
                              total_rows_est=rows_est)
                for ch in compute_chunks(probe, t, self.parallelism * 2):
                    self._chunks[ch.chunk_id] = ch
                    man.add_chunk(self.run_id, t.fqn, ch.chunk_id,
                                  predicate=ch.source_where(),
                                  lo=None if ch.lo is None else str(ch.lo),
                                  hi=None if ch.hi is None else str(ch.hi))
        finally:
            man.close()
            probe.close()

    # --- execution -------------------------------------------------------
    def run(self) -> LoadStats:
        if not self._chunks:
            self.plan()
        self._errors: Dict[str, str] = {}

        # First pass honors the requested parallelism. The serial retry pass mops
        # up any chunk that failed only under contention -- some targets (e.g.
        # HeliosDB-Lite today) don't support concurrent transactions, so a chunk
        # can fail in parallel yet succeed serially. Chunks are idempotent
        # (DELETE-range-then-load in one txn), so a retry never duplicates.
        self._load_pending(parallel=True)
        if self.parallelism > 1:
            self._load_pending(parallel=False)

        stats = LoadStats()
        man = Manifest(self.manifest_path)
        try:
            stats.rows = man.rows_by_table(self.run_id)
            counts = man.summary(self.run_id)["chunk_states"]
            stats.chunks_loaded = counts.get(M.LOADED, 0) + counts.get(M.VERIFIED, 0)
            stats.chunks_total = sum(counts.values())
            # Authoritative warnings: only chunks that are STILL not loaded after
            # every pass, annotated with their last error.
            for c in man.pending_chunks(self.run_id):
                stats.warnings.append("chunk {} not loaded: {}".format(
                    c.chunk_id, self._errors.get(c.chunk_id, c.state)))
        finally:
            man.close()
        return stats

    def _load_pending(self, parallel: bool) -> None:
        man = Manifest(self.manifest_path)
        try:
            man.recover(self.run_id)  # in_progress (crash) -> pending
            pending = man.pending_chunks(self.run_id)  # pending + failed
        finally:
            man.close()
        if not pending:
            return
        if parallel and self.parallelism > 1 and len(pending) > 1:
            with ThreadPoolExecutor(max_workers=self.parallelism) as ex:
                list(ex.map(self._safe_load, pending))
        else:
            for c in pending:
                self._safe_load(c)

    def _safe_load(self, chunk_row) -> None:  # type: ignore[no-untyped-def]
        chunk = self._chunks.get(chunk_row.chunk_id)
        if chunk is None:
            return
        try:
            self._load_chunk(chunk_row.table_fqn, chunk)
        except Exception as e:  # noqa: BLE001
            self._errors[chunk_row.chunk_id] = str(e)[:300]

    def _load_chunk(self, table_fqn: str, chunk: Chunk) -> None:
        from ..config.store import build_source_adapter, build_target_driver

        table = self._table_by_fqn[table_fqn]
        src = build_source_adapter(self.cfg)
        tgt = build_target_driver(self.cfg)
        man = Manifest(self.manifest_path)
        try:
            src.connect()
            tgt.connect()
            man.set_chunk_state(self.run_id, table_fqn, chunk.chunk_id, M.IN_PROGRESS, bump_attempt=True)
            tname = table.target_name(self.preserve_case)
            tw = chunk.target_where(self.preserve_case)
            cols = [c.name for c in table.columns]
            tcols = [_ident(c, self.preserve_case) for c in cols]
            rows = src.stream_rows(table, cols, where=chunk.source_where())
            # Atomic per-chunk: DELETE range + load in one transaction (idempotent).
            # No in-chunk COPY->INSERT fallback: a transient COPY failure (e.g. a
            # target that serializes transactions) is retried by the loader's
            # serial-retry pass on a fresh connection, which avoids both the
            # connection desync and the binary-param encoding issues of INSERT.
            n = tgt.load_range(tname, tcols, rows, where=tw, use_copy=self.use_copy)
            man.set_chunk_state(self.run_id, table_fqn, chunk.chunk_id, M.LOADED, rows_loaded=n)
        except Exception as e:  # noqa: BLE001
            man.set_chunk_state(self.run_id, table_fqn, chunk.chunk_id, M.FAILED, error=str(e)[:300])
            raise
        finally:
            man.close()
            src.close()
            tgt.close()
