"""Durable, atomic binlog/LSN position-file I/O with fail-closed recovery.

The MySQL binlog source treats an empty position as *"anchor at the current
coordinate, capture nothing yet"* — correct only on a genuinely fresh extract.
A crash *during* a plain ``open(path, "w")`` truncates the file to zero bytes, so
a re-anchor there would silently skip every change between the last good cursor
and "now" (unbounded data loss).

This module closes both holes:

* :func:`write_pos_atomic` never leaves a torn file visible — it writes a temp
  file in the same directory, ``fsync``s it (mirroring :mod:`trail`'s durability),
  then ``os.replace``s it into place, so a reader sees either the whole old value
  or the whole new value.
* :func:`read_pos` distinguishes *"file never existed"* (fresh extract — return
  ``None``, safe to anchor) from *"file exists but is empty/corrupt"* (a truncated
  or malformed cursor — raise, so capture fails closed instead of re-anchoring).
"""
from __future__ import annotations

import os
import tempfile
from typing import Optional

from ..errors import Any2HeliosError


def _looks_like_pos(s: str) -> bool:
    """A binlog coordinate is ``<file>:<int-pos>`` (e.g. ``mysql-bin.000003:1547``);
    a PostgreSQL LSN mirror is ``<hi>/<lo>`` hex. Accept either shape so a merely
    truncated/garbage cursor is rejected while both real formats pass."""
    file_part, colon, pos_part = s.rpartition(":")
    if colon and file_part and pos_part.isdigit():
        return True
    hi, slash, lo = s.partition("/")
    if slash and hi and lo:
        try:
            int(hi, 16)
            int(lo, 16)
            return True
        except ValueError:
            return False
    return False


def read_pos(path: str) -> Optional[str]:
    """Return the persisted cursor, or ``None`` iff the file has never existed.

    ``None`` means *fresh extract*: the caller may anchor at the source's current
    coordinate. A file that exists but is blank or malformed is treated as a
    corrupt/truncated cursor and raises :class:`Any2HeliosError` — silently
    re-anchoring from it would skip every change since the last durable cursor.
    """
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        s = f.read().strip()
    if not s:
        raise Any2HeliosError(
            "CDC position file {!r} exists but is empty — a crash likely truncated it "
            "mid-write. Refusing to re-anchor (that would skip every change since the "
            "last cursor and lose data). Restore the file from backup, or delete it to "
            "deliberately re-anchor at the source's CURRENT position (accepting that "
            "changes made while it was gone are not captured).".format(path))
    if not _looks_like_pos(s):
        raise Any2HeliosError(
            "CDC position file {!r} holds a malformed cursor {!r} (expected "
            "'<binlog-file>:<pos>' or an LSN '<hi>/<lo>'). Refusing to resume from a "
            "corrupt coordinate. Restore from backup, or delete it to re-anchor at the "
            "source's CURRENT position (uncaptured changes are lost).".format(path, s))
    return s


def write_pos_atomic(path: str, data: str) -> None:
    """Durably replace the pos file so a crash can never leave it torn.

    Writes a temp file in the same directory, ``fsync``s its contents, then
    ``os.replace``s it over ``path`` (atomic on POSIX). The parent directory is
    ``fsync``ed too so the rename itself survives a crash. On any failure the
    temp file is cleaned up and the original ``path`` is left untouched.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".binlog.pos.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Persist the rename itself: fsync the directory entry (best-effort — some
    # platforms/filesystems disallow opening a directory for fsync).
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass
