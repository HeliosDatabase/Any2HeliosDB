"""Any2HeliosDB command-line interface (``a2h``).

Verbs mirror Ora2Pg's workflow, modernized: an interactive ``wizard`` plus
``assess`` / ``export`` / ``migrate`` / ``test*`` / ``report`` and the CDC verbs
``extract`` / ``replicat``. Heavy imports are deferred into each command so
``--help`` and ``doctor`` work with only the core deps installed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table as RichTable

from . import __version__

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,  # we render our own friendly errors in main()
    help="Migrate Oracle / MySQL / SQL Server into HeliosDB (a modern Ora2Pg successor).",
)
console = Console()
CONFIG_OPT = typer.Option("config.toml", "--config", "-c", help="Project config TOML.")


def _version_callback(value: bool) -> None:
    if value:
        console.print("a2h (any2heliosdb) {}".format(__version__))
        raise typer.Exit()


@app.callback()
def _main(
    version: Optional[bool] = typer.Option(
        None, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Any2HeliosDB — a2h."""


def _fail(msg: str) -> None:
    console.print("[red]error:[/red] {}".format(msg))
    raise typer.Exit(code=1)


def _run_context(cfg):  # type: ignore[no-untyped-def]
    """Deterministic (manifest_path, run_id) for a config, so `resume`/`status`
    find the same ledger as the `migrate` that created it."""
    key = "{}:{}/{}->{}:{}/{}".format(
        cfg.source.host, cfg.source.port, cfg.source.schema or "",
        cfg.target.host, cfg.target.port, cfg.target.dbname)
    run_id = "run_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return os.path.join(cfg.options.output_dir, "manifest.db"), run_id


def _print_validation(res) -> None:  # type: ignore[no-untyped-def]
    color = "green" if res.passed else "red"
    console.print("[{}]{}: {}[/]".format(color, res.validation_type.value,
                                         "PASS" if res.passed else "FAIL"))
    for e in res.errors:
        console.print("  [{}] {}: {}".format(e.severity.value, e.table, e.message))
    if res.metrics:
        console.print("  metrics: {}".format(res.metrics))


# --- environment / config ----------------------------------------------------
@app.command()
def doctor() -> None:
    """Check the local environment: core deps and which source-DB drivers are available."""
    table = RichTable(title="Any2HeliosDB environment")
    table.add_column("Component"); table.add_column("Status"); table.add_column("Detail")

    def check(mod: str, label: str, role: str) -> None:
        if importlib.util.find_spec(mod) is not None:
            ver = ""
            try:
                ver = getattr(__import__(mod), "__version__", "")
            except Exception:  # noqa: BLE001
                ver = ""
            table.add_row(label, "[green]available[/green]", "{} {}".format(role, ver))
        else:
            table.add_row(label, "[red]missing[/red]", role)

    check("psycopg", "psycopg (v3)", "target: PG-wire driver [core]")
    check("oracledb", "oracledb", "source: Oracle / native target [extra: oracle]")
    check("pymysql", "pymysql", "source: MySQL [extra: mysql]")
    check("pyodbc", "pyodbc", "source: SQL Server [extra: mssql]")
    check("typer", "typer", "CLI [core]")
    check("rich", "rich", "CLI UX [core]")
    console.print(table)


@app.command()
def wizard() -> None:
    """Interactively configure source + target and run a smoke test (replaces ora2pg.conf)."""
    from .config.wizard import run_wizard

    run_wizard()


