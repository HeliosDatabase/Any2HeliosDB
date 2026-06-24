"""Snapshot -> Rich renderable, plus the read-only refresh loop.

Design: ``render_snapshot`` is a *pure* function — given a ``progress_snapshot``
dict (and optional elapsed/eta), it returns a Rich renderable and raises
nothing, so it can be unit-tested with a hand-built dict and no TTY. The
``run_monitor`` loop opens the manifest read-only, polls the snapshot ~1/sec,
and drives a Rich ``Live`` display, auto-exiting when the run is complete or on
Ctrl-C. A ``--once`` / non-TTY path renders a single frame for CI/scripts.

Python 3.9 compatible: ``from __future__ import annotations``, no structural
pattern matching, str-Enum module constants only, Rich imported lazily so the
import cost is paid only when the monitor actually runs.
"""
from __future__ import annotations

import time
from typing import Optional

from ..core import manifest as M

# Display styling per derived status (kept tiny + dependency-free).
_STATUS_STYLE = {
    M.PENDING: ("dim", "pending"),
    M.IN_PROGRESS: ("bold yellow", "loading"),
    M.LOADED: ("green", "loaded"),
    M.VERIFIED: ("bold green", "verified"),
    M.FAILED: ("bold red", "FAILED"),
}


def _fmt_int(n: int) -> str:
    """Group-separated integer, e.g. 1234567 -> '1,234,567'."""
    try:
        return "{:,}".format(int(n))
    except (TypeError, ValueError):
        return str(n)


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "{}h{:02d}m{:02d}s".format(h, m, s)
    if m:
        return "{}m{:02d}s".format(m, s)
    return "{}s".format(s)


def _bar(loaded: int, total: int, width: int = 18) -> str:
    """A plain text progress bar (no Rich Progress task plumbing needed)."""
    if total <= 0:
        return "[" + (" " * width) + "]"
    frac = max(0.0, min(1.0, loaded / total))
    filled = int(round(frac * width))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def _volume_left(row: dict) -> Optional[int]:
    """est_rows - rows_loaded, only when an estimate exists (else unknown)."""
    est = int(row.get("rows_est") or 0)
    if est <= 0:
        return None
    return max(0, est - int(row.get("rows_loaded") or 0))


def _overall_pct(snapshot: dict) -> float:
    """Overall percent complete, by chunk progress (rows estimates are coarse)."""
    total = int(snapshot.get("chunks_total") or 0)
    if total <= 0:
        return 100.0 if snapshot.get("complete") else 0.0
    loaded = int(snapshot.get("chunks_loaded") or 0)
    return max(0.0, min(100.0, 100.0 * loaded / total))


def _eta_seconds(rows_loaded: int, rows_est: int, elapsed: Optional[float]) -> Optional[float]:
    """Simple ETA from average rows/sec since start; None if not enough info."""
    if not elapsed or elapsed <= 0 or rows_loaded <= 0 or rows_est <= 0:
        return None
    remaining = rows_est - rows_loaded
    if remaining <= 0:
        return 0.0
    rate = rows_loaded / elapsed  # rows/sec
    if rate <= 0:
        return None
    return remaining / rate


