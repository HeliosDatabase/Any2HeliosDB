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
from typing import Dict, List, Optional, Tuple

from ...core.change_record import DELETE, INSERT, UPDATE, ChangeRecord
from ...errors import Any2HeliosError

_SLOT_SANITIZE = re.compile(r"[^a-z0-9_]")
# "table <schema>.<rel>: <OP>: <fields>"
_LINE_RE = re.compile(r"^table\s+(.+?):\s+(INSERT|UPDATE|DELETE):\s*(.*)$")
# test_decoding's sentinel for a TOASTed column NOT modified by the UPDATE: it
# emits the bare token below (never quoted) instead of the value, because the
# out-of-line datum was not rewritten. Storing it as the column value would
# clobber the target's real (large) value with this literal string, so we OMIT
# the column and let the replicat do a column-subset update. A genuine text value
# of these exact characters arrives single-quoted, so the bare-token match here
# can never collide with real data.
_UNCHANGED_TOAST = "unchanged-toast-datum"
# A trailing timezone offset on a timestamp literal: '+00', '-0530', '+05:30', 'Z'.
_TZ_SUFFIX = re.compile(r"(?:[+-]\d{2}(?::?\d{2})?|Z)$")


def slot_name(extract_name: str) -> str:
    """A valid PG slot name (<=63 chars, ``[a-z0-9_]``) derived from the extract."""
    return ("a2h_" + _SLOT_SANITIZE.sub("_", extract_name.lower()))[:63]


def lsn_to_int(lsn: str) -> Optional[int]:
    """Encode a PostgreSQL LSN ``'<hi>/<lo>'`` (hex halves) as one comparable int.

    A pg_lsn is a 64-bit WAL byte position printed as two 32-bit hex halves, so
    ``(hi << 32) | lo`` recovers the monotonically-increasing integer the extract
    uses to drop already-trailed changes. Returns ``None`` for an empty/malformed
    value (the caller then leaves ``source_pos`` unset — no dedup, still correct
    under at-least-once).
    """
    hi, slash, lo = (lsn or "").partition("/")
    if not slash:
        return None
    try:
        return (int(hi, 16) << 32) | int(lo, 16)
    except ValueError:
        return None


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
            tok = s[i:j]
            i = j
            # OMIT an unchanged-TOAST column (bare sentinel) so a partial update
            # leaves the target's stored value alone rather than overwriting it.
            if tok == _UNCHANGED_TOAST:
                continue
            out[name] = _coerce(tok, quoted=False, typ=typ)
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
    # INSERT or UPDATE. The key-changed UPDATE form is
    # "old-key: <pre-image> new-tuple: <post-image>". ``key`` always carries the
    # NEW/current identity; when the PK actually moved we also record the old
    # identity in ``before_key`` so the replicat drops the orphaned old-key row.
    before_key: Dict[str, object] = {}
    if "new-tuple:" in rest:
        oldpart, newpart = rest.split("new-tuple:", 1)
        after = parse_fields(newpart)
        # old-key/old-tuple is the pre-image: just the PK under the default
        # REPLICA IDENTITY, or every column under FULL — keep only the key cols.
        old = parse_fields(oldpart.replace("old-key:", "", 1))
        # A PK component the UPDATE did not touch is emitted as an unchanged-TOAST
        # datum and therefore OMITTED from the after-image (parse_fields drops it).
        # Its value is unchanged, so it equals the pre-image's — recover it from
        # the old-key so the record's key (and its insertable after-image) never
        # carries a None PK component. Fail closed if it is unrecoverable.
        _fill_pk_from_old(after, old, pk, schema, tbl)
        key = {k: after[k] for k in pk}
        # A key-changing UPDATE must carry EVERY PK component in its pre-image
        # (old-key), or we cannot identify the row to move. Under REPLICA IDENTITY
        # USING INDEX (a non-PK index) the pre-image carries the index columns, not
        # the PK, so ``{k: old[k] for k in pk if k in old}`` would silently yield a
        # partial/empty before_key -> the old-key row leaks or a duplicate logical
        # row appears. Fail closed with the same actionable REPLICA IDENTITY message
        # as an unrecoverable PK (K3). Under DEFAULT the old-key IS the PK, and under
        # FULL the pre-image is the whole row, so both carry every PK component —
        # this only fires for the genuinely unsafe USING-INDEX(non-PK) config.
        missing_old = [k for k in pk if k not in old]
        before_key = {} if missing_old else {k: old[k] for k in pk}
        if before_key == key:
            before_key = {}
        elif missing_old:
            raise _replica_identity_error(schema, tbl, missing_old)
    else:
        after = parse_fields(rest)
        # Non-key-changing UPDATE under REPLICA IDENTITY DEFAULT: there is no
        # pre-image line, so an unchanged-TOAST (hence omitted) PK component cannot
        # be recovered — fail closed rather than key the row on None.
        _require_full_pk(after, pk, schema, tbl)
        key = {k: after[k] for k in pk}
    return ChangeRecord(op=(INSERT if op == "INSERT" else UPDATE),
                        schema=schema, table=tbl, key=key, after=after, before_key=before_key)


