"""Normalized change record + a type-preserving JSON codec.

A :class:`ChangeRecord` is the unit that flows source -> trail -> sink, modeled
on HeliosDB's CdcEvent shape (op, schema/table, key, after-image, source
position). The Oracle SCN-watermark source emits upserts ("U") only; the
log-based sources (MySQL binlog, PostgreSQL logical decoding) emit real inserts
("I"), updates ("U") and deletes ("D"). A PK-changing UPDATE additionally carries
``before_key`` (the row's old identity) so the replicat can drop the orphaned
old-key row.

Oracle hands back ``Decimal``, ``datetime``, and ``bytes`` (LOB/RAW) values that
plain JSON cannot round-trip, so values are tagged on encode and rebuilt on
decode, preserving the exact type the target driver needs to bind.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, List, Optional, Tuple, Union

# op codes
INSERT = "I"
UPDATE = "U"
DELETE = "D"

# A source position is either a plain int (a singleton event/txn coordinate) or a
# compound ``[base, seq]`` where ``seq`` is the record's ordinal within that base
# coordinate — rows of one binlog event, or lines sharing an LSN within one PG
# transaction, share the ``base`` and are distinguished by ``seq``.
SourcePos = Union[int, List[int]]


def source_pos_key(pos: object) -> Optional[Tuple[int, int]]:
    """Total order over source positions for extract-start dedup.

    Normalizes both shapes to a ``(base, seq)`` tuple so a legacy/singleton int
    ``p`` orders identically to a compound ``[p, 0]`` — backward compatible with
    round-4 int-tagged trails and legacy untagged ones. This total order is what
    lets a prefix-crash within one multi-row event (all rows sharing ``base``)
    re-append only the never-trailed remainder: with a plain-int coordinate the
    shared ``base`` made ``<= last`` drop the tail rows forever. ``None`` (a record
    with no position) has no key and is never dropped by the caller.
    """
    if pos is None:
        return None
    if isinstance(pos, (list, tuple)):
        return (int(pos[0]), int(pos[1]))
    if isinstance(pos, bool):  # bool is an int subclass; never a valid position
        return None
    if isinstance(pos, int):
        return (pos, 0)
    return None


def _encode(v: object) -> object:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Decimal):
        return {"__t__": "dec", "v": str(v)}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"__t__": "b64", "v": base64.b64encode(bytes(v)).decode("ascii")}
    if isinstance(v, _dt.datetime):
        return {"__t__": "ts", "v": v.isoformat()}
    if isinstance(v, _dt.date):
        return {"__t__": "d", "v": v.isoformat()}
    return {"__t__": "str", "v": str(v)}


def _decode(v: object) -> object:
    if not isinstance(v, dict) or "__t__" not in v:
        return v
    t, raw = v["__t__"], v["v"]
    if t == "dec":
        return Decimal(raw)
    if t == "b64":
        return base64.b64decode(raw)
    if t == "ts":
        return _dt.datetime.fromisoformat(raw)
    if t == "d":
        return _dt.date.fromisoformat(raw)
    return raw


@dataclass
class ChangeRecord:
    op: str
    schema: str
    table: str
    key: Dict[str, object] = field(default_factory=dict)
    after: Dict[str, object] = field(default_factory=dict)
    scn: int = 0
    commit_ts: str = ""
    # Primary-key of the row *before* this change, when the change moved the row
    # to a different key (a PK-changing UPDATE). ``key`` always carries the row's
    # new/current identity; ``before_key`` is set only when the old identity
    # differs, so the replicat can delete the orphaned old-key row. Empty on every
    # other record. Serialized only when present, so old trails (and every
    # non-PK-changing record) stay byte-for-byte as before and old readers that
    # ignore the field still parse.
    before_key: Dict[str, object] = field(default_factory=dict)
    # Monotonic source position this change was captured at, as a single
    # comparable coordinate per source (PostgreSQL LSN = ``(hi << 32) | lo``; MySQL
    # binlog = ``(file-index << 48) | log_pos``; Oracle SCN = the SCN itself).
    # It is a plain int for a singleton, or a compound ``[base, seq]`` when several
    # records share one base coordinate (rows of a multi-row binlog event; lines
    # sharing an LSN in one PG transaction) so every RECORD is totally ordered —
    # compare via :func:`source_pos_key`. The extract uses it to drop events
    # already durably in the trail after a crash between ``trail.append`` and the
    # position write (which would otherwise re-read and re-append them as duplicate
    # lines). ``None`` when the source cannot supply a per-event position;
    # serialized only when present, so legacy trails and readers that predate the
    # field are unaffected.
    source_pos: Optional[SourcePos] = None

    def to_json(self) -> str:
        d = {
            "op": self.op, "schema": self.schema, "table": self.table,
            "key": {k: _encode(v) for k, v in self.key.items()},
            "after": {k: _encode(v) for k, v in self.after.items()},
            "scn": self.scn, "commit_ts": self.commit_ts,
        }
        if self.before_key:
            d["before_key"] = {k: _encode(v) for k, v in self.before_key.items()}
        if self.source_pos is not None:
            d["source_pos"] = self.source_pos
        return json.dumps(d, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "ChangeRecord":
        d = json.loads(line)
        return cls(
            op=d["op"], schema=d["schema"], table=d["table"],
            key={k: _decode(v) for k, v in d.get("key", {}).items()},
            after={k: _decode(v) for k, v in d.get("after", {}).items()},
            scn=int(d.get("scn", 0)), commit_ts=d.get("commit_ts", ""),
            before_key={k: _decode(v) for k, v in d.get("before_key", {}).items()},
            source_pos=d.get("source_pos"),
        )


def encode_records(records: List[ChangeRecord]) -> str:
    return "".join(r.to_json() + "\n" for r in records)
