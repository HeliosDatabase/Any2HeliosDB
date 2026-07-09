"""The MCP tool catalog.

Each :class:`Tool` wraps one Any2HeliosDB *engine* function (never the CLI) and
returns structured JSON — the shapes an AI agent can branch on — rather than the
human-formatted text the CLI prints. Every tool declares the **minimum role**
required to call it (see :mod:`any2heliosdb.mcp.auth`); the dispatcher refuses a
caller whose role is below that bar with :class:`ForbiddenError` (→ 403 / MCP
error).

Heavy imports (psycopg, oracledb, the orchestrator, …) stay lazy inside each
handler so ``tools/list`` and the auth/RBAC path work with only the core deps
installed and never touch a database.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..errors import Any2HeliosError
from .auth import ForbiddenError, Principal, Role

# A handler takes the parsed argument dict and returns a JSON-serializable result.
Handler = Callable[[Dict[str, Any]], Dict[str, Any]]


@dataclass
class Tool:
    """One callable MCP tool."""

    name: str
    role: Role  # minimum role required to call it
    description: str
    handler: Handler
    # JSON-Schema (object) describing the arguments, surfaced via tools/list.
    properties: Dict[str, Any] = field(default_factory=dict)
    required: List[str] = field(default_factory=list)

    def input_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": self.properties,
            "required": list(self.required),
            "additionalProperties": True,
        }

    def to_meta(self) -> Dict[str, Any]:
        """The ``tools/list`` entry (MCP wire shape) augmented with the role."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema(),
            # Non-standard but harmless hint so agents can pre-filter by role.
            "_meta": {"requiredRole": self.role.value},
        }


# --- config resolution -------------------------------------------------------
# Every tool accepts EITHER a ``config`` path OR an inline ``source`` / ``target``
# / ``options`` block (+ optional ``data_type`` / ``modify_type`` / ``capability``).
# Inline config is serialized to a throwaway TOML file and fed through the very
# same ``load_config`` the CLI uses, so parsing/validation can't drift.
_CONFIG_PROP: Dict[str, Dict[str, Any]] = {
    "config": {
        "type": "string",
        "description": "Path to a project config TOML on the server.",
    },
    "source": {"type": "object", "description": "Inline [source] config block."},
    "target": {"type": "object", "description": "Inline [target] config block."},
    "options": {"type": "object", "description": "Inline [options] config block."},
    "data_type": {"type": "object", "description": "Ora2Pg-style DATA_TYPE overrides."},
    "modify_type": {"type": "object", "description": "Ora2Pg-style MODIFY_TYPE overrides."},
}


