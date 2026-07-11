"""Interactive setup wizard + connection smoke test (replaces hand-edited config).

``run_wizard`` collects source + target details, runs ``smoke_test`` (connect
both ends, detect edition, probe capabilities, round-trip a tiny COPY to prove
NULL-vs-empty-string fidelity), and writes ``config.toml``. ``smoke_test`` is
factored out so it is unit/integration testable without the prompts.
"""
from __future__ import annotations

from typing import Dict

from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt

from ..constants import SourceDialect, TargetDriverKind
from .model import Options, ProjectConfig, SourceConfig, TargetConfig
from .store import build_source_adapter, build_target_driver, save_config

console = Console()


def smoke_test(cfg: ProjectConfig) -> Dict[str, object]:
    """Connect both ends, probe the target, and round-trip a tiny COPY.

    Returns a report dict; raises on a hard failure (e.g. cannot connect).
    """
    report: Dict[str, object] = {}
    src = build_source_adapter(cfg)
    src.connect()
    try:
        report["source_version"] = src.server_version()
        report["source_schema"] = src.default_schema()
    finally:
        src.close()

    tgt = build_target_driver(cfg)
    tgt.connect()
    try:
        banner = tgt.server_banner()
        caps = tgt.probe_capabilities()
        report["target_banner"] = banner
        report["target_edition"] = caps.edition.value
        report["copy_from_stdin"] = caps.copy_from_stdin
        report["enforces_check"] = caps.enforces_check
        report["enforces_fk"] = caps.enforces_fk
        # NULL vs empty-string fidelity round-trip
        tgt.execute("DROP TABLE IF EXISTS _a2h_smoke")
        tgt.execute("CREATE TABLE _a2h_smoke (n int, s text)")
        if caps.copy_from_stdin:
            tgt.copy_rows("_a2h_smoke", ["n", "s"], [(1, ""), (2, None)])
        else:
            tgt.insert_rows("_a2h_smoke", ["n", "s"], [(1, ""), (2, None)])
        rows = tgt.query("SELECT n, s IS NULL AS s_null, s FROM _a2h_smoke ORDER BY n")
        # row for n=1 must have '' (not NULL); n=2 must be NULL. A round-trip
        # that LOSES a row is itself a fidelity failure — report False, never
        # crash with IndexError on rows[1].
        fidelity_ok = (len(rows) >= 2 and (rows[0][1] is False)
                       and (rows[1][1] is True))
        report["null_empty_fidelity"] = fidelity_ok
        tgt.execute("DROP TABLE IF EXISTS _a2h_smoke")
    finally:
        tgt.close()
    return report


def _prompt_source() -> SourceConfig:
    console.print("[bold]Source database[/bold]")
    dialect = SourceDialect(Prompt.ask("dialect", choices=[d.value for d in
                            (SourceDialect.ORACLE, SourceDialect.MYSQL, SourceDialect.MSSQL)],
                            default="oracle"))
    host = Prompt.ask("host", default="127.0.0.1")
    default_port = 1521 if dialect is SourceDialect.ORACLE else (3306 if dialect is SourceDialect.MYSQL else 1433)
    port = IntPrompt.ask("port", default=default_port)
    user = Prompt.ask("user")
    password_env = Prompt.ask("password env var (recommended; leave blank to enter literal)", default="")
    password = None
    if not password_env:
        password = Prompt.ask("password (dev only)", password=True, default="")
    sc = SourceConfig(dialect=dialect, host=host, port=port, user=user,
                      password_env=password_env or None, password=password or None)
    if dialect is SourceDialect.ORACLE:
        sc.service_name = Prompt.ask("service_name", default="XEPDB1")
    else:
        sc.database = Prompt.ask("database")
    sc.schema = Prompt.ask("schema to migrate (blank = the user's own)", default="") or None
    return sc


def _prompt_target() -> TargetConfig:
    console.print("[bold]Target[/bold] (HeliosDB Nano/Lite/Full, or a stock PostgreSQL "
                  "server — both speak the psycopg PG-wire driver)")
    driver = TargetDriverKind(Prompt.ask("driver", choices=["psycopg", "native"], default="psycopg"))
    host = Prompt.ask("host", default="127.0.0.1")
    port = IntPrompt.ask("port", default=5432)
    dbname = Prompt.ask("database", default="postgres")
    user = Prompt.ask("user", default="postgres")
    password_env = Prompt.ask("password env var (blank for trust)", default="")
    return TargetConfig(driver=driver, host=host, port=port, dbname=dbname, user=user,
                        password_env=password_env or None)


def run_wizard(config_path: str = "config.toml") -> None:
    console.print("[bold cyan]Any2HeliosDB setup wizard[/bold cyan]")
    cfg = ProjectConfig(source=_prompt_source(), target=_prompt_target(), options=Options())

    console.print("\n[bold]Running smoke test...[/bold]")
    try:
        report = smoke_test(cfg)
    except Exception as e:  # noqa: BLE001
        console.print("[red]smoke test failed:[/red] {}".format(e))
        if not Confirm.ask("Save the config anyway?", default=False):
            return
    else:
        for k, v in report.items():
            mark = ""
            if k in ("null_empty_fidelity", "copy_from_stdin") and v is True:
                mark = " [green]OK[/green]"
            console.print("  {:22} {}{}".format(k, v, mark))
        cfg.capability = {k: v for k, v in report.items() if k.startswith(("copy", "enforces", "target_edition"))}

    save_config(cfg, config_path)
    console.print("\n[green]wrote {}[/green]. Next: [bold]a2h assess[/bold] then [bold]a2h migrate[/bold].".format(config_path))
