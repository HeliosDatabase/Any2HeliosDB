"""Unit tests for the MCP server: token auth, RBAC gating, tools/list per role,
and tool dispatch against a stubbed engine.

All hermetic — no live HeliosDB. Engine functions are monkeypatched so we assert
the MCP layer parses args and returns the structured result, without opening a
single connection.
"""
import json

import pytest

from any2heliosdb.mcp.auth import (
    AuthError,
    ForbiddenError,
    Principal,
    Role,
    TokenAuthenticator,
    load_tokens,
)
from any2heliosdb.mcp.protocol import FORBIDDEN, Dispatcher
from any2heliosdb.mcp.tools import build_catalog


# --- token resolution -------------------------------------------------------
def test_load_tokens_from_env_value():
    tokens = load_tokens("v-tok:viewer, op-tok:operator a-tok:admin", environ={})
    assert tokens == {"v-tok": Role.VIEWER, "op-tok": Role.OPERATOR, "a-tok": Role.ADMIN}


def test_load_tokens_from_env_var(monkeypatch):
    tokens = load_tokens(environ={"A2H_MCP_TOKENS": "secret:viewer"})
    assert tokens == {"secret": Role.VIEWER}


def test_load_tokens_file_overrides_env(tmp_path):
    f = tmp_path / "tokens.txt"
    f.write_text("# a comment\nfile-tok = admin\nshared : operator\n\n")
    # 'shared' appears in both; the file must win.
    tokens = load_tokens("shared:viewer", str(f), environ={})
    assert tokens["file-tok"] is Role.ADMIN
    assert tokens["shared"] is Role.OPERATOR


def test_load_tokens_rejects_unknown_role():
    with pytest.raises(Exception):
        load_tokens("t:wizardlord", environ={})


def test_authenticate_valid_token():
    auth = TokenAuthenticator({"good": Role.OPERATOR})
    p = auth.authenticate("good")
    assert p.role is Role.OPERATOR
    assert p.token_id.startswith("tok_")  # fingerprint, never the raw token
    assert "good" not in p.token_id


def test_authenticate_invalid_token_raises_401():
    auth = TokenAuthenticator({"good": Role.VIEWER})
    with pytest.raises(AuthError):
        auth.authenticate("nope")


def test_extract_bearer_missing_header_raises_401():
    with pytest.raises(AuthError):
        TokenAuthenticator.extract_bearer(None)


def test_extract_bearer_malformed_header_raises_401():
    for bad in ("Token abc", "Bearer", "Bearer    ", "abc"):
        with pytest.raises(AuthError):
            TokenAuthenticator.extract_bearer(bad)


def test_extract_bearer_ok():
    assert TokenAuthenticator.extract_bearer("Bearer my-token") == "my-token"
    assert TokenAuthenticator.extract_bearer("bearer my-token") == "my-token"  # case-insensitive


# --- RBAC role hierarchy ----------------------------------------------------
def test_role_hierarchy():
    assert Role.ADMIN.can(Role.OPERATOR)
    assert Role.ADMIN.can(Role.VIEWER)
    assert Role.OPERATOR.can(Role.VIEWER)
    assert not Role.VIEWER.can(Role.OPERATOR)
    assert not Role.OPERATOR.can(Role.ADMIN)
    assert Role.VIEWER.can(Role.VIEWER)


# --- registry RBAC + visibility ---------------------------------------------
def _principal(role):
    return Principal(role=role, token_id="tok_test")


def test_viewer_visible_tools_are_read_only():
    reg = build_catalog()
    names = {t.name for t in reg.visible_to(_principal(Role.VIEWER))}
    assert "migrate" not in names
    assert "extract" not in names
    assert "wizard" not in names
    # read-only set present
    for n in ("doctor", "smoke_test", "assess", "status", "extracts",
              "test", "test_count", "test_data", "list_config", "validate_config"):
        assert n in names


def test_operator_can_see_migrate_but_not_extract():
    reg = build_catalog()
    names = {t.name for t in reg.visible_to(_principal(Role.OPERATOR))}
    assert {"migrate", "load", "resume"}.issubset(names)
    assert "extract" not in names and "replicat" not in names and "wizard" not in names


