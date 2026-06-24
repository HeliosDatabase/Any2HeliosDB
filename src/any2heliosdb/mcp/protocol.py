"""A tiny, transport-agnostic MCP / JSON-RPC-2.0 dispatcher.

This is the portable core the HTTP and stdio servers share. It implements just
enough of the Model Context Protocol for an agent to use the tools:

* ``initialize``      — handshake + capability advertisement
* ``tools/list``      — the tools the *authenticated caller's role* may call
* ``tools/call``      — RBAC-checked dispatch, structured-JSON result
* ``ping``            — liveness

The official ``mcp`` Python SDK (FastMCP) requires Python >= 3.10; this runtime
is 3.9, so we ship this minimal JSON-RPC-2.0-over-HTTP/stdio endpoint instead
(the contract is identical on the wire for these methods).

Authentication is layered *outside* this dispatcher (the HTTP server checks the
Bearer header per request); the dispatcher receives an already-resolved
:class:`~any2heliosdb.mcp.auth.Principal`.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from ..errors import Any2HeliosError
from .auth import ForbiddenError, Principal
from .tools import ToolRegistry

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "any2heliosdb-mcp"

# JSON-RPC 2.0 error codes (negative reserved range + MCP conventions).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# MCP/app convention: 403 forbidden surfaces as a tools/call application error.
FORBIDDEN = -32001


def _result(rpc_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(rpc_id: Any, code: int, message: str,
           data: Optional[Any] = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": rpc_id, "error": err}


def _tool_result(content_obj: Dict[str, Any], is_error: bool = False) -> Dict[str, Any]:
    """Wrap a structured dict as an MCP ``tools/call`` result.

    Per spec the result carries a ``content`` array; we put the JSON in a text
    block (the universal shape) and also surface it as ``structuredContent`` so
    structured-output-aware clients can consume it directly.
    """
    text = json.dumps(content_obj, default=str)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": content_obj,
        "isError": is_error,
    }


class Dispatcher:
    """Routes a single JSON-RPC request for an authenticated principal."""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry

    def handle(self, message: Dict[str, Any], principal: Principal) -> Optional[Dict[str, Any]]:
        """Handle one parsed JSON-RPC message. Returns the response dict, or
        ``None`` for a notification (no ``id``) that needs no reply."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(message.get("id") if isinstance(message, dict) else None,
                          INVALID_REQUEST, "not a JSON-RPC 2.0 request")
        rpc_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        is_notification = "id" not in message

        if method == "initialize":
            resp = _result(rpc_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": _server_version()},
            })
            return None if is_notification else resp

        if method in ("notifications/initialized", "initialized"):
            return None  # client handshake ack; nothing to return

        if method == "ping":
            return None if is_notification else _result(rpc_id, {})

        if method == "tools/list":
            return _result(rpc_id, {"tools": self.registry.list_meta(principal)})

        if method == "tools/call":
            return self._call(rpc_id, params, principal)

        if is_notification:
            return None
        return _error(rpc_id, METHOD_NOT_FOUND, "unknown method: {}".format(method))

    def _call(self, rpc_id: Any, params: Dict[str, Any],
              principal: Principal) -> Dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not name:
            return _error(rpc_id, INVALID_PARAMS, "tools/call requires a 'name'")
        if not isinstance(arguments, dict):
            return _error(rpc_id, INVALID_PARAMS, "'arguments' must be an object")
        try:
            result = self.registry.call(principal, name, arguments)
        except ForbiddenError as e:
            # RBAC denial: a protocol-level error (403-equivalent) so the agent
            # can distinguish "not allowed" from "tool ran and failed".
            return _error(rpc_id, FORBIDDEN, str(e), data={"status": 403})
        except KeyError as e:
            return _error(rpc_id, METHOD_NOT_FOUND, str(e).strip("'"))
        except Any2HeliosError as e:
            # The tool ran but failed (e.g. could not connect). Report as a
            # tools/call result with isError=True, not a transport error.
            return _result(rpc_id, _tool_result(
                {"ok": False, "error": str(e), "error_type": type(e).__name__},
                is_error=True))
        except Exception as e:  # noqa: BLE001
            return _result(rpc_id, _tool_result(
                {"ok": False, "error": str(e), "error_type": type(e).__name__},
                is_error=True))
        return _result(rpc_id, _tool_result(result))


def _server_version() -> str:
    from .. import __version__

    return __version__
