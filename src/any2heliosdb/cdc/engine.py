"""CDC engine: wires registry + source capture + trail + replicat apply.

Symmetric Extract -> trail -> Replicat so capture and apply advance on their own
durable cursors. v1 source is Oracle SCN-watermark; the trail and replicat are
source-agnostic, so log-based sources (v2) and HeliosDB-as-source drop in here.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Tuple

from ..core.change_record import ChangeRecord, source_pos_key
from ..errors import Any2HeliosError
from .posfile import read_pos, write_pos_atomic
from .registry import CdcRegistry, Extract
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


def run_extract(cfg, name: str) -> Dict[str, object]:
    from ..config.store import build_source_adapter
    from ..constants import SourceDialect

    reg = CdcRegistry(_registry_path(cfg))
    adapter = build_source_adapter(cfg)
    adapter.connect()
    try:
        schema_ir = adapter.introspect_schema(cfg.source.schema)
        schema_name = cfg.source.schema or schema_ir.name
        reg.register(name, schema_name, [t.name for t in schema_ir.tables])
        ext = reg.get(name)
        assert ext is not None
        trail = Trail(_trail_dir(cfg, name))

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
            mysql_source = MySqlBinlogSource(cfg.source.to_dsn(), schema_name, schema_ir.tables)
            records, new_pos = mysql_source.capture(since or "")
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
            # Persist the advanced cursor atomically (temp + fsync + os.replace)
            # only AFTER the records are durably in the trail, so a crash re-reads
            # from the old cursor rather than losing the window.
            write_pos_atomic(posf, new_pos)
            return {"captured": captured, "watermark": new_pos,
                    "since": since or "(current)", "skipped": [], "mode": "binlog"}

        if cfg.source.dialect is SourceDialect.POSTGRESQL:
            # Log-based capture via PostgreSQL logical decoding (test_decoding):
            # real I/U/D, deletes included. Peek -> persist to trail -> advance the
            # slot, so a crash re-reads rather than loses changes. The slot is the
            # durable server-side cursor; we mirror its LSN into the pos file for
            # display + `a2h status`.
            from .sources.postgres_logical import PostgresLogicalSource

            pg_source = PostgresLogicalSource(adapter, schema_name, schema_ir.tables, name)
            # LSNs are only comparable within one cluster lifetime + timeline.
            # Check the coordinate-space identity BEFORE peeking: a PITR restore
            # or a different cluster can emit new LSNs that straddle the stale
            # trail tail, which the LSN-order guard below cannot distinguish from
            # a legitimate crash-window overlap (dedup would silently drop the
            # genuinely new changes).
            _require_matching_epoch_identity(
                _pg_epoch_file(cfg, name), pg_source.epoch_identity(), name)
            records, new_lsn, skipped = pg_source.capture()
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
            # The slot is the durable cursor; this file only mirrors the LSN for
            # display/`a2h status`. Still write it atomically so it never shows a
            # torn value.
            posf = _binlog_pos_file(cfg, name)
            if new_lsn:
                write_pos_atomic(posf, new_lsn)
            return {"captured": captured, "watermark": new_lsn or "(slot)",
                    "since": "(slot)", "skipped": skipped, "mode": "logical"}

        # Default: Oracle SCN-watermark capture.
        oracle_source = OracleScnSource(adapter, schema_name, schema_ir.tables)
        records, new_watermark, skipped = oracle_source.capture(ext.watermark)
        captured = trail.append(records)
        reg.set_watermark(name, new_watermark)
        return {"captured": captured, "watermark": new_watermark,
                "since": ext.watermark, "skipped": skipped, "mode": "scn"}
    finally:
        adapter.close()
        reg.close()


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
            start = ext.apply_cursor
            records, new_cursor = Trail(_trail_dir(cfg, name)).read(start)
            # Apply with a durability barrier around every keymove: the cursor is
            # persisted just before and just after each keymove so a keymove can
            # only ever be REPLAYED alone (never batched with a neighbour), which is
            # the single op whose replay is not target-state-idempotent. Each trail
            # line is one record, so ``start + consumed`` is the line the barrier
            # has durably applied through. Non-keymove-only slices flush once at the
            # end — the same single cursor advance as before.
            applied, warnings = rep.apply_barriered(
                records, on_flush=lambda consumed: reg.set_apply_cursor(name, start + consumed))
            # Make the final cursor exact (covers an empty read and any blank line
            # that would make the record count trail the line count).
            reg.set_apply_cursor(name, new_cursor)
            deleted = 0
            if reconcile_deletes:
                deleted, dwarn = rep.reconcile_deletes(adapter)
                warnings = warnings + dwarn
            return {"applied": applied, "deleted": deleted, "cursor": new_cursor,
                    "read": len(records), "warnings": warnings}
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
