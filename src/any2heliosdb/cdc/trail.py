"""Durable append-only change trail (one directory per extract).

Records are appended as JSON lines and fsync'd before the append returns, so a
committed record survives a crash. The reader is a simple line cursor: reading
from cursor N returns every record after line N and the new line count, which
the replicat persists only *after* a successful apply (at-least-once; combined
with idempotent upserts on the key, effectively-once per row).

**Rotation (tier-2).** An unbounded ``trail.jsonl`` grows forever and the
replicat's read is O(file). When ``rotate_mb`` is set, the trail is split into
size-bounded **segments**: the legacy ``trail.jsonl`` is segment 0, and rotated
segments are ``trail.00001.jsonl``, ``trail.00002.jsonl``, … The active segment
is always the highest-numbered one. Crucially the apply **cursor stays a single
global line index** spanning every segment — it never becomes a ``(segment,
line)`` pair — so:

* a legacy single-file trail (and its integer cursor) keeps working byte-for-byte
  (``rotate_mb=0`` never creates a numbered segment);
* the keymove barrier, torn-tail heal, and ``source_pos`` dedup are all unchanged
  (they operate on global line indices / the active segment exactly as before).

``purge_applied`` deletes fully-applied *closed* segments (never the active one,
never past the apply cursor). Because that removes lines from the front, the
count of removed lines is persisted in ``trail.meta`` (``purged_lines``) and the
reader treats the first ``purged_lines`` global indices as already gone — so the
global cursor stays valid and monotonic across a purge.

**Crash-safe purge ordering.** For every closed segment the meta is persisted
*first* (``purged_lines += segment_line_count`` + the segment's number as
``last_purged_segment``, atomic write + fsync) and the file is removed only
*after*. A crash between those two steps therefore leaves the file on disk while
its lines are already counted in ``purged_lines`` — the reader double-counts them,
so global indices shift **up** (a safe replay window under at-least-once +
idempotent apply, never a skip). ``last_purged_segment`` makes recovery
deterministic: on the next purge a still-present segment whose lines are already
counted is detected and removed. The opposite ordering (remove-then-write) would
shift indices *down* on a crash and silently skip the un-applied records that fell
below the stale cursor.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

from ..core.change_record import ChangeRecord
from ..errors import Any2HeliosError

_SEG_RE = re.compile(r"^trail\.(\d{5})\.jsonl$")
_META_NAME = "trail.meta"


class Trail:
    def __init__(self, trail_dir: str, rotate_mb: int = 0) -> None:
        os.makedirs(trail_dir, exist_ok=True)
        self.dir = trail_dir
        # ``path`` remains the legacy single-file location (segment 0), so callers
        # and tests that reference it keep working; rotation only *adds* numbered
        # segments beside it.
        self.path = os.path.join(trail_dir, "trail.jsonl")
        # <=0 disables rotation entirely (one trail.jsonl — pre-tier-2 behaviour).
        self.rotate_bytes = int(rotate_mb) * 1024 * 1024 if rotate_mb and rotate_mb > 0 else 0
        # Per-instance cache of CLOSED (non-active) segment line counts. A closed
        # segment is immutable, so caching its count lets ``read`` skip a whole
        # already-applied segment without re-scanning it line by line on every
        # chunked read of the per-run apply loop (was O(lines x chunks)).
        self._seg_line_counts: dict = {}

    # --- segment layout ---------------------------------------------------
    def _seg_path(self, idx: int) -> str:
        """Path of segment *idx* (0 == legacy ``trail.jsonl``)."""
        if idx == 0:
            return self.path
        return os.path.join(self.dir, "trail.{:05d}.jsonl".format(idx))

    def _segment_indices(self) -> List[int]:
        """Sorted indices of the segment files that currently exist (0 for
        ``trail.jsonl``, N for ``trail.NNNNN.jsonl``). May be a non-contiguous
        suffix after ``purge_applied`` deleted a prefix of closed segments."""
        idxs: List[int] = []
        if os.path.exists(self.path):
            idxs.append(0)
        for name in os.listdir(self.dir):
            m = _SEG_RE.match(name)
            if m:
                idxs.append(int(m.group(1)))
        return sorted(idxs)

    def _ordered_segments(self) -> List[Tuple[int, str]]:
        return [(i, self._seg_path(i)) for i in self._segment_indices()]

    def _active_index(self) -> int:
        idxs = self._segment_indices()
        return idxs[-1] if idxs else 0

    def _active_path(self) -> str:
        return self._seg_path(self._active_index())

    # --- purge bookkeeping ------------------------------------------------
    def _meta_path(self) -> str:
        return os.path.join(self.dir, _META_NAME)

    def _read_meta(self) -> dict:
        p = self._meta_path()
        if not os.path.exists(p):
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except (ValueError, OSError):
            return {}

    def _purged_lines(self) -> int:
        try:
            return int(self._read_meta().get("purged_lines", 0))
        except (ValueError, TypeError):
            return 0

    def _last_purged_segment(self) -> int:
        """Segment number whose lines are the highest already counted into
        ``purged_lines`` (``-1`` when nothing has been purged). Persisted alongside
        the count so a crash between the meta write and the file removal is
        recoverable deterministically (:meth:`_reconcile_purged_segments`)."""
        try:
            return int(self._read_meta().get("last_purged_segment", -1))
        except (ValueError, TypeError):
            return -1

    def _write_purged_meta(self, purged: int, last_segment: int) -> None:
        directory = self.dir
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".trail.meta.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"purged_lines": int(purged),
                           "last_purged_segment": int(last_segment)}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._meta_path())
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @contextmanager
    def exclusive(self, operation: str) -> Iterator[None]:
        """Exclusive per-trail lock for operations that map global line indices
        to durable state: a replicat run (reads indices, persists the apply
        cursor) and ``purge_applied`` (removes segments, shifting what a
        concurrent unlocked reader would see). Without it, a purge crash could
        leave a double-counted leftover segment that a concurrently-running
        replicat bakes into its persisted cursor — the deflation on the next
        reconcile would then skip records. flock is advisory but every writer
        in this codebase takes it; fail fast (non-blocking) with an actionable
        message rather than queueing silently."""
        lock_path = os.path.join(self.dir, "trail.lock")
        f = open(lock_path, "w")
        try:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Any2HeliosError(
                    "trail {!r} is locked by another process — a replicat run or "
                    "--purge-applied is already operating on this extract. Re-run "
                    "{} after it finishes (concurrent index-shifting operations "
                    "on one trail are not safe).".format(self.dir, operation))
            yield
        finally:
            f.close()

    def reconcile_purged(self) -> List[str]:
        """Public entry for crash recovery: see :meth:`_reconcile_purged_segments`.
        Callers MUST hold :meth:`exclusive` — reconciling shifts global line
        indices, so it must never race a reader that persists cursors."""
        return self._reconcile_purged_segments()

    def _reconcile_purged_segments(self) -> List[str]:
        """Remove any closed segment whose lines are ALREADY counted in
        ``purged_lines`` but whose file still exists — a crash between the meta
        write and the file removal in a prior :meth:`purge_applied`. Deterministic
        via ``last_purged_segment``: a still-present closed segment at-or-below it
        is a leftover (its lines are double-counted until dropped). Returns the
        paths removed. A no-op in the normal case (no leftover)."""
        last = self._last_purged_segment()
        if last < 0:
            return []
        active = self._active_index()
        removed: List[str] = []
        for idx in self._segment_indices():
            if idx <= last and idx != active:
                path = self._seg_path(idx)
                try:
                    os.remove(path)
                    removed.append(path)
                except FileNotFoundError:
                    pass
                self._seg_line_counts.pop(idx, None)
        return removed

    # --- append -----------------------------------------------------------
    def append(self, records: List[ChangeRecord]) -> int:
        if not records:
            return 0
        active = self._active_path()
        # Rotate BEFORE writing when the active segment has reached the size cap
        # (and rotation is enabled). The new records then start a fresh segment, so
        # a segment never exceeds the cap by more than one final batch. Segment 0
        # (legacy trail.jsonl) rotates to trail.00001.jsonl and so on.
        if (self.rotate_bytes and os.path.exists(active)
                and os.path.getsize(active) >= self.rotate_bytes):
            active = self._seg_path(self._active_index() + 1)
        with open(active, "a", encoding="utf-8") as f:
            for r in records:
                f.write(r.to_json() + "\n")
            f.flush()
            os.fsync(f.fileno())
        return len(records)

    def line_count(self) -> int:
        total = self._purged_lines()
        for _idx, path in self._ordered_segments():
            with open(path, "r", encoding="utf-8") as f:
                total += sum(1 for _ in f)
        return total

    # --- read -------------------------------------------------------------
    def read(self, cursor: int, limit: Optional[int] = None) -> Tuple[List[ChangeRecord], int]:
        """Return records after global line ``cursor`` and the new global cursor.

        ``limit`` (tier-2) bounds how many *records* are returned in one call, so
        the replicat can apply a large slice in memory-bounded chunks: the returned
        cursor advances only past the lines actually read, and a follow-up
        ``read(new_cursor, limit)`` continues from there. ``limit=None`` reads the
        whole remaining slice (pre-tier-2 behaviour).

        Torn-tail aware. A crash can persist a prefix of a batch that ends mid-line
        (``append`` fsyncs once, after writing every record), so the reader
        distinguishes two failure shapes:

        * A **torn FINAL line** — an unterminated last line of the **active**
          (last) segment. This is an in-flight append that never committed, so we
          **stop before it**: it is neither applied nor wedges the replicat; the
          next ``extract`` self-heals it (:meth:`heal_torn_tail`).
        * A **corrupt terminated line** (mid-file, or an unterminated line in a
          *closed* segment which by construction always ends complete). That is
          real corruption, so we **raise** rather than silently drop a change.

        Segments are walked in order and share one continuous global line index
        that starts just past the purged prefix, so the cursor is directly
        comparable across a rotation or a purge.
        """
        out: List[ChangeRecord] = []
        gidx = self._purged_lines()
        segs = self._ordered_segments()
        last_pos = len(segs) - 1
        for seg_pos, (_idx, path) in enumerate(segs):
            is_last = seg_pos == last_pos
            # Skip a whole CLOSED segment that ends at-or-below the cursor without
            # opening it: its line count is immutable, so serve it from the cache
            # (only the active/last segment grows and is never cached here). This
            # turns the chunked apply loop's repeated reads from O(lines x chunks)
            # into O(remaining lines) per chunk.
            if not is_last:
                n = self._closed_seg_line_count(_idx, path)
                if gidx + n <= cursor:
                    gidx += n
                    continue
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    gidx += 1
                    if gidx <= cursor:
                        continue
                    if not raw.endswith("\n"):
                        if is_last:
                            # In-flight torn tail of the active segment: stop before
                            # it and return a cursor that excludes it.
                            return out, max(cursor, gidx - 1)
                        # A closed segment must end on a complete line; an
                        # unterminated line there is corruption, not an in-flight tail.
                        raise Any2HeliosError(
                            "CDC trail segment {!r} line {}: unterminated line in a "
                            "closed segment (real corruption, not an in-flight tail). "
                            "Restore the segment from backup, or truncate it at the last "
                            "good line to resume.".format(path, gidx))
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        out.append(ChangeRecord.from_json(line))
                    except (ValueError, KeyError) as e:
                        raise Any2HeliosError(
                            "CDC trail {!r} line {}: corrupt/unparseable change record ({}). "
                            "This is a terminated mid-trail line, not an in-flight tail, so "
                            "refusing to skip it and silently drop a change. Restore the trail "
                            "from backup, or truncate it at the last good line to resume.".format(
                                path, gidx, e))
                    if limit is not None and len(out) >= limit:
                        return out, gidx
        return out, max(cursor, gidx)

    # --- purge ------------------------------------------------------------
    def _closed_seg_line_count(self, idx: int, path: str) -> int:
        """Line count of CLOSED segment *idx*, cached (immutable once closed)."""
        n = self._seg_line_counts.get(idx)
        if n is None:
            with open(path, "r", encoding="utf-8") as f:
                n = sum(1 for _ in f)
            self._seg_line_counts[idx] = n
        return n

    def purge_applied(self, cursor: int) -> List[str]:
        """Delete fully-applied *closed* segments (a prefix), return their paths.

        A closed segment is deletable only when its highest global line index is
        ``<=`` *cursor* (every record in it is durably applied). The active (last)
        segment is never deleted, and deletion stops at the first segment that is
        not fully applied — so the surviving segments are always a suffix and the
        removed line count is a clean prefix, persisted as ``purged_lines`` so the
        global cursor stays valid.

        **Crash-safe ordering.** Per segment the meta is persisted *first*
        (``purged_lines`` bumped + this segment recorded as ``last_purged_segment``,
        atomic + fsync) and the file removed only *after*. A crash between the two
        leaves a counted-but-present segment that :meth:`_reconcile_purged_segments`
        (run first, below) removes on the next purge — the reader double-counts it
        meanwhile, shifting indices *up* into a safe replay window rather than down
        into a silent skip.
        """
        # Heal any leftover from a crash between a prior meta-write and remove.
        self._reconcile_purged_segments()
        segs = self._ordered_segments()
        if len(segs) <= 1:
            return []  # only the active segment (or none) — nothing closed to purge
        active_idx = segs[-1][0]
        gidx = self._purged_lines()
        deleted: List[str] = []
        for idx, path in segs:
            if idx == active_idx:
                break
            n = self._closed_seg_line_count(idx, path)
            end = gidx + n
            if end > cursor:
                break  # not fully applied — stop (segments are ordered)
            # (1) durably count this segment's lines and mark it purged BEFORE
            #     removing the file, so a crash here re-applies (never skips).
            self._write_purged_meta(end, idx)
            # (2) then remove the file.
            os.remove(path)
            self._seg_line_counts.pop(idx, None)
            deleted.append(path)
            gidx = end
        return deleted

    def segment_paths(self) -> List[str]:
        """Ordered paths of the currently-existing segment files (for display)."""
        return [path for _idx, path in self._ordered_segments()]

    # --- torn-tail heal (active segment) ----------------------------------
    def heal_torn_tail(self) -> bool:
        """Truncate a torn final fragment so the active segment ends on a complete line.

        ``append`` fsyncs only once, after writing every record, so a crash can
        persist a prefix ending mid-line. Such a torn fragment can only be in the
        **active** (last) segment — a segment is only ever written while active,
        and rotation happens between appends, so any earlier segment was closed
        after a complete append. This truncates everything after the last newline
        (+fsync) of the active segment and returns ``True`` if it truncated.

        Safe to call at ``extract`` start BEFORE dedup: a torn tail is by
        construction an incomplete append whose events were NOT covered by the
        durable source cursor, so re-capture + dedup restores them exactly once.
        """
        active = self._active_path()
        if not os.path.exists(active):
            return False
        with open(active, "rb+") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return False
            f.seek(size - 1)
            if f.read(1) == b"\n":
                return False  # ends on a complete line — nothing torn to heal
            keep = self._last_newline_offset(f, size)
            f.truncate(keep)
            f.flush()
            os.fsync(f.fileno())
        return True

    @staticmethod
    def _last_newline_offset(f, size: int) -> int:
        """Byte offset just past the last ``\\n`` in *f* (0 if there is none)."""
        chunk = 4096
        pos = size
        while pos > 0:
            read = min(chunk, pos)
            pos -= read
            f.seek(pos)
            data = f.read(read)
            nl = data.rfind(b"\n")
            if nl != -1:
                return pos + nl + 1
        return 0

    @staticmethod
    def _last_complete_line_in(path: str) -> Optional[str]:
        """The last COMPLETE (newline-terminated) line of *path*, skipping a torn
        final fragment. Reads backward in chunks so we don't scan the whole file.
        Returns ``None`` for a missing/empty/torn-only/all-blank file."""
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            if pos == 0:
                return None
            chunk = 4096
            data = b""
            while pos > 0:
                read = min(chunk, pos)
                pos -= read
                f.seek(pos)
                data = f.read(read) + data
                last_nl = data.rfind(b"\n")
                if last_nl == -1:
                    continue  # buffer so far is one torn fragment; read further back
                prev_nl = data.rfind(b"\n", 0, last_nl)
                if prev_nl != -1 or pos == 0:
                    return data[prev_nl + 1:last_nl].decode("utf-8", "replace").strip() or None
        return None

    def _last_complete_line(self) -> Optional[str]:
        """The trail's last COMPLETE line across segments (active first, then the
        previous segment if the active is empty/torn-only)."""
        for _idx, path in reversed(self._ordered_segments()):
            line = self._last_complete_line_in(path)
            if line:
                return line
        return None

    def last_source_pos(self) -> Optional[object]:
        """Return the ``source_pos`` of the trail's last COMPLETE record, else ``None``.

        ``None`` when the trail is empty, the last complete record predates the
        field (legacy line), or that line is unparseable. A torn final fragment is
        skipped, so a crash mid-append does not disable dedup. Callers treat
        ``None`` as "no dedup" — safe, because at-least-once apply tolerates a
        re-read. The value is the raw ``source_pos`` (int or ``[base, seq]``).
        """
        line = self._last_complete_line()
        if not line:
            return None
        try:
            return ChangeRecord.from_json(line).source_pos
        except (ValueError, KeyError):
            return None
