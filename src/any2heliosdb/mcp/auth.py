"""Bearer-token authentication and role-based access control for the MCP server.

Tokens are configured *out of band* (never in the project config that the tools
operate on), so a leaked migration config can't grant API access. Two sources,
merged (the file wins on a key clash so an on-disk rotation overrides a stale
env):

* ``A2H_MCP_TOKENS`` — a ``token:role,token:role`` (or whitespace-separated) list.
* a tokens file (``A2H_MCP_TOKENS_FILE`` or an explicit path) — one
  ``token:role`` / ``token = role`` per line; ``#`` comments and blanks ignored.

Roles are an ordered hierarchy (``viewer`` < ``operator`` < ``admin``); a role
inherits every permission of the roles below it. Which *tool* each role may call
is declared on the tool itself (see :mod:`any2heliosdb.mcp.tools`) as the minimum
role required; :func:`Role.can` answers whether a caller's role clears that bar.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from ..errors import Any2HeliosError

ENV_TOKENS = "A2H_MCP_TOKENS"
ENV_TOKENS_FILE = "A2H_MCP_TOKENS_FILE"


class AuthError(Any2HeliosError):
    """Authentication failed (missing/unknown token) — maps to HTTP 401."""


class ForbiddenError(Any2HeliosError):
    """The caller's role may not call the requested tool — maps to HTTP 403."""


class Role(str, Enum):
    """An access role. ``str``-valued so it serializes cleanly into JSON.

    The ordering is a strict hierarchy: a higher role clears every lower bar.
    """

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

    @property
    def _rank(self) -> int:
        return _ROLE_RANK[self]

    def can(self, required: "Role") -> bool:
        """True iff this role is at least as privileged as *required*."""
        return self._rank >= required._rank


_ROLE_RANK: Dict[Role, int] = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: an opaque token id (never the token itself in
    logs) and the resolved role."""

    role: Role
    token_id: str  # a short, non-secret fingerprint for audit lines


def _parse_pairs(text: str, *, sep_lines: bool) -> Dict[str, Role]:
    """Parse ``token:role`` pairs. With *sep_lines*, one pair per line (file);
    otherwise comma/whitespace separated (env var)."""
    out: Dict[str, Role] = {}
    if not text:
        return out
    chunks = text.splitlines() if sep_lines else text.replace(",", " ").split()
    for raw in chunks:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # accept ``token:role``, ``token=role``, or ``token role``
        for delim in (":", "="):
            if delim in line:
                token, _, role_s = line.partition(delim)
                break
        else:
            token, _, role_s = line.partition(" ")
        token = token.strip()
        role_s = role_s.strip().lower()
        if not token or not role_s:
            raise Any2HeliosError(
                "malformed token mapping {!r} (expected 'token:role')".format(raw)
            )
        try:
            out[token] = Role(role_s)
        except ValueError as e:
            raise Any2HeliosError(
                "unknown role {!r} (valid: {})".format(
                    role_s, ", ".join(r.value for r in Role)
                )
            ) from e
    return out


def load_tokens(
    env_value: Optional[str] = None,
    tokens_file: Optional[str] = None,
    *,
    environ: Optional[Dict[str, str]] = None,
) -> Dict[str, Role]:
    """Resolve the configured ``token -> role`` map from env + file.

    Explicit *env_value* / *tokens_file* args (used by the CLI/tests) take
    precedence over the corresponding environment variables. The file overrides
    the env var on duplicate tokens (an on-disk rotation beats a stale export).
    """
    env = os.environ if environ is None else environ
    raw_env = env_value if env_value is not None else env.get(ENV_TOKENS, "")
    mapping: Dict[str, Role] = dict(_parse_pairs(raw_env or "", sep_lines=False))

    path = tokens_file if tokens_file is not None else env.get(ENV_TOKENS_FILE)
    if path:
        try:
            with open(path, "r", encoding="utf-8") as f:
                contents = f.read()
        except FileNotFoundError as e:
            raise Any2HeliosError("tokens file not found: {}".format(path)) from e
        mapping.update(_parse_pairs(contents, sep_lines=True))
    return mapping


def _fingerprint(token: str) -> str:
    """A short non-secret id for a token, safe to log (never the token)."""
    import hashlib

    return "tok_" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


class TokenAuthenticator:
    """Resolves a Bearer token to a :class:`Principal`."""

    def __init__(self, tokens: Dict[str, Role]):
        self._tokens = dict(tokens)

    @classmethod
    def from_env(
        cls,
        env_value: Optional[str] = None,
        tokens_file: Optional[str] = None,
        *,
        environ: Optional[Dict[str, str]] = None,
    ) -> "TokenAuthenticator":
        return cls(load_tokens(env_value, tokens_file, environ=environ))

    def __len__(self) -> int:
        return len(self._tokens)

    @staticmethod
    def extract_bearer(authorization: Optional[str]) -> str:
        """Pull the raw token out of an ``Authorization: Bearer <token>`` header.

        Raises :class:`AuthError` (→ 401) when the header is absent or malformed.
        """
        if not authorization:
            raise AuthError("missing Authorization header (expected 'Bearer <token>')")
        parts = authorization.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise AuthError("malformed Authorization header (expected 'Bearer <token>')")
        return parts[1].strip()

    def authenticate(self, token: str) -> Principal:
        """Resolve a raw token to a :class:`Principal` or raise :class:`AuthError`."""
        role = self._tokens.get(token)
        if role is None:
            raise AuthError("invalid or unknown token")
        return Principal(role=role, token_id=_fingerprint(token))

    def authenticate_header(self, authorization: Optional[str]) -> Principal:
        """Convenience: extract the bearer token then authenticate it."""
        return self.authenticate(self.extract_bearer(authorization))


# --- token generation / token-file management (a2h mcp auth) -----------------

def generate_token(nbytes: int = 32) -> str:
    """A cryptographically-strong, URL-safe Bearer token (no ``:`` so it never
    collides with the ``token:role`` field separator)."""
    return secrets.token_urlsafe(nbytes)


def default_tokens_file(environ: Optional[Dict[str, str]] = None) -> str:
    """The token file a2h reads/writes by default: ``$A2H_MCP_TOKENS_FILE`` if set,
    else ``~/.config/a2h/mcp-tokens``."""
    env = os.environ if environ is None else environ
    return env.get(ENV_TOKENS_FILE) or os.path.join(
        os.path.expanduser("~"), ".config", "a2h", "mcp-tokens")


def write_token_file(path: str, token: str, role: Role, *, append: bool = True) -> None:
    """Write ``token:role`` to *path* as a private tokens file the server reads.

    The file is created with ``0600`` and its parent dir with ``0700`` (best
    effort) so the secret is never world-readable — the whole point of keeping
    tokens in a file instead of on the command line or in the project config.
    Appends by default (multiple tokens coexist); ``append=False`` truncates
    first (rotation). The line format round-trips through :func:`load_tokens`.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
        try:
            os.chmod(parent, 0o700)
        except OSError:  # pragma: no cover - best effort on exotic filesystems
            pass
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)  # 0600 from creation — no world-readable window
    try:
        os.write(fd, "{}:{}\n".format(token, role.value).encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)  # enforce 0600 even if the file pre-existed
    except OSError:  # pragma: no cover
        pass