def _replica_identity_error(schema: str, tbl: str, missing: List[str]) -> Any2HeliosError:
    return Any2HeliosError(
        "PostgreSQL CDC: an UPDATE on {}.{} left primary-key column(s) {} as an "
        "unchanged-TOAST datum with no pre-image to recover them from, so the row "
        "cannot be keyed without setting a PK column to NULL (a guaranteed apply "
        "failure). Set REPLICA IDENTITY FULL (or USING INDEX <pk-index>) on that "
        "table so the WAL carries the full key on every UPDATE. NOTE: the "
        "offending change is already decoded in the replication slot, so fixing "
        "the table alone does not unwedge this extract — the slot re-peeks the "
        "same change every run. To unblock, either re-snapshot the table (migrate "
        "it again, then recreate the slot), or advance/recreate the slot past the "
        "poisoned change and accept losing the changes up to that point."
        .format(schema, tbl, missing))


def _fill_pk_from_old(after: Dict[str, object], old: Dict[str, object],
                      pk: List[str], schema: str, tbl: str) -> None:
    """Backfill any PK column omitted from *after* (unchanged TOAST) from *old*.

    Mutates *after* in place. Raises if a PK column is present in neither image —
    a None-keyed record would SET a PK column to NULL (PK violation) and render a
    ``WHERE pk = NULL`` on the partial path.
    """
    missing = [k for k in pk if k not in after and k not in old]
    if missing:
        raise _replica_identity_error(schema, tbl, missing)
    for k in pk:
        if k not in after:
            after[k] = old[k]


def _require_full_pk(after: Dict[str, object], pk: List[str],
                     schema: str, tbl: str) -> None:
    """Raise unless every PK column is present in *after* (no pre-image to fall
    back on, so an omitted — unchanged-TOAST — PK component is unrecoverable)."""
    missing = [k for k in pk if k not in after]
    if missing:
        raise _replica_identity_error(schema, tbl, missing)


