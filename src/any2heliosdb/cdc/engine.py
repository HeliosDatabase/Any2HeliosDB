"""CDC engine: wires registry + source capture + trail + replicat apply.

Symmetric Extract -> trail -> Replicat so capture and apply advance on their own
durable cursors. v1 source is Oracle SCN-watermark; the trail and replicat are
source-agnostic, so log-based sources (v2) and HeliosDB-as-source drop in here.
"""
from __future__ import annotations

import os
import re
from typing import Dict, List

from ..errors import Any2HeliosError
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


def _version_tuple(version: str):  # type: ignore[no-untyped-def]
    """First X.Y.Z in a HeliosDB version banner as an int tuple, else None."""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version or "")
    return tuple(int(g) for g in m.groups()) if m else None


def _registry_path(cfg) -> str:  # type: ignore[no-untyped-def]
    return os.path.join(cfg.options.output_dir, "cdc.db")


def _trail_dir(cfg, name: str) -> str:  # type: ignore[no-untyped-def]
    return os.path.join(cfg.options.output_dir, "trail", name)


def _binlog_pos_file(cfg, name: str) -> str:  # type: ignore[no-untyped-def]
    return os.path.join(_trail_dir(cfg, name), "binlog.pos")


def run_extract(cfg, name: str) -> Dict[str, object]:  # type: ignore[no-untyped-def]
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
            since = ""
            if os.path.exists(posf):
                with open(posf) as f:
                    since = f.read().strip()
            source = MySqlBinlogSource(cfg.source.to_dsn(), schema_name, schema_ir.tables)
            records, new_pos = source.capture(since)
            captured = trail.append(records)
            with open(posf, "w") as f:
                f.write(new_pos)
            return {"captured": captured, "watermark": new_pos,
                    "since": since or "(current)", "skipped": [], "mode": "binlog"}

        # Default: Oracle SCN-watermark capture.
        source = OracleScnSource(adapter, schema_name, schema_ir.tables)
        records, new_watermark, skipped = source.capture(ext.watermark)
        captured = trail.append(records)
        reg.set_watermark(name, new_watermark)
        return {"captured": captured, "watermark": new_watermark,
                "since": ext.watermark, "skipped": skipped, "mode": "scn"}
    finally:
        adapter.close()
        reg.close()


def run_replicat(cfg, name: str, reconcile_deletes: bool = True) -> Dict[str, object]:  # type: ignore[no-untyped-def]
    from ..config.store import build_source_adapter, build_target_driver
    from ..constants import Edition

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
            records, new_cursor = Trail(_trail_dir(cfg, name)).read(ext.apply_cursor)
            applied, warnings = rep.apply(records)
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


def list_extracts(cfg) -> List[Extract]:  # type: ignore[no-untyped-def]
    reg = CdcRegistry(_registry_path(cfg))
    try:
        return reg.list()
    finally:
        reg.close()
