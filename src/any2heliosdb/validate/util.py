"""Shared helpers for the post-load validators (CLI + MCP).

Kept in one place so the CLI ``test-count`` / ``test-data`` / ``test-index``
commands and the MCP ``test_count`` / ``test_data`` tools resolve identifiers
identically — a single definition is the only guard against the two surfaces
drifting and one of them querying wrong-cased relations.
"""
from __future__ import annotations


def effective_preserve_case(cfg, tgt) -> bool:
    """Whether validators must render identifiers in the source (upper) case.

    A validator has to spell target relations the way the migration created
    them. The native (Oracle-wire) target keeps source-case names — the
    orchestrator uses ``keep_source_case = preserve_case or oracle-dialect`` — so
    a validator that folded to lowercase would query a wrong-cased / missing
    relation and report a false mismatch. Mirror that rule: preserve case when
    ``options.preserve_case`` is set OR the target dialect is native Oracle.
    """
    return bool(getattr(cfg.options, "preserve_case", False)
                or getattr(tgt, "dialect", "") == "oracle")
