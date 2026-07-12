"""End-to-end migration orchestrator (sequential reference path).

Drives: introspect source → emit + apply DDL → stream + load data (COPY fast
path when the capability probe allows it, else INSERT) → add foreign keys last
→ create views. Parallelism, chunking, and resume layer on top of this in the
data engine (manifest-driven); this sequential path is the correctness baseline
and is what the smoke/integration tests exercise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional

from ..constants import Edition
from ..core.catalog_model import DataTypeKind
from ..emit import mysql_ddl, oracle_ddl
from ..emit.ddl import (
    render_create_table,
    render_drop_foreign_keys,
    render_foreign_keys,
    render_index,
    render_sequence,
    render_view,
)
from ..plsql.rewrite import rewrite_sql
from ..sources.base import SourceAdapter
from ..target.base import TargetDriver
from ..target.mysql_driver import mysql_ident
from ..target.psycopg_driver import quote_table
from ..typemap.registry import TypeRegistry


def _quote_table_for(dialect: str, name: str) -> str:
    """Quote a (possibly schema-qualified) table name in the target's dialect."""
    if dialect == "oracle":
        return ".".join(oracle_ddl.oracle_ident(p) for p in name.split("."))
    if dialect == "mysql":
        return ".".join(mysql_ident(p) for p in name.split("."))
    return quote_table(name)  # postgres / HeliosDB PG-wire


def _dedup_index_name(name: str, table_name: str, seen: set) -> str:
    """Return a schema-unique index name. Oracle/MySQL allow the same index name
    on different tables, but PostgreSQL-family targets require it unique within
    the schema; on a collision, prefix with the table name, then a counter."""
    if name not in seen:
        return name
    cand = "{}_{}".format(table_name, name)
    n = 2
    while cand in seen:
        cand = "{}_{}_{}".format(table_name, name, n)
        n += 1
    return cand


def _translate_bool_comparisons(sql: str, bool_cols) -> str:
    """Rewrite ``<boolcol> = 1`` / ``= 0`` (and ``'1'``/``'0'``) to ``= true`` /
    ``= false`` for the named BOOLEAN columns.

    A MySQL view over a ``TINYINT(1)`` (mapped to BOOLEAN) compares it to an int,
    e.g. ``WHERE employees.active = 1``; strict PostgreSQL rejects ``boolean =
    integer``. Only the schema's actual boolean columns are touched, optionally
    table-qualified, so a same-named non-boolean column elsewhere is left alone.
    """
    out = sql
    for c in bool_cols:
        ref = r"((?:\b\w+\.)?\b" + re.escape(c) + r"\b)\s*=\s*'?{}'?(?!\d)"
        out = re.sub(ref.format("1"), r"\1 = true", out)
        out = re.sub(ref.format("0"), r"\1 = false", out)
    return out


def _portable_view(view, dialect, capabilities, bool_cols=()):
    """Make a source view's body portable for the target; return (view, notes).

    On the PG-wire path (``dialect`` is neither oracle nor mysql) the source-
    dialect body is translated toward PostgreSQL via the dialect rewriter
    (seq.NEXTVAL->nextval, SYS_GUID, FROM DUAL strip, ROWNUM->LIMIT, NVL/DECODE/
    SYSDATE, with (+)-outer-join detection) and boolean comparisons against the
    schema's BOOLEAN columns are normalized, so an Oracle/MySQL view doesn't reach
    the target as raw source SQL. The native-Oracle and MySQL targets keep the
    source body verbatim (their own surface accepts it).
    """
    if dialect in ("oracle", "mysql"):
        return view, []
    body, _passes, gaps = rewrite_sql(view.definition, capabilities)
    body = _translate_bool_comparisons(body, bool_cols)
    notes = [g.recommendation or g.feature for g in gaps]
    if body != view.definition:
        view = replace(view, definition=body)
    return view, notes


