"""CDC engine: wires registry + source capture + trail + replicat apply.

Symmetric Extract -> trail -> Replicat so capture and apply advance on their own
durable cursors. v1 source is Oracle SCN-watermark; the trail and replicat are
source-agnostic, so log-based sources (v2) and HeliosDB-as-source drop in here.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from ..core.change_record import INSERT, ChangeRecord, source_pos_key
from ..errors import Any2HeliosError
from .deadletter import DeadLetter
from .posfile import read_pos, write_pos_atomic
from .registry import CdcRegistry, Extract, _reject_comma_table_names
from .replicat import Replicat
from .sources.oracle_scn import OracleScnSource
from .trail import Trail

# HeliosDB-Nano resolved INSERT ... ON CONFLICT DO UPDATE's quoted SET target in
# v3.58.2 (#34); v3.58.3 accepts E'...' escaped string literals so the replicat's
# bytea ON CONFLICT upsert (psycopg escapes bytea params as E'\\x..') works; and
# v3.58.5 backfills the index auto-created by ADD FOREIGN KEY from existing rows
# (without it, an index-driven lookup/join on an FK column after a load-then-add-
# FK migration silently returned too few rows). Require 3.58.5 so a keyed CDC
# apply — and the snapshot it builds on — are correct.
_NANO_MIN_CDC_VERSION = (3, 58, 5)


def _version_tuple(version: str):
    """First X.Y.Z in a HeliosDB version banner as an int tuple, else None."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version or "")
    return tuple(int(g) for g in m.groups()) if m else None


def _registry_path(cfg) -> str:
    return os.path.join(cfg.options.output_dir, "cdc.db")


def _trail_dir(cfg, name: str) -> str:
    return os.path.join(cfg.options.output_dir, "trail", name)


def _binlog_pos_file(cfg, name: str) -> str:
    return os.path.join(_trail_dir(cfg, name), "binlog.pos")


def _mysql_coord_key(coord: str) -> Optional[Tuple[int, int]]:
    """Total-order key of a ``<file>:<pos>`` binlog coordinate string (pos file /
    ``current_position()`` format), comparable with record ``source_pos`` keys."""
    from .sources.mysql_binlog import binlog_pos_to_int

    log_file, _, log_pos = (coord or "").rpartition(":")
    if not log_file or not log_pos.isdigit():
        return None
    return source_pos_key(binlog_pos_to_int(log_file, int(log_pos)))


def _pg_coord_key(lsn: str) -> Optional[Tuple[int, int]]:
    """Total-order key of a PostgreSQL LSN string, comparable with record keys."""
    from .sources.postgres_logical import lsn_to_int

    return source_pos_key(lsn_to_int(lsn))


def _pg_epoch_file(cfg, name: str) -> str:
    return os.path.join(_trail_dir(cfg, name), "epoch.id")


def _require_matching_epoch_identity(path: str, identity: Optional[str],
                                     name: str) -> None:
    """Fail closed when the trail was written against a DIFFERENT LSN epoch.

    The LSN-order sanity check (:func:`_require_same_epoch`) cannot catch a
    coordinate rewind whose new LSNs happen to straddle the stale tail (a PITR
    restore, or pointing the extract at another cluster with the same trail
    directory): dedup would then silently drop genuinely new changes ordering
    at-or-below the tail. The cluster's ``system_identifier:timeline_id`` is the
    identity of the coordinate space itself, so persist it beside the trail and
    refuse to mix epochs. ``identity=None`` (probe unavailable) skips the check —
    the LSN-order guard still applies.
    """
    if identity is None:
        return
    prev = None
    if os.path.exists(path):
        with open(path, "r") as f:
            prev = f.read().strip() or None
    if prev is None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_pos_atomic(path, identity)
        return
    if prev != identity:
        raise Any2HeliosError(
            "CDC extract {!r}: the trail was written against PostgreSQL coordinate "
            "epoch {} but the source now reports {} — the cluster was restored "
            "(PITR/timeline change) or the extract points at a different cluster. "
            "LSN positions across epochs are not comparable, so refusing to "
            "dedup/append. Archive or remove the trail directory (already-applied "
            "records are safe: apply is idempotent and the apply cursor lives in "
            "the registry), then re-run the extract.".format(name, prev, identity))