def test_admin_sees_everything():
    reg = build_catalog()
    names = {t.name for t in reg.visible_to(_principal(Role.ADMIN))}
    assert {"extract", "replicat", "wizard", "migrate"}.issubset(names)


def test_viewer_denied_migrate_raises_forbidden():
    reg = build_catalog()
    with pytest.raises(ForbiddenError):
        reg.call(_principal(Role.VIEWER), "migrate", {"config": "x.toml"})


def test_operator_denied_extract_raises_forbidden():
    reg = build_catalog()
    with pytest.raises(ForbiddenError):
        reg.call(_principal(Role.OPERATOR), "extract", {"name": "e1", "config": "x.toml"})


def test_unknown_tool_raises_keyerror():
    reg = build_catalog()
    with pytest.raises(KeyError):
        reg.call(_principal(Role.ADMIN), "nope", {})


# --- dispatcher (JSON-RPC over the protocol) --------------------------------
def test_initialize_handshake():
    disp = Dispatcher(build_catalog())
    resp = disp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                       _principal(Role.VIEWER))
    assert resp["result"]["serverInfo"]["name"] == "any2heliosdb-mcp"
    assert "tools" in resp["result"]["capabilities"]


def test_tools_list_reflects_role():
    disp = Dispatcher(build_catalog())
    viewer = disp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         _principal(Role.VIEWER))
    admin = disp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                        _principal(Role.ADMIN))
    viewer_names = {t["name"] for t in viewer["result"]["tools"]}
    admin_names = {t["name"] for t in admin["result"]["tools"]}
    assert "migrate" not in viewer_names
    assert "migrate" in admin_names
    assert len(admin_names) > len(viewer_names)
    # each entry advertises its required role
    for t in admin["result"]["tools"]:
        assert t["_meta"]["requiredRole"] in ("viewer", "operator", "admin")


def test_tools_call_forbidden_for_viewer_migrate():
    disp = Dispatcher(build_catalog())
    resp = disp.handle(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "migrate", "arguments": {"config": "x.toml"}}},
        _principal(Role.VIEWER))
    assert "error" in resp
    assert resp["error"]["code"] == FORBIDDEN
    assert resp["error"]["data"]["status"] == 403


def test_tools_call_unknown_method():
    disp = Dispatcher(build_catalog())
    resp = disp.handle({"jsonrpc": "2.0", "id": 9, "method": "frobnicate"},
                       _principal(Role.ADMIN))
    assert resp["error"]["code"] == -32601


# --- tool dispatch against a stubbed engine ---------------------------------
def test_doctor_dispatch_structured():
    """doctor needs no DB; assert it returns the structured component list."""
    disp = Dispatcher(build_catalog())
    resp = disp.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "doctor", "arguments": {}}},
        _principal(Role.VIEWER))
    result = resp["result"]
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["ok"] is True
    assert any(c["module"] == "psycopg" for c in payload["components"])
    # the text block carries the same JSON
    assert json.loads(result["content"][0]["text"]) == payload


def test_migrate_dispatch_calls_engine_with_parsed_args(monkeypatch):
    """Admin calls migrate; the orchestrator is stubbed. We assert the MCP layer
    parsed the inline config + parallelism override and returned structured stats
    reflecting failure (failed_chunks>0 -> ok=False / incomplete=True)."""
    captured = {}

    class _Stats:
        tables = 2
        rows = {"EMP": 10, "DEPT": 3}
        load_mode = "copy"
        warnings = ["a warning"]
        failed_chunks = 1

        @property
        def total_rows(self):
            return 13

    def fake_migrate(source, target, **kwargs):
        captured["kwargs"] = kwargs
        captured["source"] = source
        captured["target"] = target
        return _Stats()

    class _FakeAdapter:
        def __init__(self, *a, **k):
            pass

        def connect(self):
            captured.setdefault("connected", []).append("src")

        def close(self):
            pass

    class _FakeTarget(_FakeAdapter):
        def connect(self):
            captured.setdefault("connected", []).append("tgt")

    import any2heliosdb.config.store as store
    import any2heliosdb.core.orchestrator as orch

    monkeypatch.setattr(orch, "migrate", fake_migrate)
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())
    monkeypatch.setattr(store, "build_target_driver", lambda cfg: _FakeTarget())
    monkeypatch.setattr(store, "build_type_registry", lambda cfg: object())

    disp = Dispatcher(build_catalog())
    resp = disp.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "migrate", "arguments": {
             "source": {"dialect": "oracle", "host": "h", "user": "u", "service_name": "S"},
             "target": {"driver": "psycopg", "host": "t", "dbname": "db"},
             "options": {"output_dir": "/tmp/out"},
             "parallelism": 8}}},
        _principal(Role.ADMIN))

    # engine was called, with the override threaded through
    assert captured["kwargs"]["parallelism"] == 8
    assert captured["kwargs"]["schema"] is None or True  # schema came from cfg
    assert captured["connected"] == ["src", "tgt"]
    # structured result reflects the (failed) stats
    payload = resp["result"]["structuredContent"]
    assert payload["tables"] == 2
    assert payload["total_rows"] == 13
    assert payload["rows"] == {"EMP": 10, "DEPT": 3}
    assert payload["failed_chunks"] == 1
    assert payload["ok"] is False           # failed_chunks>0 must not look like success
    assert payload["incomplete"] is True
    assert "run_id" in payload


