"""Durable append-only change trail (one file per extract).

Records are appended as JSON lines and fsync'd before the append returns, so a
committed record survives a crash. The reader is a simple line cursor: reading
from cursor N returns every record after line N and the new line count, which
the replicat persists only *after* a successful apply (at-least-once; combined
with idempotent upserts on the key, effectively-once per row).
"""
from __future__ import annotations

import os
from typing import List, Tuple

from ..core.change_record import ChangeRecord


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
        """Return records after line ``cursor`` and the new cursor (total lines)."""
        if not os.path.exists(self.path):
            return [], cursor
        out: List[ChangeRecord] = []
        n = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for n, line in enumerate(f, start=1):
                if n <= cursor:
                    continue
                line = line.strip()
                if line:
                    out.append(ChangeRecord.from_json(line))
        return out, max(cursor, n)