def _drop_already_trailed(trail: Trail, records: List[ChangeRecord],
                          since_key: Optional[Tuple[int, int]] = (0, 0)) -> List[ChangeRecord]:
    """Drop captured records already durably in the trail (extract-start dedup).

    A log-based extract can crash between ``trail.append`` and its position write;
    the next run then re-reads the same source events (the server-side cursor —
    binlog pos file / logical slot — was not advanced) and would append them a
    second time. A duplicate line is not harmless for a keymove even with the K1
    barrier (a second keymove line re-opens the impostor), so we drop any captured
    record whose ``source_pos`` orders ``<=`` the last COMPLETE one already in the
    trail. The comparison is over the total order of :func:`source_pos_key`, so a
    prefix crash that trailed only some rows of one multi-row event (all sharing a
    base coordinate) re-appends exactly the never-trailed remainder — a plain-int
    coordinate would have dropped that tail forever. No-op for legacy trails /
    sources without per-event positions (last pos ``None``, or a record whose
    ``source_pos`` is ``None``), keeping their behavior byte-identical.

    ``since_key`` is the durable resume coordinate this capture started from. The
    crash-window overlap can only cover ``(since, tail]``: when the trail tail
    orders BELOW ``since`` there is no window at all — the tail is either from an
    older, already-pos-advanced batch or (after an operator re-anchor) from a
    different binlog coordinate epoch whose encodings are not comparable — and
    dedup must not run, or it would silently discard genuinely new events
    (captured=0 forever). Pass ``None`` for a fresh anchor (no resume coordinate):
    a fresh anchor starts at the source's CURRENT position and can never re-read
    old events, so nothing is dropped (the epoch sanity check for that case lives
    in :func:`_require_same_epoch`). The default ``(0, 0)`` means "the window is
    always valid" (the PG slot resume path, and the pure drop-mechanics tests).
    """
    if since_key is None:
        return records
    last_key = source_pos_key(trail.last_source_pos())
    if last_key is None or last_key < since_key:
        return records
    # A record whose position has no key (absent OR malformed) can't be ordered
    # and is never dropped — dropping on malformed would turn a codec bug into
    # silent data loss.
    kept = []
    for r in records:
        key = source_pos_key(r.source_pos)
        if key is None or key > last_key:
            kept.append(r)
    return kept


def _require_same_epoch(trail: Trail, end_key: Optional[Tuple[int, int]],
                        name: str, hint: str) -> None:
    """Fail closed when the trail tail orders AHEAD of the capture stream's end.

    That ordering is impossible within one coordinate epoch (positions only
    grow), so it means the source's coordinate space restarted underneath a
    surviving trail: MySQL ``RESET MASTER`` / binlog basename change / failover
    to lower-numbered files after the operator re-anchored, or a PostgreSQL
    restore/PITR rewind. Comparing across epochs is meaningless — silently
    continuing would either drop every new event as "already trailed" or let
    stale tail positions poison future dedup — so make the operator resolve the
    epoch break explicitly (archiving the old trail is safe: apply is idempotent
    and the replicat cursor lives in the registry, not the trail).
    """
    if end_key is None:
        return
    tail_key = source_pos_key(trail.last_source_pos())
    # Compare BASE coordinates only: rows of a multi-row event share the event's
    # end coordinate as their base with seq 0..n, so a tail of (P, 2) is NOT
    # ahead of a stream end keyed (P, 0) — that is the normal state right after
    # capturing a multi-row event. Only a strictly greater base is impossible
    # within one epoch.
    if tail_key is not None and tail_key[0] > end_key[0]:
        raise Any2HeliosError(
            "CDC extract {!r}: the trail's last position {} orders AHEAD of the "
            "source's current stream end {} — the source's coordinate space has "
            "restarted (e.g. {}). Refusing to dedup/append across coordinate "
            "epochs. Archive or remove the trail directory to start a fresh epoch "
            "(already-applied records are safe to archive: apply is idempotent and "
            "the apply cursor lives in the registry), then re-run the "
            "extract.".format(name, tail_key, end_key, hint))


def _capture_set(schema_ir, prev: Optional[Extract]):
    """Resolve (capture_tables, new_tables) for this cycle (tier-2 H2).

    The capture set is **pinned** to the registry: a brand-new extract captures
    every current table (nothing is pinned yet); a routine cycle captures only the
    tables already registered, so a table that appeared in the source *after*
    registration is NOT silently absorbed — it is reported as ``new_tables`` and
    warned each cycle. ``--refresh-tables`` is the explicit verb that snapshot-loads
    and then adopts those new tables (this cycle still captures only the pinned set,
    so the snapshot lands before the new tables' first captured events next cycle).
    """
    current_names = [t.name for t in schema_ir.tables]
    if prev is None:
        return list(schema_ir.tables), []
    pinned = set(prev.tables)
    new_tables = sorted(set(current_names) - pinned)
    capture_tables = [t for t in schema_ir.tables if t.name in pinned]
    return capture_tables, new_tables


