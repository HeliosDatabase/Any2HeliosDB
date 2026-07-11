"""MCP parity for the tier-2 CDC operator verbs (interface-coverage gate).

Every new CLI verb/flag must be reachable over MCP too. These assert the tool
schemas advertise the new args and that the ``extract`` handler dispatches the
lifecycle verbs to the right engine function.
"""
from __future__ import annotations


from any2heliosdb.mcp.tools import build_catalog


def _tool(name):
    return build_catalog().get(name)


def test_extract_tool_exposes_lifecycle_args():
    props = _tool("extract").input_schema()["properties"]
    for key in ("name", "refresh_tables", "drop", "purge_trail", "purge_applied"):
        assert key in props, key


def test_extracts_tool_exposes_lag_arg():
    props = _tool("extracts").input_schema()["properties"]
    assert "lag" in props


def test_config_tools_accept_inline_cdc_block():
    # CLI/MCP parity for the [cdc] tuning knobs (incl. poison_max_per_run): the
    # inline config block must accept a `cdc` object and thread it into the config.
    from any2heliosdb.mcp.tools import _CONFIG_PROP, _load_cfg
    assert "cdc" in _CONFIG_PROP
    cfg = _load_cfg({
        "source": {"dialect": "postgresql", "host": "h", "port": 5432,
                   "database": "hr", "schema": "public", "user": "u", "password": "p"},
        "target": {"driver": "psycopg"},
        "cdc": {"poison_max_per_run": 7, "poison_retries": 1},
    })
    assert cfg.cdc.poison_max_per_run == 7 and cfg.cdc.poison_retries == 1


def _inline_args(tmp_path, **extra):
    args = {
        "source": {"dialect": "postgresql", "host": "h", "port": 5432,
                   "database": "hr", "schema": "public", "user": "u", "password": "p"},
        "target": {"driver": "psycopg"},
        "options": {"output_dir": str(tmp_path)},
        "name": "e1",
    }
    args.update(extra)
    return args


def test_h_extract_dispatches_drop(tmp_path, monkeypatch):
    from any2heliosdb.cdc import engine
    from any2heliosdb.mcp import tools
    called = {}

    def _fake(cfg, name, purge_trail=False):
        called["drop"] = (name, purge_trail)
        return {}

    monkeypatch.setattr(engine, "drop_extract", _fake)
    r = tools._h_extract(_inline_args(tmp_path, drop=True, purge_trail=True))
    assert called["drop"] == ("e1", True) and r["ok"] is True


def test_h_extract_dispatches_purge_applied(tmp_path, monkeypatch):
    from any2heliosdb.cdc import engine
    from any2heliosdb.mcp import tools
    called = {}

    def _fake(cfg, name):
        called["purge"] = name
        return {"purged_segments": 0}

    monkeypatch.setattr(engine, "purge_applied_segments", _fake)
    tools._h_extract(_inline_args(tmp_path, purge_applied=True))
    assert called["purge"] == "e1"


def test_h_extract_dispatches_refresh_tables(tmp_path, monkeypatch):
    from any2heliosdb.cdc import engine
    from any2heliosdb.mcp import tools
    called = {}

    def _fake(cfg, name, refresh_tables=False):
        called["run"] = (name, refresh_tables)
        return {"captured": 0}

    monkeypatch.setattr(engine, "run_extract", _fake)
    tools._h_extract(_inline_args(tmp_path, refresh_tables=True))
    assert called["run"] == ("e1", True)


def test_h_extracts_reports_dead_letters(tmp_path):
    from any2heliosdb.cdc.deadletter import DeadLetter
    from any2heliosdb.cdc.engine import _registry_path, _trail_dir
    from any2heliosdb.cdc.registry import CdcRegistry
    from any2heliosdb.config.model import Options, ProjectConfig, SourceConfig, TargetConfig
    from any2heliosdb.constants import SourceDialect, TargetDriverKind
    from any2heliosdb.core.change_record import INSERT, ChangeRecord
    from any2heliosdb.mcp import tools

    cfg = ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.POSTGRESQL, host="h", port=5432,
                            database="hr", schema="public", user="u", password="p"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG),
        options=Options(output_dir=str(tmp_path)))
    reg = CdcRegistry(_registry_path(cfg))
    reg.register("e1", "public", ["t"])
    reg.close()
    DeadLetter(_trail_dir(cfg, "e1")).append(
        ChangeRecord(op=INSERT, schema="public", table="t", key={"id": 1}, after={"id": 1},
                     source_pos=1), "boom")

    res = tools._h_extracts(_inline_args(tmp_path))
    assert res["ok"] and res["extracts"][0]["dead_letters"] == 1
    assert "lag" not in res["extracts"][0]     # lag off by default
