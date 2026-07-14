"""F4 — a failing SECOND connect must not leak the already-open FIRST connection.

Every two-connection command opens the source, then the target. If the target
connect raises, the source is already open but the caller's ``try/finally`` (which
closes both) has not been entered yet — so without a guard the source socket
leaks. ``connect_both`` centralizes the guard; these tests prove it, and prove a
representative call site on each swept surface (CLI, MCP, the CDC engine) uses it.

All hermetic: fake adapters count connect/close; no server is opened.
"""
from __future__ import annotations

import pytest


class _Src:
    def __init__(self):
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True


class _TgtFailConnect:
    """A target whose connect() raises (source already open at that point)."""

    def __init__(self):
        self.closed = False

    def connect(self):
        raise RuntimeError("target down")

    def close(self):
        self.closed = True


# --- the helper itself -------------------------------------------------------
def test_connect_both_success_opens_both_and_closes_neither():
    from any2heliosdb.config.store import connect_both

    src, tgt = _Src(), _Src()
    connect_both(src, tgt)
    assert src.connected and tgt.connected
    assert not src.closed and not tgt.closed


def test_connect_both_closes_first_when_second_connect_fails():
    from any2heliosdb.config.store import connect_both

    src, tgt = _Src(), _TgtFailConnect()
    with pytest.raises(RuntimeError, match="target down"):
        connect_both(src, tgt)
    assert src.connected and src.closed          # opened, then closed on the failed 2nd connect


def test_connect_both_reraises_connect_error_even_if_first_close_raises():
    from any2heliosdb.config.store import connect_both

    class _SrcBadClose(_Src):
        def close(self):
            raise ValueError("close boom")

    with pytest.raises(RuntimeError, match="target down"):   # the real connect error, not close boom
        connect_both(_SrcBadClose(), _TgtFailConnect())


# --- surface: cli.py (representative: the `test` command) --------------------
def test_cli_command_closes_source_when_target_connect_fails(monkeypatch):
    from any2heliosdb import cli
    from any2heliosdb.config import store
    from any2heliosdb.config.model import ProjectConfig

    src = _Src()
    monkeypatch.setattr(store, "load_config", lambda p: ProjectConfig())
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: src)
    monkeypatch.setattr(store, "build_target_driver", lambda cfg: _TgtFailConnect())
    with pytest.raises(RuntimeError, match="target down"):
        cli.test_(config="ignored")
    assert src.connected and src.closed


# --- surface: mcp/tools.py (representative: the `test_data` handler) ---------
def test_mcp_handler_closes_source_when_target_connect_fails(monkeypatch):
    from any2heliosdb.config import store
    from any2heliosdb.mcp import tools

    src = _Src()
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: src)
    monkeypatch.setattr(store, "build_target_driver", lambda cfg: _TgtFailConnect())
    args = {"source": {"dialect": "mysql", "host": "h", "database": "d", "user": "u"},
            "target": {"driver": "psycopg", "host": "t", "dbname": "db"}}
    with pytest.raises(RuntimeError, match="target down"):
        tools._h_test_data(args)
    assert src.connected and src.closed


# --- surface: cdc/engine.py (run_replicat: target connect fails) ------------
def _cdc_cfg(tmp_path):
    from any2heliosdb.config.model import (Options, ProjectConfig, SourceConfig,
                                           TargetConfig)
    from any2heliosdb.constants import SourceDialect, TargetDriverKind

    return ProjectConfig(
        source=SourceConfig(dialect=SourceDialect.POSTGRESQL, host="h", port=5432,
                            database="hr", schema="hr", user="u", password="p"),
        target=TargetConfig(driver=TargetDriverKind.PSYCOPG),
        options=Options(output_dir=str(tmp_path)))


def test_run_replicat_closes_source_when_target_connect_fails(tmp_path, monkeypatch):
    from any2heliosdb.cdc.engine import _registry_path, run_replicat
    from any2heliosdb.cdc.registry import CdcRegistry
    from any2heliosdb.config import store

    cfg = _cdc_cfg(tmp_path)
    reg = CdcRegistry(_registry_path(cfg))
    reg.register("e1", "hr", ["t"])
    reg.close()

    src = _Src()
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: src)
    monkeypatch.setattr(store, "build_target_driver", lambda cfg: _TgtFailConnect())
    with pytest.raises(RuntimeError, match="target down"):
        run_replicat(cfg, "e1")
    assert src.connected and src.closed


# --- surface: cdc/engine.py (run_extract: source connect fails leaks reg) ----
def test_run_extract_closes_registry_when_source_connect_fails(tmp_path, monkeypatch):
    from any2heliosdb.cdc import engine
    from any2heliosdb.cdc.registry import CdcRegistry
    from any2heliosdb.config import store

    cfg = _cdc_cfg(tmp_path)
    # cfg is POSTGRESQL above; run_extract opens the registry (SQLite) BEFORE the
    # source connect, so a source-connect failure must still close the registry.
    closes = []
    orig_close = CdcRegistry.close

    def spy(self):
        closes.append(1)
        return orig_close(self)

    monkeypatch.setattr(CdcRegistry, "close", spy)

    class _SrcFailConnect:
        def connect(self):
            raise RuntimeError("source down")

        def close(self):
            pass

    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _SrcFailConnect())
    with pytest.raises(RuntimeError, match="source down"):
        engine.run_extract(cfg, "e1")
    assert closes  # the registry was closed despite the source connect failing


def test_run_extract_closes_registry_when_source_BUILDER_fails(tmp_path, monkeypatch):
    # The builder (build_source_adapter) runs AFTER the registry opens; a builder
    # failure (e.g. a missing optional driver) must also close the registry, not
    # only a connect failure.
    from any2heliosdb.cdc import engine
    from any2heliosdb.cdc.registry import CdcRegistry
    from any2heliosdb.config import store

    cfg = _cdc_cfg(tmp_path)
    closes = []
    orig_close = CdcRegistry.close
    monkeypatch.setattr(CdcRegistry, "close",
                        lambda self: (closes.append(1), orig_close(self))[1])

    def _raise_builder(cfg):
        raise RuntimeError("no driver installed")

    monkeypatch.setattr(store, "build_source_adapter", _raise_builder)
    with pytest.raises(RuntimeError, match="no driver installed"):
        engine.run_extract(cfg, "e1")
    assert closes  # registry closed despite the builder raising before connect