def _snapshot_start_seq(records: List[ChangeRecord], base: Optional[int],
                        tail_key: Optional[Tuple[int, int]] = None) -> int:
    """Lowest snapshot ``seq`` (at ``base``) that cannot collide with a record this
    cycle already captured (tier-2 M1).

    The cycle's last captured event tags its rows ``[base, 0..k]`` at the SAME
    ``base`` the snapshot uses (the advance coordinate). Starting the snapshot at
    ``seq 0`` would reuse those keys, so a snapshot row could order at-or-below an
    already-captured event row — the trail tail would then stop being monotonic and
    a crash-window re-capture would re-append the event's higher-seq rows past
    dedup (a duplicate row, possibly a duplicate keymove). Start ABOVE the maximum
    ``seq`` any captured record used at this base (a bare-int position counts as
    ``seq 0``); ``-1 + 1 == 0`` when nothing shares the base. ``base is None``
    (a source without per-event positions) has no dedup and starts at 0.

    ``tail_key`` is the DURABLE trail tail's position key (post-append): the
    cycle's records list alone is not enough, because extract-start dedup may
    have dropped every re-captured record (prior crash between append and pos
    write) — the rows already in the trail at this base then still occupy
    ``[base, 0..k]`` even though *records* is empty. The tail is ground truth;
    fold it in so snapshot seqs stay strictly above anything durable."""
    if base is None:
        return 0
    mx = -1
    for r in records:
        key = source_pos_key(r.source_pos)
        if key is not None and key[0] == base:
            mx = max(mx, key[1])
    if tail_key is not None and tail_key[0] == base:
        mx = max(mx, tail_key[1])
    return mx + 1


def _snapshot_tables(adapter, trail: Trail, schema_name: str, schema_ir,
                     table_names: List[str], base: Optional[int],
                     start_seq: int = 0, chunk: int = 10_000) -> Tuple[int, List[str]]:
    """Snapshot-load the current rows of *table_names* into the trail as INSERT
    records (tier-2 H2 ``--refresh-tables``), returning ``(rows, skipped)``.

    Rows are appended in bounded chunks of *chunk* (the ``[cdc] apply_batch``, not a
    hardcode). Each record is tagged with a ``source_pos`` of ``[base, seq]``
    (``base`` = the cycle's advance coordinate) starting at *start_seq* so the trail
    tail keeps a comparable position ABOVE the cycle's captured records and
    extract-start dedup stays enabled and monotonic on the next routine cycle; when
    the source has no per-event coordinate (``base is None``, e.g. Oracle SCN) the
    records carry none, matching that source's other records. PK-less tables cannot
    be keyed and are skipped.
    """
    by_name = {t.name: t for t in schema_ir.tables}
    rows = 0
    skipped: List[str] = []
    seq = start_seq
    buf: List[ChangeRecord] = []
    for tname in table_names:
        t = by_name.get(tname)
        if t is None:
            continue
        if not (t.primary_key and t.primary_key.columns):
            skipped.append(tname)
            continue
        cols = [c.name for c in t.columns]
        for row in adapter.stream_rows(t, cols):
            after = {c: row[i] for i, c in enumerate(cols)}
            key = {pk: after[pk] for pk in t.primary_key.columns}
            pos = None if base is None else [base, seq]
            seq += 1
            buf.append(ChangeRecord(op=INSERT, schema=schema_name, table=t.name,
                                    key=key, after=after, source_pos=pos))
            if len(buf) >= chunk:
                rows += trail.append(buf)
                buf = []
    if buf:
        rows += trail.append(buf)
    return rows, skipped


