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

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..chunking.pk_range import Chunk, compute_chunks
from .catalog_model import Table
from . import manifest as M
from .identifiers import fold as _ident
from .manifest import Manifest


class ResumeDriftError(RuntimeError):
    """Raised when a resume can't safely replay the recorded plan — e.g. a table
    the ledger recorded has vanished from the source, or a recorded range chunk's
    table no longer exposes the single-column PK its DELETE range needs. Fail
    closed: silently skipping the affected ranges would leave the target short."""


def _single_pk_col(table: Table) -> Optional[str]:
    """The lone PK column used to render a range chunk's target DELETE, or None
    when the table has no single-column PK (whole-table chunk). Mirrors the PK
    test in ``compute_chunks`` so replay resolves the same column the plan used."""
    pk = table.primary_key
    if pk and len(pk.columns) == 1:
        return pk.columns[0]
    return None


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
                 use_copy=True, preserve_case=False, fresh=False,
                 concurrent_writes=True):
        self.cfg = cfg
        self.schema = schema
        self.manifest_path = manifest_path
        # Resumable-load ledger backend ("sqlite" default, or embedded "nano").
        self.manifest_backend = getattr(
            getattr(cfg, "options", None), "manifest_backend", "sqlite")
        self.run_id = run_id
        self.parallelism = max(1, int(parallelism))
        self.use_copy = use_copy
        self.preserve_case = preserve_case
        # Whether the target services concurrent write transactions. The Apache
        # editions (Nano/Lite) block — not error — on a second concurrent writer,
        # so a parallel load there would hang forever (the serial-retry mop-up
        # would never be reached). When False the load is serial from the start.
        self.concurrent_writes = bool(concurrent_writes)
        # fresh=True (a drop_existing migrate) clears prior chunk state so the
        # recreated tables are reloaded instead of skipped as already-LOADED.
        self.fresh = fresh
        self._chunks: Dict[str, Chunk] = {}
        self._table_by_fqn: Dict[str, Table] = {}

    # --- planning (deterministic; safe to call again on resume) ----------
    def plan(self) -> None:
        from ..config.store import build_source_adapter

        # Reset in-memory plan state on entry AND on failure (the except below)
        # so a caller that catches a plan() failure and retries — or calls
        # run(), which plans only when _chunks is empty — can never execute the
        # partial in-memory plan a failed attempt left behind. The ledger-side
        # twin of this guard is the plan-complete marker.
        self._chunks = {}
        self._table_by_fqn = {}
        probe = build_source_adapter(self.cfg)
        probe.connect()
        man = Manifest(self.manifest_path, backend=self.manifest_backend)
        try:
            cfg_hash = self._config_hash()
            prior_hash, _ = man.get_run(self.run_id)
            # Drift guard: a reused run_id whose plan-affecting config changed must
            # not trust prior LOADED chunks (their predicates/bounds are stale), so
            # reset rather than silently mix two plans.
            # A partial plan (crash mid-planning: add_chunk commits per row) must
            # never be replayed as complete — its missing tail ranges would be
            # silently skipped. `plan_recorded` says the last planning session
            # (fresh OR a resume's additive new-table planning) finished writing.
            plan_recorded = man.plan_complete(self.run_id)
            do_reset = self.fresh or bool(prior_hash and prior_hash != cfg_hash)
            # A resume is a run we have seen before whose config didn't drift and
            # isn't being force-reset. On resume we REPLAY the recorded chunk plan
            # instead of recomputing it from the (possibly drifted) live source
            # bounds — recomputing is what silently skipped/overlapped PK ranges.
            # With `plan_recorded` False the replay runs in SALVAGE mode: tables
            # with load progress are provably fully-planned (loading starts only
            # after planning finishes, and chunks are never added to an existing
            # table), so they replay; progress-less tables might be the partial
            # one, so their rows are dropped and re-planned from the live source.
            is_resume = bool(prior_hash) and not do_reset
            man.start_run(self.run_id, config_hash=cfg_hash)
            if do_reset:
                man.reset_run(self.run_id)
            # Invalidate BEFORE any planning write — including a resume's additive
            # new-table planning — so a crash anywhere mid-write leaves the flag
            # unset and the next run salvages instead of replaying a partial plan.
            man.mark_plan_complete(self.run_id, complete=False)
            # ONE read-consistency snapshot for the whole (possibly resumed) load:
            # capture it on a fresh plan and REUSE it on resume, so completed and
            # pending chunks read the same point-in-time view of the source (Oracle
            # AS OF SCN). None => no snapshot available; quiesce the source.
            # Capture the snapshot on a fresh plan or a not-yet-decided run; on a
            # resume REUSE the recorded decision. A stored empty value means "no
            # snapshot available — keep current-read mode" and must NOT silently
            # re-capture a new (inconsistent) SCN on a later chunk.
            if do_reset or not man.snapshot_decided(self.run_id):
                snapshot = probe.capture_snapshot()
                man.set_snapshot(self.run_id, snapshot)
            else:
                snapshot = man.get_snapshot(self.run_id)
            probe.use_snapshot(snapshot)
            if is_resume:
                self._replay_plan(man, probe, trust_all=plan_recorded)
            else:
                self._plan_fresh(man, probe, self.schema.tables)
            # Every table/chunk row is durably recorded (incl. tables added on
            # resume) — only now is the plan replay-safe.
            man.mark_plan_complete(self.run_id)
        except BaseException:
            # A failed plan must leave NO partial in-memory plan behind: run()
            # plans only when _chunks is empty, so surviving partial entries
            # would let an exception-swallowing caller load a partial plan and
            # mint "progress" on a partially-planned table — which the salvage
            # invariant would then trust on the next real restart.
            self._chunks = {}
            self._table_by_fqn = {}
            raise
        finally:
            man.close()
            probe.close()

    def _plan_fresh(self, man: Manifest, probe, tables: List[Table]) -> None:
        """Compute and record a chunk plan for *tables* from the CURRENT source.

        Used on a fresh migrate/reset for every table, and on resume ONLY for
        tables that appear in the source but were never recorded for this run
        (additive). Never re-plans a table the ledger already knows — that path
        replays the recorded rows instead (see ``_replay_plan``)."""
        for t in tables:
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

    def _replay_plan(self, man: Manifest, probe, trust_all: bool = True) -> None:
        """Rebuild the in-memory plan from the RECORDED manifest rows.

        The ledger — not the live source — is the single source of truth for a
        run's plan, so a resume executes byte-identical predicates against the
        loaded-state bookkeeping they were recorded with. New source tables are
        planned fresh (additive); a table the ledger recorded but that has since
        vanished from the source fails closed (a table disappearing mid-run is
        operator error, and silently dropping its ranges would leave the target
        short of rows that once existed).

        ``trust_all=False`` is SALVAGE mode (the plan-complete marker was unset:
        some planning session crashed mid-write): recorded tables WITH load
        progress are provably fully-planned and replay as usual; progress-less
        recorded tables might be the partial one, so their rows are dropped and
        they are re-planned from the live source (nothing loaded ⇒ nothing lost)."""
        recorded = man.get_chunks(self.run_id)
        recorded_by_table: Dict[str, List] = defaultdict(list)
        for rc in recorded:
            recorded_by_table[rc.table_fqn].append(rc)
        source_by_fqn = {t.fqn: t for t in self.schema.tables}

        vanished = sorted(set(recorded_by_table) - set(source_by_fqn))
        if vanished:
            raise ResumeDriftError(
                "resume aborted: {} table(s) recorded in the manifest are no longer "
                "in the source ({}). A table vanishing mid-run is operator error; "
                "fix the source (or start a fresh run) before resuming — refusing "
                "to silently drop its unloaded rows.".format(len(vanished), ", ".join(vanished)))

        salvaged: List[str] = []
        for fqn, rows in recorded_by_table.items():
            table = source_by_fqn[fqn]
            if not trust_all and not man.table_has_progress(self.run_id, fqn):
                # Salvage: this progress-less table might be the one a crashed
                # planning session left partial — drop its rows and re-plan it
                # from the live source below (nothing loaded ⇒ nothing lost).
                man.delete_table_chunks(self.run_id, fqn)
                salvaged.append(fqn)
                continue
            self._table_by_fqn[fqn] = table
            pk_col = _single_pk_col(table)
            for rc in rows:
                # A recorded RANGE chunk (non-null predicate) needs a single-column
                # PK on the current table so its idempotent target DELETE covers the
                # same range it reads; without one the DELETE would be whole-table
                # and wipe sibling chunks. Fail closed rather than lose data.
                if rc.predicate is not None and pk_col is None:
                    raise ResumeDriftError(
                        "resume aborted: table {} was chunked by a single-column PK "
                        "but no longer exposes one (chunk {}); its recorded ranges "
                        "can't be replayed safely. Start a fresh run.".format(fqn, rc.chunk_id))
                ch = Chunk.from_recorded(table, rc.chunk_id, rc.predicate,
                                         rc.bounds_lo, rc.bounds_hi, pk_col)
                # Guard against a CHANGED (not just vanished) single-column PK:
                # the recorded predicate is replayed verbatim for the source READ,
                # but the idempotent target DELETE renders from the CURRENT pk_col
                # + bounds. If re-rendering the recorded bounds with the current
                # column doesn't reproduce the recorded predicate byte-for-byte,
                # the PK (or the renderer) changed mid-run and read/delete would
                # cover DIFFERENT ranges — deleting sibling rows it never reloads.
                if (rc.predicate is not None and pk_col is not None
                        and ch.lo is not None and ch.hi is not None):
                    rerendered = Chunk(table=table, chunk_id=rc.chunk_id,
                                       pk_col=pk_col, lo=ch.lo, hi=ch.hi).source_where()
                    if rerendered != rc.predicate:
                        raise ResumeDriftError(
                            "resume aborted: chunk {} of {} was recorded with "
                            "predicate [{}] but the current table's PK column "
                            "renders it as [{}] — the primary key changed mid-run, "
                            "so the replayed read and the idempotent target DELETE "
                            "would cover different ranges. Start a fresh run."
                            .format(rc.chunk_id, fqn, rc.predicate, rerendered))
                self._chunks[ch.chunk_id] = ch

        # Plan fresh: tables that appeared in the source since the run was
        # planned (additive), plus any progress-less tables salvaged above.
        replan = set(salvaged)
        new_tables = [t for t in self.schema.tables
                      if t.fqn not in recorded_by_table or t.fqn in replan]
        if new_tables:
            self._plan_fresh(man, probe, new_tables)

    def _config_hash(self) -> str:
        """Stable hash of the plan-affecting config (source/target identity +
        schema + parallelism + preserve_case; passwords excluded). Drives the
        drift guard in plan() so a reused run_id can't trust stale LOADED chunks
        after the config that produced them changed."""
        import hashlib
        s, t, o = self.cfg.source, self.cfg.target, self.cfg.options
        src_db = (getattr(s, "database", None) or getattr(s, "service_name", None)
                  or getattr(s, "sid", None) or "")
        key = "|".join(str(x) for x in (
            getattr(s, "dialect", ""), s.host, s.port, src_db, s.schema or "", s.user,
            getattr(t, "driver", ""), t.host, t.port, t.dbname, getattr(t, "user", ""),
            bool(o.preserve_case), int(self.parallelism),
        ))
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    # --- execution -------------------------------------------------------
    def run(self) -> LoadStats:
        if not self._chunks:
            self.plan()
        self._errors: Dict[str, str] = {}
        self._notes: List[str] = []

        # Targets that don't service concurrent write transactions (Nano/Lite, the
        # Apache editions) *block* on a second concurrent writer instead of erroring,
        # which would hang the parallel pass indefinitely. Load them serially from
        # the start. Stock PostgreSQL and HeliosDB-Full run the parallel pass, then
        # a serial mop-up retries any chunk that failed only under contention
        # (chunks are idempotent — DELETE-range-then-load in one txn — so a retry
        # never duplicates).
        if self.parallelism > 1 and not self.concurrent_writes:
            self._notes.append(
                "target does not support concurrent write transactions; loaded "
                "serially (parallelism={} not applied)".format(self.parallelism))
        self._load_pending(parallel=self.concurrent_writes)
        if self.parallelism > 1 and self.concurrent_writes:
            self._load_pending(parallel=False)

        stats = LoadStats()
        stats.warnings = list(self._notes)
        man = Manifest(self.manifest_path, backend=self.manifest_backend)
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
        man = Manifest(self.manifest_path, backend=self.manifest_backend)
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

    def _safe_load(self, chunk_row) -> None:
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
        man = Manifest(self.manifest_path, backend=self.manifest_backend)
        try:
            src.connect()
            # Read this chunk at the run's pinned snapshot (Oracle AS OF SCN), so
            # a row updated/deleted on the source after an earlier chunk committed
            # can't make this chunk inconsistent with the rest of the load.
            src.use_snapshot(man.get_snapshot(self.run_id))
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