def render_snapshot(snapshot: dict, elapsed: Optional[float] = None,
                    eta: Optional[float] = None):  # type: ignore[no-untyped-def]
    """Build a Rich renderable (table + totals panel) from a snapshot dict.

    Pure: no I/O, no TTY, deterministic given its arguments. ``elapsed`` and
    ``eta`` are passed in (seconds) so the function stays time-independent and
    testable; when omitted, the footer shows '-' / derives ETA from the totals.
    Returns a ``rich.console.Group`` so the caller can hand it straight to
    ``Console.print`` or a ``Live`` display.
    """
    # Lazy Rich imports: only paid when the monitor actually renders a frame.
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table as RichTable
    from rich.text import Text

    tables = snapshot.get("tables", [])

    grid = RichTable(
        title="a2h migration monitor — run {}".format(snapshot.get("run_id", "?")),
        expand=True, header_style="bold", title_style="bold cyan",
    )
    grid.add_column("source", overflow="fold")
    grid.add_column("-> target", overflow="fold")
    grid.add_column("status", no_wrap=True)
    grid.add_column("chunks", justify="right", no_wrap=True)
    grid.add_column("rows", justify="right", no_wrap=True)
    grid.add_column("est", justify="right", no_wrap=True)
    grid.add_column("progress", no_wrap=True)
    grid.add_column("vol left", justify="right", no_wrap=True)

    for row in tables:
        status = row.get("status", M.PENDING)
        style, label = _STATUS_STYLE.get(status, ("", str(status)))
        ctotal = int(row.get("chunks_total") or 0)
        cloaded = int(row.get("chunks_loaded") or 0)
        cfailed = int(row.get("chunks_failed") or 0)
        chunk_txt = "{}/{}".format(cloaded, ctotal)
        if cfailed:
            chunk_txt += " ({} failed)".format(cfailed)
        rows_loaded = int(row.get("rows_loaded") or 0)
        rows_est = int(row.get("rows_est") or 0)
        est_txt = _fmt_int(rows_est) if rows_est > 0 else "?"
        vleft = _volume_left(row)
        vleft_txt = _fmt_int(vleft) if vleft is not None else "?"
        bar = "{} {:3.0f}%".format(
            _bar(cloaded, ctotal),
            100.0 * cloaded / ctotal if ctotal else (100.0 if status in (M.LOADED, M.VERIFIED) else 0.0),
        )
        # In-progress rows are visually distinct (whole row styled).
        row_style = "bold yellow" if status == M.IN_PROGRESS else (
            "red" if status == M.FAILED else None)
        grid.add_row(
            row.get("table_fqn", "?"),
            row.get("target_table", "?"),
            Text(label, style=style),
            chunk_txt,
            _fmt_int(rows_loaded),
            est_txt,
            bar,
            vleft_txt,
            style=row_style,
        )

    if not tables:
        grid.add_row("(no tables in this run yet)", "", "", "", "", "", "", "")

    # ---- totals / footer panel ----
    tables_total = int(snapshot.get("tables_total") or 0)
    tables_done = int(snapshot.get("tables_done") or 0)
    rows_loaded = int(snapshot.get("rows_loaded") or 0)
    rows_est = int(snapshot.get("rows_est") or 0)
    pct = _overall_pct(snapshot)
    agg_left = max(0, rows_est - rows_loaded) if rows_est > 0 else None
    if eta is None:
        eta = _eta_seconds(rows_loaded, rows_est, elapsed)
    chunks_failed = int(snapshot.get("chunks_failed") or 0)

    footer = Text()
    footer.append("tables ")
    footer.append("{}/{}".format(tables_done, tables_total), style="bold")
    footer.append("   rows ")
    footer.append(_fmt_int(rows_loaded), style="bold")
    if rows_est > 0:
        footer.append(" / ~{}".format(_fmt_int(rows_est)))
    footer.append("   overall ")
    footer.append("{:.1f}%".format(pct), style="bold cyan")
    footer.append("   vol left ")
    footer.append(_fmt_int(agg_left) if agg_left is not None else "?", style="bold")
    footer.append("\nelapsed ")
    footer.append(_fmt_duration(elapsed), style="bold")
    footer.append("   eta ")
    footer.append(_fmt_duration(eta) if eta is not None else "-", style="bold")
    if chunks_failed:
        footer.append("   ")
        footer.append("{} chunk(s) FAILED".format(chunks_failed), style="bold red")
    if snapshot.get("complete"):
        footer.append("   ")
        footer.append("COMPLETE", style="bold green")

    panel = Panel(footer, title="totals", border_style="cyan", expand=True)
    return Group(grid, panel)


def run_monitor(manifest_path: str, run_id: str, interval: float = 1.0,
                once: bool = False, console=None) -> int:  # type: ignore[no-untyped-def]
    """Read-only refresh loop. Returns 0 when the run is complete, else 1.

    Opens the manifest read-only and polls ``progress_snapshot`` every
    ``interval`` seconds, driving a Rich ``Live`` full-screen display. Auto-exits
    when every table is loaded/verified or on Ctrl-C. ``once=True`` (and the
    non-TTY case) renders a single frame and returns — the CI/script fallback.
    """
    from rich.console import Console
    from rich.live import Live

    if console is None:
        console = Console()

    man = M.Manifest.open_readonly(manifest_path)
    start = time.monotonic()

    def snap():  # type: ignore[no-untyped-def]
        return man.progress_snapshot(run_id)

    try:
        first = snap()
        # Single-frame path: explicit --once, or no interactive terminal (CI).
        if once or not console.is_terminal:
            elapsed = time.monotonic() - start
            console.print(render_snapshot(first, elapsed=elapsed))
            return 0 if first.get("complete") else 1

        with Live(render_snapshot(first, elapsed=0.0), console=console,
                  refresh_per_second=max(1.0, 1.0 / max(interval, 0.05)),
                  screen=True) as live:
            current = first
            while True:
                if current.get("complete"):
                    # Render the final frame once more, then exit cleanly.
                    live.update(render_snapshot(current, elapsed=time.monotonic() - start))
                    break
                time.sleep(max(interval, 0.05))
                current = snap()
                live.update(render_snapshot(current, elapsed=time.monotonic() - start))
        return 0 if current.get("complete") else 1
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 1
    finally:
        man.close()