def run_extract(cfg, name: str, refresh_tables: bool = False) -> Dict[str, object]:
    from ..config.store import build_source_adapter
    from ..constants import SourceDialect

    cap_batch = cfg.cdc.capture_batch
    reg = CdcRegistry(_registry_path(cfg))
    adapter = build_source_adapter(cfg)
    adapter.connect()
    try:
        schema_ir = adapter.introspect_schema(cfg.source.schema)
        schema_name = cfg.source.schema or schema_ir.name
        current_names = [t.name for t in schema_ir.tables]
        # Fail closed on comma-bearing table names AT CYCLE START — before any
        # capture or snapshot work. The registry's tables_csv storage cannot
        # hold them, and discovering that only at the post-snapshot adopt
        # (inside reg.register) would bloat the trail with a full snapshot on
        # every wedged --refresh-tables retry.
        _reject_comma_table_names(current_names)
        prev = reg.get(name)
        # Pin the capture set to the registry; detect tables that appeared later.
        capture_tables, new_tables = _capture_set(schema_ir, prev)
        # Ensure the entry exists and refresh the schema, but adopt the full table
        # set ONLY on the first run (there is nothing pinned yet and no snapshot to
        # land). A ``--refresh-tables`` run does NOT adopt here — adoption is
        # deferred until AFTER its snapshot is durably in the trail (B3), so a
        # failure in between never pins a table with no snapshot.
        reg.register(name, schema_name, current_names, adopt_tables=(prev is None))
        ext = reg.get(name)
        assert ext is not None
        trail = Trail(_trail_dir(cfg, name), rotate_mb=cfg.cdc.trail_rotate_mb)

        if cfg.source.dialect is SourceDialect.MYSQL:
            # Log-based capture: real I/U/D from the binlog. Cursor is the binlog
            # coordinate, persisted in a small pos file alongside the trail.
            from .sources.mysql_binlog import MySqlBinlogSource

            posf = _binlog_pos_file(cfg, name)
            # read_pos returns None only when the file has NEVER existed (fresh
            # extract -> anchor at current). An existing-but-empty/corrupt file
            # raises (fail closed) rather than silently re-anchoring and skipping
            # everything since the last durable cursor.
            since = read_pos(posf)
            mysql_source = MySqlBinlogSource(cfg.source.to_dsn(), schema_name, capture_tables)
            records, new_pos = mysql_source.capture(since or "", limit=cap_batch)
            # An empty anchor means capture could not read a binlog coordinate
            # (SHOW BINARY LOG STATUS / SHOW MASTER STATUS returned nothing): the
            # source has binary logging off, or the user lacks REPLICATION CLIENT.
            # Fail closed — do NOT append or persist. Writing an empty pos file
            # would let this run "succeed", then make every later run abort on the
            # empty/corrupt cursor. (Mirrors the PG `if new_lsn:` guard, but raises
            # because for MySQL the pos file IS the durable cursor.)
            if not new_pos:
                raise Any2HeliosError(
                    "MySQL CDC extract {!r}: could not read a binlog coordinate to anchor "
                    "at. The source has binary logging disabled (log_bin=OFF) or the "
                    "connecting user lacks REPLICATION CLIENT. Enable log_bin with "
                    "binlog_format=ROW and grant REPLICATION CLIENT/SLAVE, then re-run the "
                    "extract. Refusing to persist an empty position (that would make every "
                    "later run abort on a corrupt cursor).".format(name))
            # Self-heal a torn tail (a crashed prior append that persisted a prefix
            # ending mid-line) BEFORE dedup, so dedup keys off the last COMPLETE
            # record rather than the torn fragment. The healed-away events were not
            # covered by the durable pos file, so re-capture + dedup restores them.
            trail.heal_torn_tail()
            # Extract-start dedup: if a previous run crashed after appending but
            # before the pos write, this re-read overlaps the trail's tail. Drop
            # the already-trailed events so no duplicate binlog line is appended.
            # The dedup window only exists when we RESUMED from a durable pos
            # (since); a fresh anchor starts at the current coordinate and never
            # re-reads — but its trail tail must not order ahead of the stream
            # end, or the coordinate space restarted (RESET MASTER / basename
            # change / failover) and dedup math is meaningless: fail closed.
            if since:
                records = _drop_already_trailed(trail, records,
                                                since_key=_mysql_coord_key(since))
            else:
                _require_same_epoch(
                    trail, _mysql_coord_key(new_pos), name,
                    "RESET MASTER / RESET BINARY LOGS, a log_bin basename change, "
                    "or failover to a server with lower binlog numbering")
            captured = trail.append(records)
            # Snapshot-load newly-adopted tables AFTER the captured events, tagged
            # at the advance coordinate (seqs ABOVE the captured records' — M1) so
            # the trail tail stays ordered for dedup; adopt only after the snapshot
            # lands (B3).
            snap_rows, snap_skipped = _snapshot_and_adopt(
                cfg, reg, name, refresh_tables, new_tables, adapter, trail,
                schema_name, schema_ir, current_names, _mysql_coord_base(new_pos),
                records)
            captured += snap_rows
            # Persist the advanced cursor atomically (temp + fsync + os.replace)
            # only AFTER the records are durably in the trail, so a crash re-reads
            # from the old cursor rather than losing the window.
            write_pos_atomic(posf, new_pos)
            return {"captured": captured, "watermark": new_pos,
                    "since": since or "(current)", "skipped": snap_skipped, "mode": "binlog",
                    "new_tables": new_tables, "snapshotted": snap_rows}

        if cfg.source.dialect is SourceDialect.POSTGRESQL:
            # Log-based capture via PostgreSQL logical decoding (test_decoding):
            # real I/U/D, deletes included. Peek -> persist to trail -> advance the
            # slot, so a crash re-reads rather than loses changes. The slot is the
            # durable server-side cursor; we mirror its LSN into the pos file for
            # display + `a2h status`.
            from .sources.postgres_logical import PostgresLogicalSource

            pg_source = PostgresLogicalSource(adapter, schema_name, capture_tables, name)
            # LSNs are only comparable within one cluster lifetime + timeline.
            # Check the coordinate-space identity BEFORE peeking: a PITR restore
            # or a different cluster can emit new LSNs that straddle the stale
            # trail tail, which the LSN-order guard below cannot distinguish from
            # a legitimate crash-window overlap (dedup would silently drop the
            # genuinely new changes).
            _require_matching_epoch_identity(
                _pg_epoch_file(cfg, name), pg_source.epoch_identity(), name)
            records, new_lsn, skipped = pg_source.capture(limit=cap_batch)
            # Self-heal a torn tail before dedup (see the MySQL branch): a crashed
            # prior append leaves an unterminated fragment whose LSN was never
            # slot-advanced, so truncating it and re-peeking restores it once.
            trail.heal_torn_tail()
            # A crash between the append below and the slot advance re-peeks the
            # same changes next run (the slot is non-consuming until advanced), so
            # drop any whose LSN is already at the trail's tail before re-appending.
            # The slot IS the durable resume coordinate (always "resumed"), so the
            # dedup window is always valid — but a trail tail ordering ahead of the
            # peeked stream end means the cluster's LSN space rewound (restore /
            # PITR): fail closed rather than compare across epochs.
            _require_same_epoch(
                trail, _pg_coord_key(new_lsn) if new_lsn else None, name,
                "a restore from backup / PITR rewind, or pointing the extract at a "
                "different cluster with the same trail directory")
            records = _drop_already_trailed(trail, records)
            captured = trail.append(records)
            pg_source.advance(new_lsn)
            # Snapshot-load newly-adopted tables AFTER the captured events (and the
            # slot advance), tagged at the advance LSN with seqs ABOVE the captured
            # records' (M1) so dedup stays ordered; adopt only after the snapshot
            # lands (B3).
            snap_rows, snap_skipped = _snapshot_and_adopt(
                cfg, reg, name, refresh_tables, new_tables, adapter, trail,
                schema_name, schema_ir, current_names, _pg_coord_base(new_lsn),
                records)
            captured += snap_rows
            # The slot is the durable cursor; this file only mirrors the LSN for
            # display/`a2h status`. Still write it atomically so it never shows a
            # torn value.
            posf = _binlog_pos_file(cfg, name)
            if new_lsn:
                write_pos_atomic(posf, new_lsn)
            return {"captured": captured, "watermark": new_lsn or "(slot)",
                    "since": "(slot)", "skipped": skipped + snap_skipped, "mode": "logical",
                    "new_tables": new_tables, "snapshotted": snap_rows}

        # Default: Oracle SCN-watermark capture.
        oracle_source = OracleScnSource(adapter, schema_name, capture_tables)
        records, new_watermark, skipped = oracle_source.capture(ext.watermark)
        captured = trail.append(records)
        # Oracle rows carry no per-event coordinate (dedup is disabled for the
        # idempotent SCN source), so snapshot records carry none either (base=None);
        # adopt only after the snapshot lands (B3).
        snap_rows, snap_skipped = _snapshot_and_adopt(
            cfg, reg, name, refresh_tables, new_tables, adapter, trail,
            schema_name, schema_ir, current_names, None, records)
        captured += snap_rows
        reg.set_watermark(name, new_watermark)
        return {"captured": captured, "watermark": new_watermark,
                "since": ext.watermark, "skipped": skipped + snap_skipped, "mode": "scn",
                "new_tables": new_tables, "snapshotted": snap_rows}
    finally:
        adapter.close()
        reg.close()