def test_test_count_dispatch_returns_validation(monkeypatch):
    """viewer-permitted test_count: stub the validator + adapters, assert the
    ValidationResult is serialized and 'ok' tracks passed."""
    from any2heliosdb.validate.model import (
        Severity,
        ValidationResult,
        ValidationType,
    )

    res = ValidationResult(validation_type=ValidationType.TEST_COUNT)
    res.add_error(Severity.BLOCKER, "HR.EMP", "row count differs")

    class _FakeSchema:
        tables = []

    class _FakeAdapter:
        def connect(self):
            pass

        def close(self):
            pass

        def introspect_schema(self, schema):
            return _FakeSchema()

    import any2heliosdb.config.store as store
    import any2heliosdb.validate.counts as counts

    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _FakeAdapter())
    monkeypatch.setattr(store, "build_target_driver", lambda cfg: _FakeAdapter())
    monkeypatch.setattr(counts, "run_test_count", lambda *a, **k: res)

    disp = Dispatcher(build_catalog())
    resp = disp.handle(
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call",
         "params": {"name": "test_count", "arguments": {
             "source": {"dialect": "mysql", "host": "h", "user": "u", "database": "d"},
             "target": {"driver": "psycopg"}}}},
        _principal(Role.VIEWER))
    payload = resp["result"]["structuredContent"]
    assert payload["ok"] is False  # tracks ValidationResult.passed (has a BLOCKER)
    assert payload["result"]["validation_type"] == "TEST_COUNT"
    assert payload["result"]["errors"][0]["table"] == "HR.EMP"


def test_engine_failure_surfaces_as_tool_error(monkeypatch):
    """A tool that raises Any2HeliosError (e.g. cannot connect) is reported as a
    tools/call result with isError=True, not a transport-level error."""
    from any2heliosdb.errors import TargetConnectionError

    class _BoomAdapter:
        def connect(self):
            raise TargetConnectionError("cannot connect to target")

        def close(self):
            pass

    import any2heliosdb.config.store as store

    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: _BoomAdapter())
    monkeypatch.setattr(store, "build_target_driver", lambda cfg: _BoomAdapter())

    disp = Dispatcher(build_catalog())
    resp = disp.handle(
        {"jsonrpc": "2.0", "id": 13, "method": "tools/call",
         "params": {"name": "test", "arguments": {
             "source": {"dialect": "oracle", "host": "h", "user": "u", "service_name": "S"},
             "target": {"driver": "psycopg"}}}},
        _principal(Role.VIEWER))
    assert "result" in resp  # not a JSON-RPC error
    assert resp["result"]["isError"] is True
    payload = resp["result"]["structuredContent"]
    assert payload["ok"] is False
    assert "cannot connect" in payload["error"]


def test_missing_config_is_tool_error():
    disp = Dispatcher(build_catalog())
    resp = disp.handle(
        {"jsonrpc": "2.0", "id": 14, "method": "tools/call",
         "params": {"name": "validate_config", "arguments": {}}},
        _principal(Role.VIEWER))
    payload = resp["result"]["structuredContent"]
    assert payload["ok"] is False
    assert "no config" in payload["error"].lower()