def _order_views(views):
    """Order views so each is emitted AFTER every other view it references.

    Both PostgreSQL and HeliosDB require a view's referents to exist at CREATE
    time, so a view that selects from another view must be created after it.
    Dependencies are detected by a whole-word, case-insensitive occurrence of one
    view's name in another's definition (a schema-qualified ``schema.v`` reference
    still contains the bare name as a word); table references are ignored because
    only view names are in the graph, and a view's reference to its own name (a
    self/recursive view) is not a dependency. Views in a reference cycle — which
    no engine accepts anyway — keep their original relative order. Stable:
    independent views stay in source order.
    """
    n = len(views)
    if n < 2:
        return list(views)
    lname = [v.name.lower() for v in views]
    bodies = [(v.definition or "") for v in views]
    patterns = [re.compile(r"\b" + re.escape(nm) + r"\b", re.IGNORECASE) for nm in lname]
    # deps[i] = indices of the OTHER views that view i references
    deps = []
    for i in range(n):
        d = set()
        for j in range(n):
            if j == i or lname[j] == lname[i]:
                continue
            if patterns[j].search(bodies[i]):
                d.add(j)
        deps.append(d)
    # Kahn-style passes in original order (stable). Emit any view whose deps are
    # all already emitted; repeat until no progress.
    emitted: set = set()
    resolved: List[int] = []
    remaining = list(range(n))
    progress = True
    while remaining and progress:
        progress = False
        still = []
        for i in remaining:
            if deps[i] <= emitted:
                resolved.append(i)
                emitted.add(i)
                progress = True
            else:
                still.append(i)
        remaining = still
    resolved.extend(remaining)  # any cycle: fall back to original relative order
    return [views[i] for i in resolved]


@dataclass
class MigrateStats:
    tables: int = 0
    rows: Dict[str, int] = field(default_factory=dict)
    load_mode: str = ""
    warnings: List[str] = field(default_factory=list)
    failed_chunks: int = 0  # >0 means the data load is INCOMPLETE (not a clean migrate)

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())


