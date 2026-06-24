"""Live full-screen migration monitor (``a2h monitor``).

Watches an in-progress (or finished) migration by reading the durable run
manifest **read-only** while the loader writes concurrently (sqlite WAL allows
one reader). The snapshot->renderable logic lives in a pure function so it is
unit-testable without a TTY or a live database; the refresh loop wraps it.
"""
from __future__ import annotations

from .live import render_snapshot, run_monitor

__all__ = ["render_snapshot", "run_monitor"]