def _snapshot_and_adopt(cfg, reg, name: str, refresh_tables: bool, new_tables: List[str],
                        adapter, trail: Trail, schema_name: str, schema_ir,
                        current_names: List[str], base: Optional[int],
                        records: List[ChangeRecord]) -> Tuple[int, List[str]]:
    """Snapshot newly-adopted tables into the trail, THEN adopt them (tier-2 H2/B3).

    Ordering is load-bearing: the snapshot rows are appended durably to the trail
    FIRST and the registry adopts the new tables ONLY after that append succeeds. A
    crash in between leaves the tables still "new" (unadopted), so the next
    ``--refresh-tables`` re-snapshots them — the idempotent upsert apply tolerates
    the duplicate snapshot rows (an at-least-once window). Adopting first (the bug)
    would pin the tables with no snapshot on any failure, and the new-table warning
    would never fire again.
    """
    if not (refresh_tables and new_tables):
        return 0, []
    chunk = cfg.cdc.apply_batch if cfg.cdc.apply_batch and cfg.cdc.apply_batch > 0 else 10_000
    # The durable tail (post-append) is the collision ground truth: when dedup
    # dropped every re-captured record, the records list is empty but the trail
    # still holds [base, 0..k] rows from the crashed prior cycle.
    start_seq = _snapshot_start_seq(records, base,
                                    tail_key=source_pos_key(trail.last_source_pos()))
    snap_rows, snap_skipped = _snapshot_tables(
        adapter, trail, schema_name, schema_ir, new_tables, base, start_seq, chunk)
    # Adopt only now that the snapshot rows are durably in the trail.
    reg.register(name, schema_name, current_names, adopt_tables=True)
    return snap_rows, snap_skipped


def _mysql_coord_base(coord: str) -> Optional[int]:
    key = _mysql_coord_key(coord)
    return key[0] if key else None


def _pg_coord_base(lsn: str) -> Optional[int]:
    if not lsn:
        return None
    key = _pg_coord_key(lsn)
    return key[0] if key else None


def _mysql_coord_parts(coord: str) -> Optional[Tuple[int, int]]:
    """``(file_index, byte_pos)`` of a ``<base>.<NNNNNN>:<pos>`` binlog coordinate,
    or ``None`` if it cannot be parsed. Unlike :func:`_mysql_coord_base` this keeps
    the file index and byte offset SEPARATE, so lag can report them independently
    (a raw encoded delta across a file rollover is meaningless — it mixes a
    ``file-index << 48`` jump with a byte offset)."""
    log_file, _, log_pos = (coord or "").rpartition(":")
    if not log_file or not log_pos.isdigit():
        return None
    _, _, num = log_file.rpartition(".")
    try:
        return (int(num), int(log_pos))
    except ValueError:
        return None