def migrate(
    source: SourceAdapter,
    target: TargetDriver,
    schema: Optional[str] = None,
    registry: Optional[TypeRegistry] = None,
    drop_existing: bool = True,
    preserve_case: bool = False,
    batch_size: int = 1000,
    prefer_copy: bool = True,
    cfg=None,                # ProjectConfig: enables the resumable/parallel loader
    manifest_path: Optional[str] = None,
    run_id: Optional[str] = None,
    parallelism: int = 1,
    chunks_per_worker: int = 2,
    do_schema: bool = True,  # False on resume: skip DDL, continue the data load
) -> MigrateStats:
    stats = MigrateStats()
    src = source.introspect_schema(schema)
    # Probe only if the caller hasn't already (respects a forced capability set).
    if target.capabilities.edition == Edition.UNKNOWN:
        target.probe_capabilities()
    caps = target.capabilities
    use_copy = bool(prefer_copy and caps.copy_from_stdin)
    stats.load_mode = "copy" if use_copy else "insert"
    # Dialect dispatch: each target driver advertises its SQL dialect via the
    # ``dialect`` attribute. ``oracle``/``mysql`` are heterogeneous targets that
    # want their own DDL spelling and quoting; the default (``postgres``, the
    # psycopg/HeliosDB PG-wire path) is unaffected.
    #   * oracle: source-case identifiers (HeliosDB does the translation),
    #     Oracle-dialect DDL, inline INSERT load (no COPY, no chunked loader yet).
    #   * mysql:  lowercased identifiers, MySQL-dialect DDL, INSERT load (no COPY);
    #     the chunked/resumable loader works since it is driver-agnostic.
    #   * postgres: lowercased identifiers, PG-dialect DDL, COPY fast path.
    dialect = getattr(target, "dialect", "postgres")
    is_oracle = dialect == "oracle"
    is_mysql = dialect == "mysql"
    # Oracle preserves the source (upper) case; PG and MySQL lowercase by default.
    keep_source_case = preserve_case or is_oracle

    def tname(name: str) -> str:
        return name if keep_source_case else name.lower()

    # Per-dialect DDL emitters (closures so the per-statement loops stay readable).
    if is_oracle:
        def render_table(t):
            return oracle_ddl.render_create_table_oracle(t)

        def render_seq(seq):
            return oracle_ddl.render_sequence_oracle(seq)

        def render_idx(t, idx):
            return oracle_ddl.render_index_oracle(t, idx)

        def render_fks(t):
            return oracle_ddl.render_foreign_keys_oracle(t)

        def render_drop_fks(t):
            return []  # native Oracle path doesn't run the chunked loader
    elif is_mysql:
        def render_table(t):
            return mysql_ddl.render_create_table(t, registry, preserve_case)

        def render_seq(seq):
            return None  # MySQL has no CREATE SEQUENCE (use AUTO_INCREMENT)

        def render_idx(t, idx):
            return mysql_ddl.render_index(t, idx, preserve_case)

        def render_fks(t):
            return mysql_ddl.render_foreign_keys(t, preserve_case)

        def render_drop_fks(t):
            return mysql_ddl.render_drop_foreign_keys(t, preserve_case)
    else:
        def render_table(t):
            return render_create_table(t, registry, preserve_case)

        def render_seq(seq):
            return render_sequence(seq, preserve_case)

        def render_idx(t, idx):
            return render_index(t, idx, preserve_case)

        def render_fks(t):
            return render_foreign_keys(t, preserve_case)

        def render_drop_fks(t):
            return render_drop_foreign_keys(t, preserve_case)

    def qtable(name: str) -> str:
        return _quote_table_for(dialect, name)

    # --- schema: tables, sequences, indexes ---
    # Drop in reverse (child-before-parent) order so foreign-key dependencies
    # don't block a re-run; create in forward order (FKs are added after data).
    if do_schema:
        if drop_existing:
            for t in reversed(src.tables):
                # MySQL has no DROP TABLE ... CASCADE; FK children are dropped in
                # child-before-parent order already, so CASCADE isn't needed.
                cascade = "" if is_mysql else " CASCADE"
                target.execute("DROP TABLE IF EXISTS {}{}".format(qtable(tname(t.name)), cascade))
            if not is_mysql:
                # Drop sequences too, so a re-run recreates them cleanly instead of
                # warning "already exists" (CREATE SEQUENCE is not IF-NOT-EXISTS).
                # Best-effort: a target that rejects the first DROP SEQUENCE doesn't
                # implement sequences, so stop rather than spam it with the rest.
                for seq in src.sequences:
                    try:
                        target.execute(
                            "DROP SEQUENCE IF EXISTS {}".format(qtable(tname(seq.name))))
                    except Exception:  # noqa: BLE001
                        break
        # Sequences BEFORE tables: a PG-source column may carry a
        # ``DEFAULT nextval('seq')`` that the target resolves at CREATE TABLE
        # time, so the sequence must already exist. (Plain CREATE SEQUENCE has no
        # table dependency, so this order is safe for every dialect.)
        for seq in src.sequences:
            stmt = render_seq(seq)
            if not stmt:
                continue
            try:
                target.execute(stmt)
            except Exception as e:  # noqa: BLE001
                stats.warnings.append("sequence {}: {}".format(seq.name, e))
        for t in src.tables:
            target.execute(render_table(t))
            stats.tables += 1
        seen_idx: set = set()
        for t in src.tables:
            for idx in t.indexes:
                stmt = render_idx(t, idx)
                if not stmt:
                    continue
                # PostgreSQL-family targets require schema-unique index names, but
                # Oracle/MySQL allow the same index name on different tables. Dedup
                # on collision (prefix with the table, then a counter) so the second
                # index isn't rejected/skipped instead of being created.
                base = tname(idx.name)
                name = _dedup_index_name(base, tname(t.name), seen_idx)
                if name != base:
                    stmt = render_idx(t, replace(idx, name=name))
                seen_idx.add(name)
                try:
                    target.execute(stmt)
                except Exception as e:  # noqa: BLE001
                    stats.warnings.append("index {}: {}".format(name, e))
    else:
        stats.tables = len(src.tables)

    # --- data ---
    # The chunked/resumable loader drives any non-Oracle target (its driver calls
    # are dialect-agnostic). The native Oracle path uses the inline INSERT load
    # below (resumable native is future work).
    if cfg is not None and manifest_path and run_id and not is_oracle:
        # Resumable, parallel, manifest-tracked load (chunked by PK range).
        from .loader import ResumableLoader

        # Drop FKs first so per-chunk range-deletes (idempotent reload) can't trip
        # enforced foreign keys; they are re-created after the data load below. On
        # a fresh migrate the tables have no FKs yet, so this is a no-op there.
        for t in src.tables:
            for stmt in render_drop_fks(t):
                try:
                    target.execute(stmt)
                except Exception:  # noqa: BLE001
                    pass
        loader = ResumableLoader(cfg, src, manifest_path, run_id, parallelism=parallelism,
                                 use_copy=use_copy, preserve_case=preserve_case,
                                 fresh=drop_existing,
                                 concurrent_writes=caps.concurrent_writes,
                                 batch_size=batch_size, chunks_per_worker=chunks_per_worker)
        ls = loader.run()
        by_fqn = {t.fqn: t.name for t in src.tables}
        for fqn, n in ls.rows.items():
            stats.rows[by_fqn.get(fqn, fqn)] = n
        stats.warnings.extend(ls.warnings)
        # Chunks that never loaded after all retries mean the target is INCOMPLETE;
        # surface it so the caller can fail loudly instead of reporting success.
        stats.failed_chunks = max(0, ls.chunks_total - ls.chunks_loaded)
    else:
        for t in src.tables:
            src_cols = [c.name for c in t.columns]
            tgt_cols = [tname(c.name) for c in t.columns]
            rows = source.stream_rows(t, src_cols, arraysize=batch_size)
            if use_copy:
                try:
                    n = target.copy_rows(tname(t.name), tgt_cols, rows)
                except Exception as e:  # noqa: BLE001
                    stats.warnings.append("COPY {} failed ({}); retrying via INSERT".format(t.name, e))
                    # A mid-COPY protocol error can desync the connection; reconnect.
                    try:
                        target.close()
                        target.connect()
                    except Exception:  # noqa: BLE001
                        pass
                    rows = source.stream_rows(t, src_cols, arraysize=batch_size)
                    n = target.insert_rows(tname(t.name), tgt_cols, rows)
            else:
                n = target.insert_rows(tname(t.name), tgt_cols, rows)
            stats.rows[t.name] = n

    # --- foreign keys after data ---
    for t in src.tables:
        for stmt in render_fks(t):
            try:
                target.execute(stmt)
            except Exception as e:  # noqa: BLE001
                stats.warnings.append("fk on {}: {}".format(t.name, e))

    # --- views ---
    bool_cols = {c.name for t in src.tables for c in t.columns
                 if c.data_type.kind is DataTypeKind.BOOLEAN}
    # Emit views in dependency order so a view that selects from another view is
    # created after it (PG and HeliosDB both require the referent to exist).
    for v in _order_views(src.views):
        # Drop-then-create so a re-run/resume re-applies the view cleanly instead
        # of warning that it already exists.
        try:
            cascade = "" if is_mysql else " CASCADE"
            target.execute("DROP VIEW IF EXISTS {}{}".format(qtable(tname(v.name)), cascade))
        except Exception:  # noqa: BLE001
            pass
        try:
            # Oracle path keeps the source-case view name; the definition is the
            # source's own SQL, which HeliosDB's Oracle surface accepts. MySQL/PG
            # use the lowercased name and the (best-effort portable) view body.
            view, notes = _portable_view(v, dialect, target.capabilities, bool_cols)
            for note in notes:
                stats.warnings.append("view {}: {}".format(v.name, note))
            target.execute(render_view(view, keep_source_case))
        except Exception as e:  # noqa: BLE001
            stats.warnings.append("view {}: {}".format(v.name, e))

    return stats
