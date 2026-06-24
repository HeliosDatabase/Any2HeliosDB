"""MCP (Model Context Protocol) server for Any2HeliosDB.

Exposes the ``a2h`` capabilities as MCP **tools** so AI agents can administer a
migration remotely, with Bearer-token authentication and role-based access
control (viewer < operator < admin). Each tool wraps an engine function and
returns structured JSON.

Transports: HTTP (streamable JSON-RPC, for remote agents) and stdio (local).
The official ``mcp`` SDK is used when importable (it needs Python >= 3.10); on
older runtimes a minimal, wire-compatible JSON-RPC-2.0 endpoint is used instead.

Importing this package is side-effect free and pulls in no database driver.
"""
from __future__ import annotations

from .auth import (
    AuthError,
    ForbiddenError,
    Principal,
    Role,
    TokenAuthenticator,
    load_tokens,
)
from .tools import Tool, ToolRegistry, build_catalog

__all__ = [
    "AuthError",
    "ForbiddenError",
    "Principal",
    "Role",
    "TokenAuthenticator",
    "load_tokens",
    "Tool",
    "ToolRegistry",
    "build_catalog",
    "serve",
]


def serve(*args, **kwargs):  # type: ignore[no-untyped-def]
    """Lazy wrapper around :func:`any2heliosdb.mcp.server.serve` (keeps the
    package import light; the server module imports ``http.server`` etc.)."""
    from .server import serve as _serve

    return _serve(*args, **kwargs)
