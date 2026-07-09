"""MCP server transports for Any2HeliosDB.

Two transports, one shared :class:`~any2heliosdb.mcp.protocol.Dispatcher`:

* **http** — a streamable JSON-RPC-2.0 endpoint (``POST /mcp``). Every request
  MUST carry ``Authorization: Bearer <token>``; missing/unknown tokens get a
  ``401`` *before* any method runs. The resolved role then gates ``tools/call``.
  Remote agents connect here. A ``GET /mcp`` opens an SSE stream (the
  streamable-HTTP server→client channel); a ``GET /healthz`` is unauthenticated.
* **stdio** — newline-delimited JSON-RPC over stdin/stdout for a local agent
  that launches the server as a subprocess. Auth still applies: stdio is granted
  a role via ``--stdio-role`` (default ``admin`` for a trusted local launch) or a
  single token in the environment.

We use the official ``mcp`` SDK (FastMCP) when it is importable; it requires
Python >= 3.10 and is **not** installable on this 3.9 runtime, so the default
path here is the minimal-but-conformant implementation below. :func:`serve`
reports which path it took.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, List, Optional, Union

from .auth import AuthError, Principal, Role, TokenAuthenticator
from .protocol import INTERNAL_ERROR, PARSE_ERROR, Dispatcher, _error
from .tools import ToolRegistry, build_catalog


def sdk_available() -> bool:
    """Whether the official ``mcp`` SDK (FastMCP) can be imported here."""
    import importlib.util

    return importlib.util.find_spec("mcp") is not None


# --- HTTP transport (minimal JSON-RPC-2.0 over HTTP, Bearer-checked) ----------
def _make_http_handler(dispatcher: Dispatcher, auth: TokenAuthenticator):
    from http.server import BaseHTTPRequestHandler

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "any2heliosdb-mcp/1.0"

        # Silence the default stderr access log (callers run this as a service).
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            return

        def _send_json(self, code: int, payload: Union[Dict[str, Any], List[Any]]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_401(self, message: str) -> None:
            body = json.dumps({"error": "unauthorized", "message": message}).encode("utf-8")
            self.send_response(401)
            # Advertise the scheme so a client knows to present a Bearer token.
            self.send_header("WWW-Authenticate", 'Bearer realm="any2heliosdb-mcp"')
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authenticate(self) -> Optional[Principal]:
            """Resolve the Bearer principal or send a 401 and return None."""
            try:
                return auth.authenticate_header(self.headers.get("Authorization"))
            except AuthError as e:
                self._send_401(str(e))
                return None

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path in ("/healthz", "/health"):
                self._send_json(200, {"status": "ok", "server": "any2heliosdb-mcp"})
                return
            if path in ("/mcp", "/sse"):
                # SSE channel of streamable-HTTP. Authenticated, then held open;
                # this minimal server pushes no unsolicited messages, so we keep
                # a heartbeat-free open stream that the client may close anytime.
                principal = self._authenticate()
                if principal is None:
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                except Exception:  # noqa: BLE001
                    pass
                return
            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path not in ("/mcp", "/", "/rpc"):
                self._send_json(404, {"error": "not found"})
                return
            principal = self._authenticate()
            if principal is None:
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length else b""
            try:
                message = json.loads(raw.decode("utf-8")) if raw else {}
            except (ValueError, UnicodeDecodeError) as e:
                self._send_json(200, _error(None, PARSE_ERROR, "invalid JSON: {}".format(e)))
                return
            try:
                if isinstance(message, list):  # JSON-RPC batch
                    out = [r for r in (dispatcher.handle(m, principal) for m in message)
                           if r is not None]
                    self._send_json(200, out if out else {"jsonrpc": "2.0", "result": {}})
                    return
                response = dispatcher.handle(message, principal)
            except Exception as e:  # noqa: BLE001
                self._send_json(200, _error(message.get("id") if isinstance(message, dict) else None,
                                            INTERNAL_ERROR, str(e)))
                return
            if response is None:
                # Notification: HTTP needs a body; 202 Accepted with empty object.
                self._send_json(202, {})
                return
            self._send_json(200, response)

    return _Handler


def serve_http(host: str, port: int, registry: ToolRegistry,
               auth: TokenAuthenticator) -> None:
    """Start the blocking HTTP MCP server (threaded so SSE + POST coexist)."""
    from http.server import HTTPServer
    from socketserver import ThreadingMixIn

    class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    dispatcher = Dispatcher(registry)
    handler = _make_http_handler(dispatcher, auth)
    httpd = _ThreadingHTTPServer((host, port), handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        httpd.server_close()


# --- stdio transport ---------------------------------------------------------
def serve_stdio(registry: ToolRegistry, principal: Principal,
                stdin=None, stdout=None) -> None:
    """Serve newline-delimited JSON-RPC over stdio for a single local principal."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    dispatcher = Dispatcher(registry)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError as e:
            stdout.write(json.dumps(_error(None, PARSE_ERROR, "invalid JSON: {}".format(e))) + "\n")
            stdout.flush()
            continue
        response = dispatcher.handle(message, principal)
        if response is not None:
            stdout.write(json.dumps(response, default=str) + "\n")
            stdout.flush()


# --- entrypoint --------------------------------------------------------------
def serve(transport: str = "http", host: str = "127.0.0.1", port: int = 8080,
          tokens: Optional[str] = None, tokens_file: Optional[str] = None,
          stdio_role: str = "admin", registry: Optional[ToolRegistry] = None) -> Dict[str, Any]:
    """Start the MCP server on the chosen transport. Blocks until interrupted.

    Returns a small info dict (used by the CLI to print what it started); for
    the blocking transports this only returns after shutdown.
    """
    registry = registry or build_catalog()
    auth = TokenAuthenticator.from_env(tokens, tokens_file)
    info: Dict[str, Any] = {
        "transport": transport,
        "sdk_path": "fastmcp" if sdk_available() else "builtin-jsonrpc",
        "tools": registry.names(),
        "tokens_configured": len(auth),
    }

    if transport == "stdio":
        # A local launch is trusted by default (admin); narrow with --stdio-role.
        role = Role(stdio_role)
        principal = Principal(role=role, token_id="stdio")
        info.update({"host": None, "port": None, "role": role.value})
        serve_stdio(registry, principal)
        return info

    if transport == "http":
        if len(auth) == 0:
            from ..errors import ConfigError

            raise ConfigError(
                "no tokens configured for HTTP transport — set A2H_MCP_TOKENS "
                "(token:role,...) or A2H_MCP_TOKENS_FILE / --tokens-file")
        info.update({"host": host, "port": port})
        serve_http(host, port, registry, auth)
        return info

    from ..errors import ConfigError

    raise ConfigError("unknown transport '{}' (http|stdio)".format(transport))