def _load_cfg(args: Dict[str, Any]):
    """Resolve a ProjectConfig from a ``config`` path or an inline config dict."""
    from ..config.store import load_config

    path = args.get("config")
    if path:
        return load_config(str(path))

    inline: Dict[str, Any] = {}
    for key in ("source", "target", "options", "data_type", "modify_type", "capability"):
        if isinstance(args.get(key), dict):
            inline[key] = args[key]
    if not inline:
        raise Any2HeliosError(
            "no config: pass a 'config' path or an inline 'source'/'target'/'options' block"
        )
    import tomli_w

    fd, tmp = tempfile.mkstemp(prefix="a2h_mcp_", suffix=".toml")
    try:
        with os.fdopen(fd, "wb") as f:
            tomli_w.dump(inline, f)
        return load_config(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _run_context(cfg):
    """Deterministic (manifest_path, run_id) — identical to the CLI's, so the MCP
    ``status``/``resume`` tools find the same ledger a CLI ``migrate`` created."""
    import hashlib

    key = "{}:{}/{}->{}:{}/{}".format(
        cfg.source.host, cfg.source.port, cfg.source.schema or "",
        cfg.target.host, cfg.target.port, cfg.target.dbname)
    run_id = "run_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    from ..core.manifest import manifest_path_for
    backend = getattr(cfg.options, "manifest_backend", "sqlite")
    return manifest_path_for(cfg.options.output_dir, backend), run_id


def _validation_to_dict(res) -> Dict[str, Any]:
    return {
        "validation_type": res.validation_type.value,
        "passed": bool(res.passed),
        "errors": [
            {"severity": e.severity.value, "table": e.table, "message": e.message}
            for e in res.errors
        ],
        "metrics": dict(res.metrics),
    }


def _stats_to_dict(stats) -> Dict[str, Any]:
    failed = int(stats.failed_chunks)
    return {
        "ok": failed == 0,
        "tables": stats.tables,
        "total_rows": stats.total_rows,
        "rows": dict(stats.rows),
        "load_mode": stats.load_mode,
        "failed_chunks": failed,
        "warnings": list(stats.warnings),
        "incomplete": failed > 0,
    }


# --- handlers ----------------------------------------------------------------
# Read-only / environment ------------------------------------------------------
def _h_doctor(args: Dict[str, Any]) -> Dict[str, Any]:
    import importlib.util

    checks = [
        ("psycopg", "psycopg (v3)", "target: PG-wire driver [core]"),
        ("oracledb", "oracledb", "source: Oracle / native target [extra: oracle]"),
        ("pymysql", "pymysql", "source: MySQL [extra: mysql]"),
        ("pyodbc", "pyodbc", "source: SQL Server [extra: mssql]"),
        ("typer", "typer", "CLI [core]"),
        ("rich", "rich", "CLI UX [core]"),
    ]
    components = []
    for mod, label, role in checks:
        available = importlib.util.find_spec(mod) is not None
        ver = ""
        if available:
            try:
                ver = getattr(__import__(mod), "__version__", "") or ""
            except Exception:  # noqa: BLE001
                ver = ""
        components.append(
            {"component": label, "module": mod, "available": available,
             "version": ver, "role": role}
        )
    return {"ok": True, "components": components}


def _h_smoke_test(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..config.wizard import smoke_test

    cfg = _load_cfg(args)
    report = smoke_test(cfg)
    return {"ok": True, "report": report}


def _h_assess(args: Dict[str, Any]) -> Dict[str, Any]:
    from dataclasses import asdict

    from ..assess.report import build_report
    from ..config.store import build_source_adapter, build_type_registry

    cfg = _load_cfg(args)
    src = build_source_adapter(cfg)
    src.connect()
    try:
        schema = src.introspect_schema(cfg.source.schema)
        report = build_report(schema, build_type_registry(cfg))
        data = asdict(report)
        # ``asdict`` keeps the Edition Enum instance; normalize to its value so
        # the result is plain JSON (mirrors assess.render).
        data["edition"] = getattr(report.edition, "value", report.edition)
        return {"ok": True, "report": data}
    finally:
        src.close()


def _h_status(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..core.manifest import Manifest

    cfg = _load_cfg(args)
    manifest_path, run_id = _run_context(cfg)
    if not os.path.exists(manifest_path):
        return {"ok": False, "exists": False, "manifest_path": manifest_path,
                "message": "no manifest (run migrate first)"}
    man = Manifest.open_readonly(manifest_path)
    try:
        summary = man.summary(run_id)
        pending = man.pending_chunks(run_id)
        return {
            "ok": True,
            "exists": True,
            "run_id": run_id,
            "manifest_path": manifest_path,
            "chunk_states": summary["chunk_states"],
            "rows_loaded": summary["rows_loaded"],
            "rows_by_table": man.rows_by_table(run_id),
            "pending_chunks": len(pending),
            "complete": not pending,
        }
    finally:
        man.close()


def _h_extracts(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..cdc.engine import list_extracts

    cfg = _load_cfg(args)
    rows = list_extracts(cfg)
    return {
        "ok": True,
        "extracts": [
            {"name": e.name, "schema": e.schema, "tables": len(e.tables),
             "watermark": e.watermark, "apply_cursor": e.apply_cursor, "state": e.state}
            for e in rows
        ],
    }


def _h_list_config(args: Dict[str, Any]) -> Dict[str, Any]:
    """Echo back the resolved config (passwords redacted) — read-only inspection."""
    from ..config.store import to_toml_dict

    cfg = _load_cfg(args)
    d = to_toml_dict(cfg)
    for side in ("source", "target"):
        if isinstance(d.get(side), dict):
            d[side].pop("password", None)  # never echo a literal secret
    return {"ok": True, "config": d}


def _h_validate_config(args: Dict[str, Any]) -> Dict[str, Any]:
    """Validate that a config loads and its runtime objects can be built (no I/O)."""
    from ..config.store import (build_source_adapter, build_target_driver,
                                build_type_registry)

    try:
        cfg = _load_cfg(args)
        build_source_adapter(cfg)
        build_target_driver(cfg)
        build_type_registry(cfg)
    except Any2HeliosError as e:
        return {"ok": False, "valid": False, "error": str(e)}
    return {
        "ok": True,
        "valid": True,
        "source_dialect": cfg.source.dialect.value,
        "target_driver": cfg.target.driver.value,
    }


def _h_test(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..config.store import build_source_adapter, build_target_driver
    from ..validate.structure import run_test

    cfg = _load_cfg(args)
    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect()
    tgt.connect()
    try:
        res = run_test(src.introspect_schema(cfg.source.schema), tgt, cfg.options.preserve_case)
        return {"ok": res.passed, "result": _validation_to_dict(res)}
    finally:
        src.close()
        tgt.close()


def _h_test_count(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..config.store import build_source_adapter, build_target_driver
    from ..validate.counts import run_test_count

    cfg = _load_cfg(args)
    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect()
    tgt.connect()
    try:
        schema = src.introspect_schema(cfg.source.schema)
        res = run_test_count(src, tgt, schema.tables, cfg.options.preserve_case)
        return {"ok": res.passed, "result": _validation_to_dict(res)}
    finally:
        src.close()
        tgt.close()


def _h_test_data(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..config.store import build_source_adapter, build_target_driver
    from ..validate.data import run_test_data

    cfg = _load_cfg(args)
    sample = int(args.get("sample", 1000))
    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect()
    tgt.connect()
    results = []
    passed = True
    try:
        for t in src.introspect_schema(cfg.source.schema).tables:
            res = run_test_data(src, tgt, t, sample_rows=sample,
                                preserve_case=cfg.options.preserve_case)
            results.append(_validation_to_dict(res))
            passed = passed and res.passed
        return {"ok": passed, "results": results}
    finally:
        src.close()
        tgt.close()


# Operator (write) ------------------------------------------------------------
def _h_migrate(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..config.store import (build_source_adapter, build_target_driver,
                                build_type_registry)
    from ..core.orchestrator import migrate as run_migrate

    cfg = _load_cfg(args)
    if "parallelism" in args:
        cfg.options.parallelism = int(args["parallelism"])
    if "batch_size" in args:
        cfg.options.batch_size = int(args["batch_size"])
    if "drop_existing" in args:
        cfg.options.drop_existing = bool(args["drop_existing"])
    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect()
    tgt.connect()
    try:
        manifest_path, run_id = _run_context(cfg)
        stats = run_migrate(
            src, tgt, schema=cfg.source.schema, registry=build_type_registry(cfg),
            drop_existing=cfg.options.drop_existing, preserve_case=cfg.options.preserve_case,
            batch_size=cfg.options.batch_size, prefer_copy=cfg.options.prefer_copy,
            cfg=cfg, manifest_path=manifest_path, run_id=run_id,
            parallelism=cfg.options.parallelism,
        )
        out = _stats_to_dict(stats)
        out["run_id"] = run_id
        return out
    finally:
        src.close()
        tgt.close()


def _h_resume(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..config.store import (build_source_adapter, build_target_driver,
                                build_type_registry)
    from ..core.orchestrator import migrate as run_migrate

    cfg = _load_cfg(args)
    manifest_path, run_id = _run_context(cfg)
    if not os.path.exists(manifest_path):
        return {"ok": False, "error": "no manifest to resume at {}".format(manifest_path)}
    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect()
    tgt.connect()
    try:
        stats = run_migrate(
            src, tgt, schema=cfg.source.schema, registry=build_type_registry(cfg),
            drop_existing=False, preserve_case=cfg.options.preserve_case,
            prefer_copy=cfg.options.prefer_copy, cfg=cfg, manifest_path=manifest_path,
            run_id=run_id, parallelism=cfg.options.parallelism, do_schema=False,
        )
        out = _stats_to_dict(stats)
        out["run_id"] = run_id
        return out
    finally:
        src.close()
        tgt.close()


# Admin (CDC capture/apply + config write) ------------------------------------
def _h_extract(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..cdc.engine import run_extract

    cfg = _load_cfg(args)
    name = args.get("name")
    if not name:
        raise Any2HeliosError("extract requires a 'name'")
    r = run_extract(cfg, str(name))
    r["ok"] = True
    return r


def _h_replicat(args: Dict[str, Any]) -> Dict[str, Any]:
    from ..cdc.engine import run_replicat

    cfg = _load_cfg(args)
    name = args.get("name")
    if not name:
        raise Any2HeliosError("replicat requires a 'name'")
    reconcile = bool(args.get("reconcile_deletes", True))
    r = run_replicat(cfg, str(name), reconcile_deletes=reconcile)
    r["ok"] = True
    return r


def _h_wizard(args: Dict[str, Any]) -> Dict[str, Any]:
    """Headless config write: run the smoke test on an inline/parameterized config
    and persist it to ``output`` (the interactive prompts have no place over MCP)."""
    from ..config.store import save_config
    from ..config.wizard import smoke_test

    cfg = _load_cfg(args)
    out_path = args.get("output", "config.toml")
    report: Dict[str, Any] = {}
    smoke_ok = True
    try:
        report = smoke_test(cfg)
        cfg.capability = {
            k: v for k, v in report.items()
            if k.startswith(("copy", "enforces", "target_edition"))
        }
    except Any2HeliosError as e:
        smoke_ok = False
        report = {"error": str(e)}
    save_config(cfg, str(out_path))
    return {"ok": True, "written": str(out_path), "smoke_ok": smoke_ok, "report": report}


# --- catalog -----------------------------------------------------------------
def build_catalog() -> "ToolRegistry":
    """Construct the full tool registry with per-tool role gates."""
    cfg_props = dict(_CONFIG_PROP)
    reg = ToolRegistry()

    # viewer (read-only)
    reg.add(Tool("doctor", Role.VIEWER,
                 "Report local environment: core deps + available source-DB drivers.",
                 _h_doctor, properties={}))
    reg.add(Tool("smoke_test", Role.VIEWER,
                 "Connect both ends, detect edition/capabilities, round-trip a tiny COPY.",
                 _h_smoke_test, properties=dict(cfg_props)))
    reg.add(Tool("assess", Role.VIEWER,
                 "Inventory + type-mapping + gap/cost assessment of the source schema.",
                 _h_assess, properties=dict(cfg_props)))
    reg.add(Tool("status", Role.VIEWER,
                 "Progress of the current/last migration run from the manifest.",
                 _h_status, properties=dict(cfg_props)))
    reg.add(Tool("extracts", Role.VIEWER,
                 "List registered CDC extracts and their capture/apply positions.",
                 _h_extracts, properties=dict(cfg_props)))
    reg.add(Tool("test", Role.VIEWER,
                 "TEST: object-inventory diff (source vs target).",
                 _h_test, properties=dict(cfg_props)))
    reg.add(Tool("test_count", Role.VIEWER,
                 "TEST_COUNT: row counts on both sides.",
                 _h_test_count, properties=dict(cfg_props)))
    sample_props = dict(cfg_props)
    sample_props["sample"] = {"type": "integer",
                              "description": "Rows per table to compare (0 = all).",
                              "default": 1000}
    reg.add(Tool("test_data", Role.VIEWER,
                 "TEST_DATA: ordered, sampled row comparison + checksums.",
                 _h_test_data, properties=sample_props))
    reg.add(Tool("list_config", Role.VIEWER,
                 "Echo the resolved config (passwords redacted).",
                 _h_list_config, properties=dict(cfg_props)))
    reg.add(Tool("validate_config", Role.VIEWER,
                 "Validate a config loads and its runtime objects build (no I/O).",
                 _h_validate_config, properties=dict(cfg_props)))

    # operator (+ write/migrate)
    migrate_props = dict(cfg_props)
    migrate_props["parallelism"] = {"type": "integer", "description": "Override parallel workers."}
    migrate_props["batch_size"] = {"type": "integer", "description": "Override fetch/insert batch size."}
    migrate_props["drop_existing"] = {"type": "boolean", "description": "Drop target tables first."}
    reg.add(Tool("migrate", Role.OPERATOR,
                 "Run a full schema+data migration; returns structured stats.",
                 _h_migrate, properties=migrate_props))
    reg.add(Tool("load", Role.OPERATOR,
                 "Load data (currently runs the full migrate).",
                 _h_migrate, properties=migrate_props))
    reg.add(Tool("resume", Role.OPERATOR,
                 "Resume an interrupted migration run from its manifest.",
                 _h_resume, properties=dict(cfg_props)))

    # admin (+ CDC capture/apply + config write)
    name_props = dict(cfg_props)
    name_props["name"] = {"type": "string", "description": "Extract/replicat process name."}
    reg.add(Tool("extract", Role.ADMIN,
                 "Capture source changes into the named CDC trail.",
                 _h_extract, properties=name_props, required=["name"]))
    rep_props = dict(name_props)
    rep_props["reconcile_deletes"] = {"type": "boolean",
                                      "description": "Reconcile deletes via key-set diff.",
                                      "default": True}
    reg.add(Tool("replicat", Role.ADMIN,
                 "Apply captured CDC changes from the named trail to the target.",
                 _h_replicat, properties=rep_props, required=["name"]))
    wiz_props = dict(cfg_props)
    wiz_props["output"] = {"type": "string", "description": "Path to write the config TOML.",
                           "default": "config.toml"}
    reg.add(Tool("wizard", Role.ADMIN,
                 "Headless config write: smoke-test a config and persist it.",
                 _h_wizard, properties=wiz_props))
    return reg


class ToolRegistry:
    """Holds the tools and enforces RBAC on dispatch."""

    def __init__(self) -> None:
        self._tools: "Dict[str, Tool]" = {}

    def add(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def visible_to(self, principal: Principal) -> List[Tool]:
        """Tools the principal's role is permitted to call (for ``tools/list``)."""
        return [t for t in (self._tools[n] for n in self.names())
                if principal.role.can(t.role)]

    def list_meta(self, principal: Principal) -> List[Dict[str, Any]]:
        return [t.to_meta() for t in self.visible_to(principal)]

    def call(self, principal: Principal, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatch a tool call after enforcing RBAC.

        Raises :class:`ForbiddenError` (→403) when the caller's role is below the
        tool's bar, :class:`KeyError` for an unknown tool, and propagates any
        :class:`Any2HeliosError` from the handler.
        """
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError("unknown tool: {}".format(name))
        if not principal.role.can(tool.role):
            raise ForbiddenError(
                "role '{}' may not call '{}' (requires '{}')".format(
                    principal.role.value, name, tool.role.value)
            )
        return tool.handler(args or {})
