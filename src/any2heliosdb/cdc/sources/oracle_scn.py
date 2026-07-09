"""v1 capture: Oracle SCN-watermark.

Captures rows whose ``ORA_ROWSCN`` exceeds the extract's watermark (a full
snapshot on the first cycle, when the watermark is 0) and emits them as upsert
change records. ``ORA_ROWSCN`` is block-granular without ``ROWDEPENDENCIES``, so
this may re-emit unchanged neighbours — harmless because the sink upserts on the
key. Deletes are not visible to a watermark scan; they are a log-based (v2)
concern. This is the guaranteed-portable Oracle "CDC" for shops without
LogMiner/supplemental-logging access.
"""
from __future__ import annotations

from typing import List, Tuple

from ..registry import Extract  # noqa: F401  (type reference for callers)
from ...core.change_record import UPDATE, ChangeRecord


class OracleScnSource:
    def __init__(self, adapter, schema, tables) -> None:
        self.adapter = adapter
        self.schema = schema
        self.tables = tables

    def capture(self, since_scn: int) -> Tuple[List[ChangeRecord], int, List[str]]:
        # Anchor the new watermark *before* scanning, so concurrent commits during
        # the scan are picked up next cycle rather than skipped.
        start_scn = self.adapter.current_scn()
        records: List[ChangeRecord] = []
        skipped: List[str] = []
        for t in self.tables:
            if not (t.primary_key and t.primary_key.columns):
                skipped.append(t.name)
                continue
            cols = [c.name for c in t.columns]
            where = None if since_scn <= 0 else "ORA_ROWSCN > {}".format(int(since_scn))
            for row in self.adapter.stream_rows(t, cols, where=where):
                after = {col: row[i] for i, col in enumerate(cols)}
                key = {pk: after[pk] for pk in t.primary_key.columns}
                records.append(ChangeRecord(op=UPDATE, schema=self.schema, table=t.name,
                                            key=key, after=after, scn=start_scn))
        new_watermark = start_scn if start_scn > 0 else since_scn
        return records, new_watermark, skipped
