"""MySQL binlog CDC capture (log-based).

Reads ROW-format binlog events (`mysql-replication`) and turns them into
`ChangeRecord`s — real inserts/updates **and deletes**, unlike the SCN-watermark
source. The capture cursor is the binlog coordinate ``"<file>:<pos>"``; on the
first cycle it anchors at the server's *current* position (so only changes after
the baseline load are captured) and returns no records.

Requires the source MySQL to have ``log_bin=ON`` and ``binlog_format=ROW``, and
the connecting user to hold ``REPLICATION SLAVE``/``REPLICATION CLIENT``.

Correctness depends on **full** row images and column metadata: with
``binlog_row_image=MINIMAL`` an UPDATE only logs the changed columns (the
replicat would then NULL the omitted ones), and with
``binlog_row_metadata=MINIMAL`` events carry ``UNKNOWN_COL0..`` placeholders
instead of real column names. The source therefore fails closed — it verifies
``binlog_format=ROW``, ``binlog_row_metadata=FULL`` and ``binlog_row_image=FULL``
before anchoring/capture, and rejects any captured event whose after-image does
not cover the table's columns.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from ...core.change_record import DELETE, INSERT, UPDATE, ChangeRecord
from ...errors import Any2HeliosError

# Server variables that must be FULL/ROW for log-based CDC to be lossless.
_REQUIRED_VARS = (
    ("binlog_format", "ROW",
     "binlog row events are required; STATEMENT/MIXED don't carry per-row before/after images"),
    ("binlog_row_metadata", "FULL",
     "MINIMAL omits column names, so events expose UNKNOWN_COL0.. instead of real columns"),
    ("binlog_row_image", "FULL",
     "MINIMAL logs only changed columns on UPDATE, so unchanged columns would be written as NULL"),
)


def binlog_pos_to_int(log_file: str, log_pos: int) -> int:
    """Encode a binlog coordinate ``(<file>, <pos>)`` as one comparable integer.

    A binlog coordinate advances by rolling to a higher-numbered file
    (``mysql-bin.000009`` -> ``.000010``) and, within a file, by increasing byte
    offset. ``(file-index << 48) | log_pos`` preserves that lexicographic order
    (a binlog file caps well under 2**48 bytes), giving the extract a single
    monotonically-increasing value to drop already-trailed events against.

    Fails closed on a file name without a numeric sequence suffix: degrading it to
    index 0 does NOT preserve order across a basename change — a new file's events
    would then encode BELOW the trail tail and be silently dropped as
    "already-trailed" (genuine data loss). So refuse to guess rather than
    over-drop.
    """
    _, _, num = (log_file or "").rpartition(".")
    try:
        idx = int(num)
    except ValueError:
        raise Any2HeliosError(
            "MySQL CDC: binlog file {!r} has no numeric sequence suffix (expected "
            "'<base>.<NNNNNN>', e.g. 'mysql-bin.000009'), so its coordinate cannot be "
            "encoded as a monotonic dedup position. Falling back to index 0 could encode "
            "a later file's new events below the trail tail and silently drop them, so "
            "refusing to guess. Check the source's binlog file naming.".format(log_file))
    return (idx << 48) | int(log_pos)


def _require_full_row_image(settings: Dict[str, str]) -> None:
    """Raise unless ROW binlog with FULL metadata + FULL row image is in effect.

    ``settings`` maps server-variable name -> value (case-insensitively), as read
    from ``SHOW VARIABLES``. Fails closed with a clear, actionable message naming
    the offending variable rather than letting capture silently corrupt the
    target. Pure (no I/O) so it can be unit-tested with plain dicts.
    """
    norm = {str(k).lower(): ("" if v is None else str(v)) for k, v in settings.items()}
    for var, want, why in _REQUIRED_VARS:
        got = norm.get(var.lower())
        if got is None:
            raise Any2HeliosError(
                "MySQL CDC: could not read '{}' (need {}={}). {}. Grant the connecting "
                "user access to read server variables, or set it server-side.".format(
                    var, var, want, why))
        if got.strip().upper() != want:
            raise Any2HeliosError(
                "MySQL CDC requires {}={} but the server reports {}={!r}. {}. Fix it with "
                "`SET GLOBAL {}={}` (needs SYSTEM_VARIABLES_ADMIN; for binlog_format also "
                "restart replication threads) or set it in my.cnf and restart.".format(
                    var, want, var, got, why, var, want))


def _check_image_columns(table: str, op: str, image_keys, expected_cols) -> None:
    """Raise if a captured row image is missing columns or carries placeholders.

    A FULL row image/metadata stream names every column; a MINIMAL one drops
    unchanged columns and/or surfaces ``UNKNOWN_COL0..`` keys. Either case would
    have the replicat write NULLs over real data, so reject the event loudly.
    ``image_keys`` is the dict-key set of the captured value map; ``expected_cols``
    is the table's full column list from the source schema.
    """
    have = set(image_keys)
    bad = sorted(k for k in have if str(k).upper().startswith("UNKNOWN_COL"))
    if bad:
        raise Any2HeliosError(
            "MySQL CDC: {} on {} carried unnamed columns {} — binlog_row_metadata is not "
            "FULL. Set binlog_row_metadata=FULL on the source and re-anchor.".format(
                op, table, bad))
    missing = [c for c in expected_cols if c not in have]
    if missing:
        raise Any2HeliosError(
            "MySQL CDC: {} on {} omitted columns {} from its row image — binlog_row_image "
            "is not FULL (partial images would be written as NULL). Set "
            "binlog_row_image=FULL on the source and re-anchor.".format(op, table, missing))


class MySqlBinlogSource:
    def __init__(self, dsn, schema, tables, server_id: int = 4279):
        self.dsn = dsn
        self.schema = schema
        self.server_id = server_id
        self._pk = {t.name: (list(t.primary_key.columns) if t.primary_key else []) for t in tables}
        self._cols = {t.name: [c.name for c in t.columns] for t in tables}
        self._tables = [t.name for t in tables]

    def _conn_settings(self) -> dict:
        return {"host": self.dsn.host, "port": int(self.dsn.port),
                "user": self.dsn.user, "passwd": self.dsn.password or ""}

    @staticmethod
    def _read_row_image_vars(cur) -> Dict[str, str]:
        """Read the binlog_* variables that gate lossless capture into a dict."""
        out: Dict[str, str] = {}
        for var, _want, _why in _REQUIRED_VARS:
            try:
                cur.execute("SHOW VARIABLES LIKE %s", (var,))
                row = cur.fetchone()
            except Exception:  # noqa: BLE001
                row = None
            if row:
                # SHOW VARIABLES returns (Variable_name, Value).
                out[str(row[0])] = "" if row[1] is None else str(row[1])
        return out

    def current_position(self) -> str:
        import pymysql

        c = pymysql.connect(**{k: v for k, v in self._conn_settings().items() if k != "passwd"},
                            password=self.dsn.password or "")
        try:
            cur = c.cursor()
            # Binlog row events only carry column *names* when row metadata is FULL
            # (the default MINIMAL yields UNKNOWN_COL0..). Set it best-effort so
            # events written after this anchor map to real column names. Requires
            # SYSTEM_VARIABLES_ADMIN; if denied, set it server-side (a documented
            # prerequisite alongside log_bin=ON / binlog_format=ROW).
            try:
                cur.execute("SET GLOBAL binlog_row_metadata = FULL")
            except Exception:  # noqa: BLE001
                pass
            # Fail closed: anchoring here means every later cycle resumes from this
            # coordinate, so the row-image guarantees must already hold *now*.
            # (binlog_row_image can't be fixed by a SET that only takes effect for
            # new sessions, so verify rather than assume the best-effort SET stuck.)
            _require_full_row_image(self._read_row_image_vars(cur))
            for q in ("SHOW BINARY LOG STATUS", "SHOW MASTER STATUS"):  # 8.4 renamed it
                try:
                    cur.execute(q)
                    row = cur.fetchone()
                    if row:
                        return "{}:{}".format(row[0], row[1])
                except Exception:  # noqa: BLE001
                    continue
            return ""
        finally:
            c.close()

    def capture(self, position: str) -> Tuple[List[ChangeRecord], str]:
        if not position:
            # First cycle: anchor at the current position, capture nothing yet.
            return [], self.current_position()

        from pymysqlreplication import BinLogStreamReader
        from pymysqlreplication.row_event import (
            DeleteRowsEvent,
            UpdateRowsEvent,
            WriteRowsEvent,
        )

        log_file, _, log_pos = position.rpartition(":")
        stream = BinLogStreamReader(
            connection_settings=self._conn_settings(), server_id=self.server_id,
            only_schemas=[self.schema], only_tables=self._tables,
            only_events=[WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent],
            log_file=log_file, log_pos=int(log_pos), resume_stream=True, blocking=False)
        records: List[ChangeRecord] = []
        try:
            for ev in stream:
                tbl = ev.table
                pk = self._pk.get(tbl, [])
                expected = self._cols.get(tbl, [])
                # The reader advances log_file/log_pos to this event's END as it
                # yields it, so this base coordinate tags every record the event
                # emits. Rows of one multi-row event share the base; each row also
                # carries its ordinal ``i`` so every RECORD is totally ordered as
                # ``[base, i]`` (a singleton event keeps the bare int for wire
                # compat). Extract-start dedup compares this total order against the
                # last COMPLETE trailed record, so a prefix crash that trailed only
                # some rows of the event re-appends exactly the missing tail rather
                # than dropping them (all-share-one-int would drop the never-trailed
                # remainder forever).
                base = binlog_pos_to_int(stream.log_file, stream.log_pos)
                rows = ev.rows
                multi = len(rows) > 1
                for i, row in enumerate(rows):
                    pos = [base, i] if multi else base
                    if isinstance(ev, WriteRowsEvent):
                        vals = row["values"]
                        _check_image_columns(tbl, "INSERT", vals.keys(), expected)
                        records.append(ChangeRecord(op=INSERT, schema=self.schema, table=tbl,
                                                    key={k: vals.get(k) for k in pk}, after=dict(vals),
                                                    source_pos=pos))
                    elif isinstance(ev, UpdateRowsEvent):
                        vals = row["after_values"]
                        _check_image_columns(tbl, "UPDATE", vals.keys(), expected)
                        new_key = {k: vals.get(k) for k in pk}
                        # A PK-changing UPDATE moves the row to a new key; the
                        # before-image (FULL row image is enforced) carries the old
                        # key, so record it as before_key when it differs. The
                        # replicat then moves the orphaned old-key row (otherwise it
                        # leaks). Fail closed on a missing before-image: without it a
                        # keymove's old key would be all-NULL and leak the old row
                        # (the FULL row-image guard should make this unreachable —
                        # belt-and-braces).
                        before = row.get("before_values")
                        if pk and not before:
                            raise Any2HeliosError(
                                "MySQL CDC: UPDATE on {} carried no before-image, so a "
                                "primary-key change cannot be identified (the old key would "
                                "be all-NULL and leak the old row). This needs "
                                "binlog_row_image=FULL; set it on the source and "
                                "re-anchor.".format(tbl))
                        old_key = {k: before.get(k) for k in pk} if before else {}
                        records.append(ChangeRecord(
                            op=UPDATE, schema=self.schema, table=tbl,
                            key=new_key, after=dict(vals),
                            before_key=(old_key if pk and old_key != new_key else {}),
                            source_pos=pos))
                    elif isinstance(ev, DeleteRowsEvent):
                        vals = row["values"]
                        # Delete only needs a sound key, but UNKNOWN_COL / missing PK
                        # still signals a non-FULL image, so verify the key columns.
                        _check_image_columns(tbl, "DELETE", vals.keys(), pk)
                        records.append(ChangeRecord(op=DELETE, schema=self.schema, table=tbl,
                                                    key={k: vals.get(k) for k in pk}, after={},
                                                    source_pos=pos))
            new_pos = "{}:{}".format(stream.log_file, stream.log_pos)
        finally:
            stream.close()
        return records, new_pos