class PostgresLogicalSource:
    def __init__(self, adapter, schema, tables, extract_name) -> None:
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

    def capture(self, limit: int = 0) -> Tuple[List[ChangeRecord], str, List[str]]:
        """Peek (non-consuming) pending changes; return records, the highest LSN
        seen (the advance point), and tables skipped for lack of a PK.

        ``limit`` (tier-2 ``capture_batch``) caps
        ``pg_logical_slot_peek_changes``'s ``upto_nchanges``. That cap is checked at
        **transaction boundaries**, not per row: decoding stops once at least
        ``limit`` changes have been emitted AND the current transaction has ended,
        so a run reads *roughly* that many changes but never splits a transaction.
        One huge transaction is therefore still materialized whole (the cap can only
        bound the backlog of *committed transactions* after an outage, not a single
        giant one) — a documented limitation, not an exact ceiling. ``0`` means no
        cap (``NULL`` — the pre-tier-2 behaviour). The slot is only advanced past
        what was captured, so a capped run leaves the remainder for the next one —
        cursor semantics are unchanged.
        """
        self.ensure_slot()
        skipped = sorted(t.name for t in self.tables
                         if not (t.primary_key and t.primary_key.columns))
        records: List[ChangeRecord] = []
        last_lsn = ""
        # ``upto_nchanges`` (3rd arg) caps the peek; NULL = unbounded. A plain int
        # literal is safe to interpolate (validated int, no user text).
        upto = "NULL" if not limit or int(limit) <= 0 else str(int(limit))
        # Several test_decoding change lines can share one LSN within a transaction,
        # so tag each record with a per-base ordinal: the first record at a base
        # keeps the bare int LSN (wire-compatible), later records at the SAME base
        # become ``[base, seq]``. That gives every RECORD a total order (stable
        # across a re-peek since the slot re-delivers the same line order), so a
        # prefix crash within a transaction re-appends only the never-trailed
        # remainder instead of dropping the shared-LSN tail.
        seq_by_base: Dict[int, int] = {}
        for lsn, data in self.adapter._qall(
                "SELECT lsn::text, data FROM pg_logical_slot_peek_changes(%s, NULL, {})".format(upto),
                self.slot):
            if lsn:
                last_lsn = lsn
            rec = parse_change(data, self.schema, self._pk, self._known)
            if rec is not None:
                # Tag the record with its LSN so the engine can drop it on a
                # re-peek if a crash lost the slot advance after the trail append.
                base = lsn_to_int(lsn)
                if base is None:
                    rec.source_pos = None
                else:
                    seq = seq_by_base.get(base, 0)
                    seq_by_base[base] = seq + 1
                    rec.source_pos = base if seq == 0 else [base, seq]
                records.append(rec)
        return records, last_lsn, skipped

    def epoch_identity(self) -> Optional[str]:
        """Identity of the LSN coordinate space: ``<system_identifier>:<timeline>``.

        LSNs are only comparable within one cluster lifetime AND timeline: a
        different cluster (same trail dir pointed elsewhere) has a different
        ``system_identifier``, and a PITR restore of the SAME cluster bumps the
        ``timeline_id`` while rewinding LSNs. Either change means positions in an
        existing trail can no longer be ordered against newly peeked ones — the
        engine fails closed on a mismatch instead of letting dedup silently drop
        genuinely new changes that happen to order at-or-below the stale tail.
        Returns ``None`` when the control functions are unavailable (restricted
        environments); the LSN-order sanity check still applies then.
        """
        try:
            sysid = self.adapter._q1("SELECT system_identifier FROM pg_control_system()")
            tli = self.adapter._q1("SELECT timeline_id FROM pg_control_checkpoint()")
        except Exception:  # noqa: BLE001 - identity probe is best-effort by design
            return None
        if not sysid or not tli:
            return None
        return "{}:{}".format(sysid[0], tli[0])

    def advance(self, lsn: str) -> None:
        """Consume the slot up to *lsn* (called after the batch is durably in the
        trail). No-op when there was nothing to capture."""
        if lsn:
            self.adapter._q1("SELECT pg_replication_slot_advance(%s, %s)", self.slot, lsn)

    def drop_slot(self) -> bool:
        """Remove the slot (cutover/teardown) so it stops pinning WAL.

        Returns ``True`` when a slot was dropped, ``False`` when it was already
        absent. **Raises** on a real failure (e.g. the slot is still ``active`` on
        another connection) rather than swallowing it: a still-WAL-pinning slot must
        not silently vanish from ``a2h extracts`` / ``--lag`` while the caller
        reports success. The caller keys registry removal on this outcome, so a
        failed drop leaves the extract intact and visible."""
        if not self.adapter._q1(
                "SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", self.slot):
            return False  # already gone -> nothing to drop, safe to proceed
        self.adapter._q1("SELECT pg_drop_replication_slot(%s)", self.slot)
        return True

    def lag(self) -> Optional[Dict[str, object]]:
        """Replication lag of this extract's slot, or ``None`` if unavailable.

        Reports the slot's ``confirmed_flush_lsn`` / ``restart_lsn`` against the
        cluster's ``pg_current_wal_lsn()``, plus ``bytes_behind`` — how much WAL
        the slot is still pinning (a large value warns that an abandoned extract is
        holding WAL and risks filling the source disk, the motivation for
        ``a2h extract NAME --drop``). ``None`` when the slot does not exist or the
        control query is not permitted.
        """
        try:
            row = self.adapter._q1(
                "SELECT confirmed_flush_lsn::text, restart_lsn::text FROM "
                "pg_replication_slots WHERE slot_name = %s", self.slot)
            if not row:
                return None
            cur = self.adapter._q1("SELECT pg_current_wal_lsn()::text")
        except Exception:  # noqa: BLE001
            return None
        confirmed, restart = row[0], row[1]
        current = cur[0] if cur else None
        cur_int = lsn_to_int(current) if current else None
        conf_int = lsn_to_int(confirmed) if confirmed else None
        behind = (cur_int - conf_int) if (cur_int is not None and conf_int is not None) else None
        return {"slot": self.slot, "confirmed_flush_lsn": confirmed,
                "restart_lsn": restart, "current_wal_lsn": current,
                "bytes_behind": behind}
