"""Durable append-only change trail (one file per extract).

Records are appended as JSON lines and fsync'd before the append returns, so a
committed record survives a crash. The reader is a simple line cursor: reading
from cursor N returns every record after line N and the new line count, which
the replicat persists only *after* a successful apply (at-least-once; combined
with idempotent upserts on the key, effectively-once per row).
"""
from __future__ import annotations

import os
from typing import List, Optional, Tuple

from ..core.change_record import ChangeRecord
from ..errors import Any2HeliosError


class Trail:
    def __init__(self, trail_dir: str) -> None:
        os.makedirs(trail_dir, exist_ok=True)
        self.path = os.path.join(trail_dir, "trail.jsonl")

    def append(self, records: List[ChangeRecord]) -> int:
        if not records:
            return 0
        with open(self.path, "a", encoding="utf-8") as f:
            for r in records:
                f.write(r.to_json() + "\n")
            f.flush()
            os.fsync(f.fileno())
        return len(records)

    def line_count(self) -> int:
        if not os.path.exists(self.path):
            return 0
        with open(self.path, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)

    def read(self, cursor: int) -> Tuple[List[ChangeRecord], int]:
        """Return records after line ``cursor`` and the new cursor (total lines).

        Torn-tail aware. A crash can persist a prefix of a batch that ends
        mid-line (``append`` fsyncs once, after writing every record), so the
        reader must distinguish two failure shapes:

        * A **torn FINAL line** — an unterminated last line (a complete appended
          record is always ``json + "\\n"``, and buffered writes flush in order,
          so a missing trailing newline means the crash cut the final record).
          This is an in-flight append that never committed, so we **stop before
          it**: it is neither applied nor does it wedge the replicat; the next
          ``extract`` self-heals it (:meth:`heal_torn_tail`). The returned cursor
          excludes it, so a later read re-evaluates it once it is completed.
        * A **corrupt MID-file line** — a terminated line that fails to parse.
          That is real corruption (not an in-flight tail), so we **raise** with an
          actionable message rather than silently skipping it and dropping a
          change (mirrors :func:`posfile.read_pos`'s fail-closed style).
        """
        if not os.path.exists(self.path):
            return [], cursor
        out: List[ChangeRecord] = []
        n = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for n, raw in enumerate(f, start=1):
                if n <= cursor:
                    continue
                if not raw.endswith("\n"):
                    # Only the file's final line can lack a trailing newline; an
                    # unterminated line is therefore the torn tail of an in-flight
                    # append. Stop before it and return a cursor that excludes it.
                    return out, max(cursor, n - 1)
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
                            self.path, n, e))
        return out, max(cursor, n)

    def heal_torn_tail(self) -> bool:
        """Truncate a torn final fragment so the trail ends on a complete line.

        ``append`` fsyncs only once, after writing every record, so a crash can
        persist a prefix ending mid-line. Such a torn FINAL fragment is
        unterminated (a committed line is always ``json + "\\n"`` and buffered
        writes flush in order). This truncates everything after the last newline
        (+fsync) and returns ``True`` if it truncated, else ``False``.

        Safe to call at ``extract`` start BEFORE dedup: a torn tail is by
        construction an incomplete append whose events were NOT covered by the
        durable source cursor (the binlog pos file / logical slot is advanced only
        AFTER ``append`` returns), so re-capture + dedup restores those events
        exactly once. Without this, :meth:`last_source_pos` would key dedup off
        the torn fragment (returning ``None`` -> dedup disabled -> duplicate lines,
        including a re-opened keymove).
        """
        if not os.path.exists(self.path):
            return False
        with open(self.path, "rb+") as f:
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
        """Byte offset just past the last ``\\n`` in *f* (0 if there is none).

        Reads backward in chunks so healing a torn tail does not scan the whole
        file. ``f`` is an open binary handle; *size* is its length.
        """
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

    def _last_complete_line(self) -> Optional[str]:
        """Return the trail's last COMPLETE (newline-terminated) line.

        Reads backwards in chunks from EOF so an extract that only needs the tail
        (the last record's source position) does not pay an O(file) scan. A torn
        FINAL fragment — an unterminated trailing partial from a crashed append —
        is **skipped**: the last complete line is the one ending at the file's
        last ``\\n``, and any bytes after that newline are the torn fragment. This
        keeps dedup keyed off a durably-written record rather than the torn tail
        (which would otherwise be unparseable -> ``None`` -> dedup disabled).
        Returns ``None`` for a missing/empty/torn-only/all-blank trail.
        """
        if not os.path.exists(self.path):
            return None
        with open(self.path, "rb") as f:
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
                # The last complete line ends at the file's last newline; anything
                # after it is a torn (unterminated) fragment we ignore. Its start is
                # the preceding newline (or the file start once fully read).
                last_nl = data.rfind(b"\n")
                if last_nl == -1:
                    continue  # buffer so far is one torn fragment; read further back
                prev_nl = data.rfind(b"\n", 0, last_nl)
                if prev_nl != -1 or pos == 0:
                    return data[prev_nl + 1:last_nl].decode("utf-8", "replace").strip() or None
            return None

    def last_source_pos(self) -> Optional[object]:
        """Return the ``source_pos`` of the trail's last COMPLETE record, else ``None``.

        ``None`` when the trail is empty, the last complete record predates the
        field (legacy line), or that line is unparseable. A torn final fragment is
        skipped by :meth:`_last_complete_line`, so a crash mid-append does not
        disable dedup. Callers treat ``None`` as "no dedup" — safe, because
        at-least-once apply already tolerates a re-read. The returned value is the
        raw ``source_pos`` (a plain int, or a compound ``[base, seq]`` list —
        compare via :func:`change_record.source_pos_key`).
        """
        line = self._last_complete_line()
        if not line:
            return None
        try:
            return ChangeRecord.from_json(line).source_pos
        except (ValueError, KeyError):
            return None
