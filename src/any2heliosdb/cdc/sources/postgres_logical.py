"""v1 PostgreSQL CDC capture: logical decoding (``test_decoding``).

True log-based change capture for a PostgreSQL source. A logical replication
slot decodes the WAL into an ordered stream of INSERT / UPDATE / **DELETE**
change records — so, unlike the Oracle SCN-watermark source, deletes are
observed natively and a full CRUD workload replicates without the key-set
reconcile pass.

Design choices that keep this in line with a2h's light-driver philosophy:

* **``test_decoding`` output plugin** — built into every PostgreSQL contrib
  install, so no extension (``wal2json``) or superuser-extension step is needed.
* **Read over plain SQL** via ``pg_logical_slot_peek_changes`` — no
  streaming-replication protocol, just queries on the existing psycopg
  connection. We *peek* (non-consuming), let the engine persist the batch to the
  durable trail, then :meth:`advance` the slot — so a crash mid-extract re-reads
  rather than loses changes (trail-first durability).

Requirements on the source: ``wal_level = logical``, a role that may create a
logical slot (REPLICATION or superuser), and a primary key on each table (the
default ``REPLICA IDENTITY`` so UPDATE/DELETE carry the key). Create the slot
*before* the initial load so changes during/after the snapshot are captured —
``a2h extract`` on its first run creates it.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Tuple

from ...core.change_record import DELETE, INSERT, UPDATE, ChangeRecord

_SLOT_SANITIZE = re.compile(r"[^a-z0-9_]")
# "table <schema>.<rel>: <OP>: <fields>"
_LINE_RE = re.compile(r"^table\s+(.+?):\s+(INSERT|UPDATE|DELETE):\s*(.*)$")
# A trailing timezone offset on a timestamp literal: '+00', '-0530', '+05:30', 'Z'.
_TZ_SUFFIX = re.compile(r"(?:[+-]\d{2}(?::?\d{2})?|Z)$")


def slot_name(extract_name: str) -> str:
    """A valid PG slot name (<=63 chars, ``[a-z0-9_]``) derived from the extract."""
    return ("a2h_" + _SLOT_SANITIZE.sub("_", extract_name.lower()))[:63]


def _strip_ident(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('""', '"')
    return s


def _coerce(text: str, quoted: bool, typ: str = "") -> object:
    """test_decoding renders values as either a single-quoted string or a bare
    token. Bare tokens carry type information we can recover (null / boolean /
    number); quoted strings are returned verbatim (the tokenizer already removed
    the quotes and un-escaped ``''``). Anything we can't classify stays a str —
    psycopg sends it text-typed and the target casts it to the column type.

    ``typ`` is the column type from the decode stream. ``timestamptz`` values
    arrive with a zone offset (``…+00``); HeliosDB stores them as a plain
    ``TIMESTAMP`` (it downgrades ``WITH TIME ZONE``) and its literal cast rejects
    the offset, so we strip it — matching what the bulk COPY migrate stored. This
    only fires for timestamp-typed columns, never plain text that ends in ``+00``."""
    if quoted:
        if "timestamp" in typ.lower():
            return _TZ_SUFFIX.sub("", text).strip()
        return text
    if text == "null":
        return None
    if text in ("t", "true"):
        return True
    if text in ("f", "false"):
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return Decimal(text)
    except InvalidOperation:
        return text


def parse_fields(s: str) -> Dict[str, object]:
    """Parse a test_decoding field list ``name[type]:value name2[type2]:value2``
    into ``{column: value}``. Handles double-quoted identifiers, bracketed type
    names that contain spaces (``character varying``), single-quoted string
    values with ``''`` escapes, and bare tokens."""
    out: Dict[str, object] = {}
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] == " ":
            i += 1
        if i >= n:
            break
        # column name (optionally double-quoted)
        if s[i] == '"':
            j = i + 1
            while j < n:
                if s[j] == '"':
                    if j + 1 < n and s[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            name = s[i + 1:j].replace('""', '"')
            i = j + 1
        else:
            j = i
            while j < n and s[j] != "[":
                j += 1
            name = s[i:j]
            i = j
        if i >= n or s[i] != "[":
            break
        # bracketed type (captured; brackets may nest for array types)
        tstart = i + 1
        depth = 0
        while i < n:
            if s[i] == "[":
                depth += 1
            elif s[i] == "]":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        typ = s[tstart:i]
        i += 1  # past the closing ']'
        if i < n and s[i] == ":":
            i += 1
        # value: single-quoted string or bare token
        if i < n and s[i] == "'":
            j = i + 1
            buf: List[str] = []
            while j < n:
                if s[j] == "'":
                    if j + 1 < n and s[j + 1] == "'":
                        buf.append("'")
                        j += 2
                        continue
                    break
                buf.append(s[j])
                j += 1
            out[name] = _coerce("".join(buf), quoted=True, typ=typ)
            i = j + 1
        else:
            j = i
            while j < n and s[j] != " ":
                j += 1
            out[name] = _coerce(s[i:j], quoted=False, typ=typ)
            i = j
    return out


def parse_change(data: str, schema: str, pk_by_table: Dict[str, List[str]],
                 known_tables) -> "ChangeRecord | None":
    """Parse one ``test_decoding`` output line into a ChangeRecord, or None for a
    BEGIN/COMMIT/unknown-table line. ``known_tables`` is a set of UPPER names to
    keep; ``pk_by_table`` maps UPPER table name -> primary-key column list."""
    m = _LINE_RE.match(data or "")
    if not m:
        return None
    rel, op, rest = m.group(1), m.group(2), m.group(3)
    tbl = _strip_ident(rel.rsplit(".", 1)[-1])
    up = tbl.upper()
    if up not in known_tables:
        return None
    pk = pk_by_table.get(up)
    if not pk:
        return None
    if op == "DELETE":
        return ChangeRecord(op=DELETE, schema=schema, table=tbl, key=parse_fields(rest))
    # INSERT or UPDATE. The key-changed UPDATE form is "old-key: ... new-tuple: ...".
    if "new-tuple:" in rest:
        oldpart, newpart = rest.split("new-tuple:", 1)
        after = parse_fields(newpart)
        key = parse_fields(oldpart.replace("old-key:", "", 1)) or {k: after.get(k) for k in pk}
    else:
        after = parse_fields(rest)
        key = {k: after.get(k) for k in pk}
    return ChangeRecord(op=(INSERT if op == "INSERT" else UPDATE),
                        schema=schema, table=tbl, key=key, after=after)


class PostgresLogicalSource:
    def __init__(self, adapter, schema, tables, extract_name) -> None:  # type: ignore[no-untyped-def]
        self.adapter = adapter
        self.schema = schema
        self.tables = tables
        self.slot = slot_name(extract_name)
        self._pk = {t.name.upper(): list(t.primary_key.columns)
                    for t in tables if t.primary_key and t.primary_key.columns}
        self._known = {t.name.upper() for t in tables}

    def ensure_slot(self) -> bool:
        """Create the logical slot if absent. Returns True if newly created. Must
        run before the initial load so the snapshot window's changes are kept."""
        if self.adapter._q1("SELECT 1 FROM pg_replication_slots WHERE slot_name = %s",
                            self.slot):
            return False
        self.adapter._q1("SELECT pg_create_logical_replication_slot(%s, 'test_decoding')",
                         self.slot)
        return True

    def capture(self) -> Tuple[List[ChangeRecord], str, List[str]]:
        """Peek (non-consuming) all pending changes; return records, the highest
        LSN seen (the advance point), and tables skipped for lack of a PK."""
        self.ensure_slot()
        skipped = sorted(t.name for t in self.tables
                         if not (t.primary_key and t.primary_key.columns))
        records: List[ChangeRecord] = []
        last_lsn = ""
        for lsn, data in self.adapter._qall(
                "SELECT lsn::text, data FROM pg_logical_slot_peek_changes(%s, NULL, NULL)",
                self.slot):
            if lsn:
                last_lsn = lsn
            rec = parse_change(data, self.schema, self._pk, self._known)
            if rec is not None:
                records.append(rec)
        return records, last_lsn, skipped

    def advance(self, lsn: str) -> None:
        """Consume the slot up to *lsn* (called after the batch is durably in the
        trail). No-op when there was nothing to capture."""
        if lsn:
            self.adapter._q1("SELECT pg_replication_slot_advance(%s, %s)", self.slot, lsn)

    def drop_slot(self) -> None:
        """Remove the slot (cutover/teardown) so it stops pinning WAL."""
        try:
            self.adapter._q1("SELECT pg_drop_replication_slot(%s)", self.slot)
        except Exception:  # noqa: BLE001
            pass