# --- assessment --------------------------------------------------------------
@app.command()
def assess(
    config: str = CONFIG_OPT,
    fmt: str = typer.Option("text", "--format", "-f", help="text | json | html"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write report to a file."),
) -> None:
    """Assess the migration: object inventory, type mapping, cost estimate, target gaps."""
    from .config.store import build_source_adapter, build_type_registry, load_config
    from .assess import render
    from .assess.report import build_report

    cfg = load_config(config)
    src = build_source_adapter(cfg)
    src.connect()
    try:
        schema = src.introspect_schema(cfg.source.schema)
        report = build_report(schema, build_type_registry(cfg))
        renderer = {"text": render.render_text, "json": render.render_json,
                    "html": render.render_html}.get(fmt)
        if renderer is None:
            _fail("unknown format '{}' (text|json|html)".format(fmt))
        text = renderer(report)
        if output:
            with open(output, "w") as f:
                f.write(text)
            console.print("wrote {}".format(output))
        else:
            console.print(text)
    finally:
        src.close()


# --- schema export -----------------------------------------------------------
@app.command()
def export(
    config: str = CONFIG_OPT,
    output: str = typer.Option("schema.sql", "--output", "-o", help="DDL output file."),
) -> None:
    """Export the source schema as HeliosDB DDL (tables, sequences, indexes, FKs, views)."""
    from .config.store import build_source_adapter, build_type_registry, load_config
    from .emit import ddl

    cfg = load_config(config)
    pc = cfg.options.preserve_case
    src = build_source_adapter(cfg)
    src.connect()
    try:
        schema = src.introspect_schema(cfg.source.schema)
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
        for v in schema.views:
            parts.append(ddl.render_view(v, pc))
        for t in schema.tables:
            parts.extend(ddl.render_foreign_keys(t, pc))
        with open(output, "w") as f:
            f.write("\n\n".join(parts) + "\n")
        console.print("wrote {} ({} tables, {} sequences, {} views)".format(
            output, len(schema.tables), len(schema.sequences), len(schema.views)))
    finally:
        src.close()


# --- migrate (schema + data) -------------------------------------------------
@app.command()
def migrate(config: str = CONFIG_OPT) -> None:
    """Run a full schema+data migration end to end (the primary command)."""
    from .config.store import (build_source_adapter, build_target_driver,
                               build_type_registry, load_config)
    from .core.orchestrator import migrate as run_migrate

    cfg = load_config(config)
    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect(); tgt.connect()
    try:
        manifest_path, run_id = _run_context(cfg)
        stats = run_migrate(
            src, tgt, schema=cfg.source.schema, registry=build_type_registry(cfg),
            drop_existing=cfg.options.drop_existing, preserve_case=cfg.options.preserve_case,
            batch_size=cfg.options.batch_size, prefer_copy=cfg.options.prefer_copy,
            cfg=cfg, manifest_path=manifest_path, run_id=run_id, parallelism=cfg.options.parallelism,
        )
        console.print("[green]migrated[/green] {} tables, {} rows (load_mode={})".format(
            stats.tables, stats.total_rows, stats.load_mode))
        for tbl, n in stats.rows.items():
            console.print("  {} -> {}".format(tbl, n))
        for w in stats.warnings:
            console.print("  [yellow]warn:[/yellow] {}".format(w))
        # A partial load must NOT look like success: fail loudly so CI/automation
        # catches it and the user can `a2h status` / `a2h resume`.
        if stats.failed_chunks:
            console.print("[red]error:[/red] {} chunk(s) failed to load — the target is "
                          "INCOMPLETE. Run `a2h status` then `a2h resume`.".format(stats.failed_chunks))
            raise typer.Exit(code=1)
    finally:
        src.close(); tgt.close()


@app.command()
def load(config: str = CONFIG_OPT) -> None:
    """Load data (currently runs the full migrate; data-only resume lands with the data engine)."""
    migrate(config)


# --- validation --------------------------------------------------------------
@app.command(name="test")
def test_(config: str = CONFIG_OPT) -> None:
    """TEST: object-inventory diff (source vs target)."""
    from .config.store import build_source_adapter, build_target_driver, load_config
    from .validate.structure import run_test

    cfg = load_config(config)
    src = build_source_adapter(cfg); tgt = build_target_driver(cfg)
    src.connect(); tgt.connect()
    try:
        res = run_test(src.introspect_schema(cfg.source.schema), tgt, cfg.options.preserve_case)
        _print_validation(res)
        raise typer.Exit(0 if res.passed else 1)
    finally:
        src.close(); tgt.close()


@app.command(name="test-count")
def test_count(config: str = CONFIG_OPT) -> None:
    """TEST_COUNT: row counts on both sides."""
    from .config.store import build_source_adapter, build_target_driver, load_config
    from .validate.counts import run_test_count

    cfg = load_config(config)
    src = build_source_adapter(cfg); tgt = build_target_driver(cfg)
    src.connect(); tgt.connect()
    try:
        schema = src.introspect_schema(cfg.source.schema)
        res = run_test_count(src, tgt, schema.tables, cfg.options.preserve_case)
        _print_validation(res)
        raise typer.Exit(0 if res.passed else 1)
    finally:
        src.close(); tgt.close()


@app.command(name="test-data")
def test_data(
    config: str = CONFIG_OPT,
    sample: int = typer.Option(1000, "--sample", help="Rows per table to compare (0 = all)."),
) -> None:
    """TEST_DATA: ordered, sampled row comparison + checksums."""
    from .config.store import build_source_adapter, build_target_driver, load_config
    from .validate.data import run_test_data

    cfg = load_config(config)
    src = build_source_adapter(cfg); tgt = build_target_driver(cfg)
    src.connect(); tgt.connect()
    failed = False
    try:
        for t in src.introspect_schema(cfg.source.schema).tables:
            res = run_test_data(src, tgt, t, sample_rows=sample, preserve_case=cfg.options.preserve_case)
            _print_validation(res)
            failed = failed or not res.passed
        raise typer.Exit(1 if failed else 0)
    finally:
        src.close(); tgt.close()


@app.command()
def report(config: str = CONFIG_OPT, output: Optional[str] = typer.Option(None, "--output", "-o")) -> None:
    """Render the migration assessment report (alias of `assess`)."""
    assess(config=config, fmt="text", output=output)


# --- CDC (M5 spine) ----------------------------------------------------------
@app.command()
def extract(name: str = typer.Argument(..., help="Extract (capture process) name."),
            config: str = CONFIG_OPT) -> None:
    """Capture source changes into the named trail (v1 SCN-watermark)."""
    from .cdc.engine import run_extract
    from .config.store import load_config

    cfg = load_config(config)
    r = run_extract(cfg, name)
    if r.get("mode") == "binlog":
        label = "binlog since {}".format(r["since"])
    elif r["since"] == 0:
        label = "full snapshot"
    else:
        label = "incremental since SCN {}".format(r["since"])
    console.print("[green]extract {}[/green]: captured {} change(s) ({}); watermark={}".format(
        name, r["captured"], label, r["watermark"]))
    for s in r["skipped"]:
        console.print("  [yellow]skipped {} (no primary key)[/yellow]".format(s))


@app.command()
def replicat(name: str = typer.Argument(..., help="Replicat (apply process) name."),
             config: str = CONFIG_OPT,
             reconcile_deletes: bool = typer.Option(
                 True, "--reconcile-deletes/--no-deletes",
                 help="Reconcile deletes via a source/target key-set diff (v1; off = apply-only).")) -> None:
    """Apply captured changes from the named trail to the target (idempotent)."""
    from .cdc.engine import run_replicat
    from .config.store import load_config

    cfg = load_config(config)
    r = run_replicat(cfg, name, reconcile_deletes=reconcile_deletes)
    console.print("[green]replicat {}[/green]: applied {} change(s), deleted {}, from {} read; cursor={}".format(
        name, r["applied"], r["deleted"], r["read"], r["cursor"]))
    for w in r["warnings"]:
        console.print("  [yellow]warn:[/yellow] {}".format(w))


@app.command()
def extracts(config: str = CONFIG_OPT) -> None:
    """List registered CDC extracts and their capture/apply positions."""
    from .cdc.engine import list_extracts
    from .config.store import load_config

    rows = list_extracts(load_config(config))
    if not rows:
        console.print("no extracts registered")
        return
    for e in rows:
        console.print("  {:16} schema={} tables={} watermark={} cursor={} state={}".format(
            e.name, e.schema, len(e.tables), e.watermark, e.apply_cursor, e.state))


@app.command()
def status(config: str = CONFIG_OPT) -> None:
    """Show progress of the current/last migration run (from the manifest)."""
    from .config.store import load_config
    from .core.manifest import Manifest

    cfg = load_config(config)
    manifest_path, run_id = _run_context(cfg)
    if not os.path.exists(manifest_path):
        _fail("no manifest at {} (run `a2h migrate` first)".format(manifest_path))
    man = Manifest(manifest_path)
    try:
        summary = man.summary(run_id)
        console.print("run {}  states={}  rows_loaded={}".format(
            run_id, summary["chunk_states"], summary["rows_loaded"]))
        for fqn, n in man.rows_by_table(run_id).items():
            console.print("  {} -> {}".format(fqn, n))
        pending = man.pending_chunks(run_id)
        if pending:
            console.print("  [yellow]{} chunk(s) pending/failed[/yellow] — run `a2h resume`".format(len(pending)))
        else:
            console.print("  [green]all chunks loaded[/green]")
    finally:
        man.close()


@app.command()
def monitor(
    config: str = CONFIG_OPT,
    manifest: Optional[str] = typer.Option(
        None, "--manifest", help="Manifest DB path (overrides the one derived from -c)."),
    run_id: Optional[str] = typer.Option(
        None, "--run-id", help="Run id (overrides the one derived from -c)."),
    interval: float = typer.Option(
        1.0, "--interval", help="Refresh interval in seconds (live mode)."),
    once: bool = typer.Option(
        False, "--once", help="Render a single snapshot and exit (non-TTY / CI fallback)."),
) -> None:
    """Live full-screen monitor of an in-progress (or finished) migration run.

    Reads the run manifest READ-ONLY (sqlite WAL allows a concurrent reader), so
    `a2h monitor -c <cfg>` can watch the same run a concurrent
    `a2h migrate -c <cfg>` is writing. Exits 0 once every table is loaded.
    """
    from .monitor import run_monitor

    # Resolve manifest path + run_id. Explicit flags win; otherwise derive them
    # from the config EXACTLY like migrate/status/resume so we attach to the
    # same ledger. --manifest and --run-id must be given together (or neither).
    if manifest or run_id:
        if not (manifest and run_id):
            _fail("--manifest and --run-id must be provided together "
                  "(or use -c to derive both)")
        manifest_path = manifest
    else:
        from .config.store import load_config

        cfg = load_config(config)
        manifest_path, run_id = _run_context(cfg)
    if not os.path.exists(manifest_path):
        _fail("no manifest at {} (run `a2h migrate` first)".format(manifest_path))
    code = run_monitor(manifest_path, run_id, interval=interval, once=once, console=console)
    raise typer.Exit(code)


@app.command()
def resume(config: str = CONFIG_OPT) -> None:
    """Resume an interrupted migration run from its manifest (continues the data load)."""
    from .config.store import (build_source_adapter, build_target_driver,
                               build_type_registry, load_config)
    from .core.orchestrator import migrate as run_migrate

    cfg = load_config(config)
    manifest_path, run_id = _run_context(cfg)
    if not os.path.exists(manifest_path):
        _fail("no manifest to resume at {} (run `a2h migrate` first)".format(manifest_path))
    src = build_source_adapter(cfg)
    tgt = build_target_driver(cfg)
    src.connect(); tgt.connect()
    try:
        stats = run_migrate(
            src, tgt, schema=cfg.source.schema, registry=build_type_registry(cfg),
            drop_existing=False, preserve_case=cfg.options.preserve_case,
            prefer_copy=cfg.options.prefer_copy, cfg=cfg, manifest_path=manifest_path,
            run_id=run_id, parallelism=cfg.options.parallelism, do_schema=False,
        )
        console.print("[green]resumed[/green]: {} rows across {} tables".format(
            stats.total_rows, stats.tables))
        for w in stats.warnings:
            console.print("  [yellow]warn:[/yellow] {}".format(w))
        # Like `migrate`: a resume that still has failed chunks must exit nonzero,
        # or automation reads a persistently-incomplete target as success.
        if stats.failed_chunks:
            console.print("[red]error:[/red] {} chunk(s) still failed after resume — the "
                          "target is INCOMPLETE. Check `a2h status` and resume again "
                          "once the cause is fixed.".format(stats.failed_chunks))
            raise typer.Exit(code=1)
    finally:
        src.close(); tgt.close()


# --- MCP server (AI-agent remote administration) -----------------------------
mcp_app = typer.Typer(no_args_is_help=True, help="Run the MCP server so AI agents "
                      "can administer a2h remotely (Bearer auth + RBAC).")
app.add_typer(mcp_app, name="mcp")


@mcp_app.command("serve")
def mcp_serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (HTTP transport)."),
    port: int = typer.Option(8080, "--port", help="Bind port (HTTP transport)."),
    transport: str = typer.Option("http", "--transport", help="http | stdio."),
    tokens: Optional[str] = typer.Option(
        None, "--tokens", help="token:role[,token:role...] (else $A2H_MCP_TOKENS)."),
    tokens_file: Optional[str] = typer.Option(
        None, "--tokens-file", help="Path to a token:role file (else $A2H_MCP_TOKENS_FILE)."),
    stdio_role: str = typer.Option(
        "admin", "--stdio-role", help="Role granted to the local stdio caller."),
) -> None:
    """Start the MCP server exposing a2h tools to AI agents over Bearer-auth HTTP (or stdio)."""
    from .mcp.server import serve

    if transport == "http":
        console.print("[bold cyan]Any2HeliosDB MCP server[/bold cyan] (http) on "
                      "http://{}:{}/mcp".format(host, port))
    elif transport == "stdio":
        # stdout is the JSON-RPC channel on stdio: the banner MUST go to stderr.
        Console(stderr=True).print(
            "[bold cyan]Any2HeliosDB MCP server[/bold cyan] (stdio, role={})".format(stdio_role))
    try:
        serve(transport=transport, host=host, port=port, tokens=tokens,
              tokens_file=tokens_file, stdio_role=stdio_role)
    except KeyboardInterrupt:  # pragma: no cover
        console.print("\nstopped")


def main() -> None:
    """Console-script entrypoint: render tool errors cleanly instead of tracebacks."""
    from .errors import Any2HeliosError

    try:
        app()
    except Any2HeliosError as e:
        console.print("[red]error:[/red] {}".format(e))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
