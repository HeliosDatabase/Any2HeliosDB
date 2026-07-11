"""Dead-letter sink for poison change records (tier-2 poison policy).

A single change record the target rejects repeatedly (an unparseable value, a
constraint the row can never satisfy) would otherwise wedge the replicat forever:
the apply cursor can never advance past it. The replicat instead retries such a
record a bounded number of times and then moves it here — an append-only
``dead_letter.jsonl`` beside the trail, written with the same atomic
append+fsync discipline as the trail — logs it loudly with its position / table /
key, advances the cursor past it, and reports the count in the run summary and in
``a2h extracts``.

**Keymoves are never dead-lettered.** Skipping a key-move diverges key state
(the old-key row leaks or the moved row is lost), so a key-move failure fails
closed instead — the engine never routes a key-move here.

**Replays never double-dead-letter — for records that carry a ``source_pos``.** A
crash between the dead-letter append and the cursor write would replay the same
record; :meth:`seen_source_positions` lets the caller skip a record whose
``source_pos`` is already recorded. Records with **no** ``source_pos`` (Oracle
SCN-watermark capture, whose rows carry no per-event coordinate; and any legacy
trail) cannot be deduped this way — the cursor advances past them on the
successful run, so the replay window is only that narrow crash-between-append-and-
cursor-write, but a poison Oracle record hit in it *can* be recorded twice.

The ``cursor`` recorded on each dead-letter is the failing record's global trail
line (passed by the engine), so an operator can locate it in the trail.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional, Set

from ..core.change_record import ChangeRecord


class DeadLetter:
    def __init__(self, trail_dir: str) -> None:
        os.makedirs(trail_dir, exist_ok=True)
        self.path = os.path.join(trail_dir, "dead_letter.jsonl")

    def count(self) -> int:
        if not os.path.exists(self.path):
            return 0
        n = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
        return n

    def seen_source_positions(self) -> Set[str]:
        """The ``source_pos`` values already dead-lettered, normalized to strings
        for hashing (a compound ``[base, seq]`` and an int never collide because
        their JSON encodings differ). Records without a position are not included."""
        seen: Set[str] = set()
        if not os.path.exists(self.path):
            return seen
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                pos = d.get("source_pos")
                if pos is not None:
                    seen.add(json.dumps(pos, separators=(",", ":")))
        return seen

    @staticmethod
    def _pos_key(record: ChangeRecord) -> Optional[str]:
        if record.source_pos is None:
            return None
        return json.dumps(record.source_pos, separators=(",", ":"))

    def append(self, record: ChangeRecord, reason: str, cursor: Optional[int] = None) -> None:
        """Atomically append *record* (with the failure *reason* and, when known,
        the global trail *cursor* it sat at) to the dead-letter file."""
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "reason": reason,
            "cursor": cursor,
            "op": record.op,
            "schema": record.schema,
            "table": record.table,
            "record": json.loads(record.to_json()),
        }
        if record.source_pos is not None:
            entry["source_pos"] = record.source_pos
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())