def _mysql_binlog_behind(current: Optional[str],
                         trailed: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """``(files_behind, bytes_behind)`` of *trailed* relative to the head *current*.

    Byte offsets only compare within one binlog file, so: same file -> the true
    byte delta and ``files_behind=0``; across a rollover -> ``bytes_behind`` is the
    head's own offset within its file (a lower bound on the WAL still to read) and
    ``files_behind`` counts the whole files in between. ``(None, None)`` when either
    coordinate is missing/unparseable."""
    cur = _mysql_coord_parts(current) if current else None
    tr = _mysql_coord_parts(trailed) if trailed else None
    if cur is None or tr is None:
        return None, None
    files_behind = cur[0] - tr[0]
    bytes_behind = (cur[1] - tr[1]) if files_behind == 0 else cur[1]
    return files_behind, bytes_behind


# Source dialects whose capture is log-based (real I/U/D events, deletes
# included), so a key-set delete reconcile is redundant AND races the keymove:
# when the source has moved a PK but the keymove is not yet applied, reconcile
# would delete the old-key row and the later keymove would then insert a partial
# (TOAST-omitting) image at the new key -> silent TOAST loss with no crash. These
# sources therefore default reconcile OFF; snapshot sources (Oracle SCN, which
# cannot observe deletes) default it ON.
_LOG_BASED_DIALECTS = frozenset({"mysql", "postgresql"})


def _default_reconcile_deletes(cfg) -> bool:
    """Mode-aware default for delete reconciliation (not a hardcode): OFF for
    log-based sources (they carry explicit DELETE events, and reconcile races the
    keymove), ON for snapshot sources (Oracle SCN watermark can't see deletes)."""
    return getattr(cfg.source.dialect, "value", cfg.source.dialect) not in _LOG_BASED_DIALECTS


def run_replicat(cfg, name: str, reconcile_deletes: Optional[bool] = None) -> Dict[str, object]:
    from ..config.store import build_source_adapter, build_target_driver
    from ..constants import Edition

    # ``None`` means "no explicit --reconcile-deletes/--no-deletes": pick the
    # source-mode-aware default. An explicit True/False from the CLI/MCP wins,
    # so --reconcile-deletes still forces the reconcile on a log-based source
    # (with the documented lag-race caveat).
    if reconcile_deletes is None:
        reconcile_deletes = _default_reconcile_deletes(cfg)

    reg = CdcRegistry(_registry_path(cfg))
    try:
        ext = reg.get(name)
        if ext is None:
            raise Any2HeliosError("no such extract '{}'; run `a2h extract {}` first".format(name, name))
        # Keep the source open: it supplies the apply-side schema (PKs/columns) and,
        # for delete reconciliation, the current key set.
        adapter = build_source_adapter(cfg)
        adapter.connect()
        target = build_target_driver(cfg)
        target.connect()
        try:
            # Gate the apply on a live capability probe: refuse editions whose
            # keyed upsert can't run, with a clear message instead of a cryptic
            # mid-apply SQL error.
            caps = target.probe_capabilities()
            if caps.edition is Edition.NANO:
                ver = _version_tuple(caps.server_version)
                if ver is None or ver < _NANO_MIN_CDC_VERSION:
                    raise Any2HeliosError(
                        "CDC apply (replicat) on HeliosDB-Nano requires >= {}: before "
                        "that, INSERT ... ON CONFLICT DO UPDATE couldn't resolve a quoted "
                        "SET target and silently corrupted keyed upserts (#34). Detected "
                        "Nano version {!r}. Upgrade Nano, or use `a2h migrate` for a "
                        "one-shot load.".format(
                            ".".join(map(str, _NANO_MIN_CDC_VERSION)),
                            caps.server_version or "unknown"))
            schema_ir = adapter.introspect_schema(ext.schema)
            rep = Replicat(target, schema_ir, cfg.options.preserve_case)
            trail_dir = _trail_dir(cfg, name)
            trail = Trail(trail_dir, rotate_mb=cfg.cdc.trail_rotate_mb)
            apply_batch = cfg.cdc.apply_batch
            limit = apply_batch if apply_batch and apply_batch > 0 else None
            poison_retries = cfg.cdc.poison_retries
            poison_max = cfg.cdc.poison_max_per_run
            dead = DeadLetter(trail_dir)
            # Dedup dead-letters by source_pos across replays (a crash between the
            # dead-letter append and the cursor write would otherwise re-record a
            # poison record). Poison policy is off when poison_retries <= 0.
            dead_seen = dead.seen_source_positions() if poison_retries > 0 else set()
            poisoned = [0]

            def _poison_cb(chunk_start: int):
                # Factory: bind this chunk's start line so a dead-letter records the
                # failing record's actual trail cursor (chunk_start + its offset).
                def _cb(record, reason: str, offset: int) -> bool:
                    key = DeadLetter._pos_key(record)
                    if key is not None and key in dead_seen:
                        return False  # already dead-lettered on a prior (crashed) run
                    dead.append(record, reason, cursor=chunk_start + offset)
                    if key is not None:
                        dead_seen.add(key)
                    poisoned[0] += 1
                    # Mass-poison circuit breaker: a flood of dead-letters in one run
                    # is almost always an environment fault (wrong target / schema
                    # drift), not bad data. Fail closed so the operator investigates;
                    # the cursor for this chunk stays put (this raise propagates out).
                    if poison_max and poisoned[0] > poison_max:
                        raise Any2HeliosError(
                            "CDC replicat {!r}: dead-lettered {} record(s) in one run, "
                            "over the poison_max_per_run={} circuit breaker. A flood of "
                            "poison usually means a target/schema problem, not bad data — "
                            "refusing to keep parking records and advancing the cursor. "
                            "Inspect dead_letter.jsonl and the target, then re-run.".format(
                                name, poisoned[0], poison_max))
                    return True
                return _cb

            # Apply in memory-bounded chunks (tier-2 apply_batch): read up to `limit`
            # records, apply them, persist the exact per-chunk cursor, repeat. The
            # keymove barrier composes — within each chunk the cursor is persisted
            # just before and just after every keymove, so a keymove can only ever
            # REPLAY alone (never batched with a neighbour), the single op whose
            # replay is not target-state-idempotent. Each trail line is one record,
            # so ``start + consumed`` is the line the barrier has durably applied
            # through; the exact line cursor is persisted after each chunk.
            applied = 0
            read_total = 0
            warnings: List[str] = []

            def _flush_from(base_cursor: int):
                # Persist the per-keymove barrier cursor relative to this chunk's
                # start line (``base + consumed``). A factory so each chunk binds
                # its own start without a shared-closure loop-variable hazard.
                def _cb(consumed: int) -> None:
                    reg.set_apply_cursor(name, base_cursor + consumed)
                return _cb

            # The whole read/apply/persist loop runs under the exclusive trail
            # lock: the cursors it persists are GLOBAL line indices, so a purge
            # (or a purge-crash leftover) shifting the index space mid-run would
            # bake a wrong cursor into the registry. Reconcile any purge-crash
            # leftover FIRST — under the same lock — so this run never reads
            # double-counted indices (that deflation-skip was the B1 residual).
            with trail.exclusive("this replicat run"):
                trail.reconcile_purged()
                start = ext.apply_cursor
                while True:
                    records, next_cursor = trail.read(start, limit=limit)
                    if not records:
                        if next_cursor != start:
                            # Only trailing blanks / an excluded torn tail remained —
                            # advance the cursor past them so we do not re-scan forever.
                            reg.set_apply_cursor(name, next_cursor)
                        break
                    a, w = rep.apply_barriered(
                        records,
                        on_flush=_flush_from(start),
                        poison_retries=poison_retries,
                        on_poison=_poison_cb(start))
                    applied += a
                    warnings += w
                    read_total += len(records)
                    # Make the per-chunk cursor exact (covers any blank line that
                    # would make the record count trail the line count).
                    reg.set_apply_cursor(name, next_cursor)
                    start = next_cursor
            deleted = 0
            if reconcile_deletes:
                deleted, dwarn = rep.reconcile_deletes(adapter, chunk_size=(limit or 0))
                warnings = warnings + dwarn
            return {"applied": applied, "deleted": deleted, "cursor": start,
                    "read": read_total, "warnings": warnings,
                    "dead_lettered": poisoned[0], "dead_letter_total": dead.count()}
        finally:
            target.close()
            adapter.close()
    finally:
        reg.close()


def list_extracts(cfg) -> List[Extract]:
    reg = CdcRegistry(_registry_path(cfg))
    try:
        return reg.list()
    finally:
        reg.close()


def dead_letter_count(cfg, name: str) -> int:
    """Number of poison records the replicat has parked for *name* (tier-2 H4)."""
    return DeadLetter(_trail_dir(cfg, name)).count()


def drop_extract(cfg, name: str, purge_trail: bool = False) -> Dict[str, object]:
    """Tear down an extract (tier-2 H3): drop the PG logical slot so it stops
    pinning WAL, remove the registry entry, and (only with *purge_trail*) delete
    the trail directory. Keeping the trail by default is safe — the apply cursor
    lives in the registry, so a re-registered extract would re-derive it — but a
    dropped registry entry means a fresh ``extract`` re-anchors, so purge the trail
    when you truly want a clean slate.
    """
    from ..config.store import build_source_adapter
    from ..constants import SourceDialect

    reg = CdcRegistry(_registry_path(cfg))
    dropped_slot = False
    try:
        ext = reg.get(name)
        if ext is None:
            raise Any2HeliosError(
                "no such extract '{}'; nothing to drop (see `a2h extracts`)".format(name))
        if cfg.source.dialect is SourceDialect.POSTGRESQL:
            from .sources.postgres_logical import PostgresLogicalSource, slot_name

            adapter = build_source_adapter(cfg)
            try:
                adapter.connect()
                # drop_slot returns True (dropped) / False (already absent) and
                # RAISES on a real failure — we must NOT swallow it, or a slot that
                # is still pinning WAL would vanish from `a2h extracts` while we
                # report success and remove the registry entry.
                dropped_slot = PostgresLogicalSource(adapter, ext.schema, [], name).drop_slot()
            except Any2HeliosError:
                raise
            except Exception as e:  # noqa: BLE001
                raise Any2HeliosError(
                    "CDC extract {!r}: failed to drop its PostgreSQL replication slot "
                    "{!r}: {}. The slot may still be pinning WAL, so the registry entry "
                    "was NOT removed (it stays visible in `a2h extracts` / --lag). "
                    "Resolve the slot — it may still be active on another connection — "
                    "then retry --drop.".format(name, slot_name(name), e)) from e
            finally:
                try:
                    adapter.close()
                except Exception:  # noqa: BLE001
                    pass
        removed = reg.remove(name)  # only reached when the slot drop succeeded or was absent
    finally:
        reg.close()
    purged = False
    if purge_trail:
        import shutil

        d = _trail_dir(cfg, name)
        if os.path.isdir(d):
            shutil.rmtree(d)
            purged = True
    return {"name": name, "removed": removed, "dropped_slot": dropped_slot,
            "purged_trail": purged}


def purge_applied_segments(cfg, name: str) -> Dict[str, object]:
    """Delete fully-applied closed trail segments for *name* (tier-2 H5).

    Reads the extract's durable apply cursor and drops every closed segment whose
    lines are all at-or-below it (never the active segment, never past the
    cursor). Safe to run any time; a no-op when nothing is rotated/applied.
    """
    reg = CdcRegistry(_registry_path(cfg))
    try:
        ext = reg.get(name)
        if ext is None:
            raise Any2HeliosError(
                "no such extract '{}'; nothing to purge (see `a2h extracts`)".format(name))
        cursor = ext.apply_cursor
    finally:
        reg.close()
    trail = Trail(_trail_dir(cfg, name), rotate_mb=cfg.cdc.trail_rotate_mb)
    # Same exclusive lock as the replicat run: purging shifts global line
    # indices, which must never happen under a running replicat's feet.
    with trail.exclusive("--purge-applied"):
        deleted = trail.purge_applied(cursor)
    return {"name": name, "cursor": cursor, "purged_segments": len(deleted),
            "purged_paths": deleted}


def extract_lag(cfg, ext: Extract) -> Optional[Dict[str, object]]:
    """Best-effort replication lag for *ext* (tier-2 H3), or ``None`` if the
    source is unreachable / the probe is not permitted.

    * PostgreSQL — the slot's ``confirmed_flush_lsn`` vs ``pg_current_wal_lsn()``
      (``bytes_behind`` = WAL the slot still pins).
    * MySQL — the extract's persisted binlog coordinate vs the server's head
      (``SHOW BINARY LOG STATUS``).
    * Oracle — the capture watermark SCN vs the current SCN.
    """
    from ..config.store import build_source_adapter
    from ..constants import SourceDialect

    dialect = cfg.source.dialect
    try:
        if dialect is SourceDialect.POSTGRESQL:
            adapter = build_source_adapter(cfg)
            adapter.connect()
            try:
                from .sources.postgres_logical import PostgresLogicalSource

                lag = PostgresLogicalSource(adapter, ext.schema, [], ext.name).lag()
                if lag is not None:
                    lag["mode"] = "logical"
                return lag
            finally:
                adapter.close()
        if dialect is SourceDialect.MYSQL:
            from .sources.mysql_binlog import MySqlBinlogSource

            try:
                trailed = read_pos(_binlog_pos_file(cfg, ext.name))
            except Exception:  # noqa: BLE001 - a missing/corrupt pos file just means "unknown"
                trailed = None
            current = MySqlBinlogSource(cfg.source.to_dsn(), ext.schema, []).master_position()
            files_behind, bytes_behind = _mysql_binlog_behind(current, trailed)
            return {"mode": "binlog", "trailed_pos": trailed or None,
                    "current_pos": current or None,
                    "files_behind": files_behind, "bytes_behind": bytes_behind}
        if dialect is SourceDialect.ORACLE:
            adapter = build_source_adapter(cfg)
            adapter.connect()
            try:
                # current_scn lives on the Oracle adapter only (not the ABC).
                current_scn = int(getattr(adapter, "current_scn", lambda: 0)())
                behind = (current_scn - ext.watermark) if current_scn else None
                return {"mode": "scn", "watermark_scn": ext.watermark,
                        "current_scn": current_scn, "scn_behind": behind}
            finally:
                adapter.close()
    except Exception:  # noqa: BLE001 - lag is advisory; never fail the listing
        return None
    return None
