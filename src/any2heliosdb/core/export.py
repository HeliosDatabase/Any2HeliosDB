"""Render an introspected schema to HeliosDB target DDL — the single engine path
behind both ``a2h export`` (CLI, writes files) and the ``export`` MCP tool
(returns the text). Keeping the builder here is the only guard against the two
surfaces drifting: the CLI would write one DDL while an agent got another.

Pure text: it opens NO target connection. The target driver is built only to read
its dialect + capabilities so the view translator can render each view toward the
target (backtick identifiers -> PG quoting, MySQL ``IF()`` -> ``CASE``, NVL/DECODE,
…), exactly as ``migrate`` does.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config.model import ProjectConfig
    from .catalog_model import Schema


def build_ddl(cfg: "ProjectConfig", schema: "Schema") -> str:
    """Return the full target DDL for *schema* (tables, sequences, indexes, views,
    foreign keys) — byte-identical to what ``a2h export`` writes to ``schema.sql``.
    """
    from ..config.store import build_target_driver, build_type_registry
    from ..core.catalog_model import DataTypeKind
    from ..core.orchestrator import _order_views, _portable_view
    from ..emit import ddl

    pc = cfg.options.preserve_case
    reg = build_type_registry(cfg)
    parts = []
    for t in schema.tables:
        parts.append(ddl.render_create_table(t, reg, pc))
    for s in schema.sequences:
        parts.append(ddl.render_sequence(s, pc))
    for t in schema.tables:
        for idx in t.indexes:
            stmt = ddl.render_index(t, idx, pc)
            if stmt:
                parts.append(stmt)
    # Views are emitted as TARGET DDL. The target driver is built only to read its
    # dialect + capabilities; no connection is opened.
    try:
        _tgt = build_target_driver(cfg)
        view_dialect = getattr(_tgt, "dialect", "postgres")
        view_caps = _tgt.capabilities
    except Exception:  # noqa: BLE001 -- no/incomplete [target]: PG-wire default
        view_dialect, view_caps = "postgres", None
    bool_cols = {c.name for t in schema.tables for c in t.columns
                 if c.data_type.kind is DataTypeKind.BOOLEAN}
    # Dependency order so a view referencing another view is written after it.
    for v in _order_views(schema.views):
        pv, _notes = _portable_view(v, view_dialect, view_caps, bool_cols)
        parts.append(ddl.render_view(pv, pc))
    for t in schema.tables:
        parts.extend(ddl.render_foreign_keys(t, pc))
    return "\n\n".join(parts) + "\n"
