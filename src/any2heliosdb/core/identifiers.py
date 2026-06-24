"""Canonical SQL identifier handling — the single source of truth shared by the
DDL emitter, the data loader/driver, and the validators so they can never
disagree on how a table or column is named on the HeliosDB target.

Two concerns are kept separate so the layering stays clean:

* **case folding** (``fold``) is a *tool* decision driven by ``preserve_case``:
  off → lowercase (Ora2Pg's default, PG-idiomatic); on → keep the source case.
* **quoting** (``quote_ident``) is a *rendering* decision: a folded identifier is
  emitted bare when it is a simple lowercase word that is not reserved, and
  double-quoted otherwise (reserved word, mixed case, or special characters).

Direct SQL builders (the DDL emitter, the validators) need both and call
``render_ident`` / ``render_table``. The loader folds (tool side) and the target
driver quotes (wire side); composing the two yields exactly the same string as
``render_ident``, which is what lets DDL, load, and validation agree.
"""
from __future__ import annotations

import re

_SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

# PostgreSQL fully-reserved keywords — those that are a syntax error when used
# unquoted as an identifier. Quoting one is always safe; leaving it bare is not.
# Non-reserved words (name, value, type, count, …) are intentionally absent so
# we keep identifiers bare wherever possible (some HeliosDB editions handle bare
# identifiers more uniformly than quoted ones).
_RESERVED = frozenset(
    {
        "all", "analyse", "analyze", "and", "any", "array", "as", "asc",
        "asymmetric", "both", "case", "cast", "check", "collate", "column",
        "constraint", "create", "current_catalog", "current_date",
        "current_role", "current_time", "current_timestamp", "current_user",
        "default", "deferrable", "desc", "distinct", "do", "else", "end",
        "except", "false", "fetch", "for", "foreign", "from", "grant", "group",
        "having", "in", "initially", "intersect", "into", "lateral", "leading",
        "limit", "localtime", "localtimestamp", "not", "null", "offset", "on",
        "only", "or", "order", "placing", "primary", "references", "returning",
        "select", "session_user", "some", "symmetric", "table", "then", "to",
        "trailing", "true", "union", "unique", "user", "using", "variadic",
        "when", "where", "window", "with",
    }
)


def fold(name: str, preserve_case: bool = False) -> str:
    """Apply the case policy: lowercase unless ``preserve_case``.

    Produces the *logical* target identifier (still bare, unquoted) — the form
    used as a manifest key and shared between the loader and the validators.
    """
    return name if preserve_case else name.lower()


def quote_ident(name: str) -> str:
    """Double-quote a SQL identifier only when necessary.

    Simple lowercase identifiers that are not reserved are emitted bare; reserved
    words and identifiers with mixed case or special characters are quoted (with
    embedded quotes doubled).
    """
    if _SAFE_IDENT.match(name) and name not in _RESERVED:
        return name
    return '"' + name.replace('"', '""') + '"'


def quote_table(name: str) -> str:
    """Quote a possibly schema-qualified table name (``schema.table``)."""
    return ".".join(quote_ident(p) for p in name.split("."))


def render_ident(name: str, preserve_case: bool = False) -> str:
    """Fold then quote — for builders that emit identifiers straight into SQL."""
    return quote_ident(fold(name, preserve_case))


def render_table(name: str, preserve_case: bool = False) -> str:
    """Fold then quote a (possibly qualified) table name for direct SQL."""
    return ".".join(render_ident(p, preserve_case) for p in name.split("."))
