"""Normalized change record + a type-preserving JSON codec.

A :class:`ChangeRecord` is the unit that flows source -> trail -> sink, modeled
on HeliosDB's CdcEvent shape (op, schema/table, key, after-image, source
position). v1 capture (SCN-watermark) emits upserts ("U"); deletes ("D") are a
log-based v2 concern.

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
from typing import Dict, List

# op codes
INSERT = "I"
UPDATE = "U"
DELETE = "D"


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

    def to_json(self) -> str:
        return json.dumps({
            "op": self.op, "schema": self.schema, "table": self.table,
            "key": {k: _encode(v) for k, v in self.key.items()},
            "after": {k: _encode(v) for k, v in self.after.items()},
            "scn": self.scn, "commit_ts": self.commit_ts,
        }, separators=(",", ":"))

    @classmethod
    def from_json(cls, line: str) -> "ChangeRecord":
        d = json.loads(line)
        return cls(
            op=d["op"], schema=d["schema"], table=d["table"],
            key={k: _decode(v) for k, v in d.get("key", {}).items()},
            after={k: _decode(v) for k, v in d.get("after", {}).items()},
            scn=int(d.get("scn", 0)), commit_ts=d.get("commit_ts", ""),
        )


def encode_records(records: List[ChangeRecord]) -> str:
    return "".join(r.to_json() + "\n" for r in records)
