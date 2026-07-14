"""Unit tests for the PostgreSQL logical-decoding (test_decoding) CDC parser."""
from __future__ import annotations

from decimal import Decimal

import pytest

from any2heliosdb.cdc.sources.postgres_logical import (
    parse_change, parse_fields, slot_name)
from any2heliosdb.core.change_record import DELETE, INSERT, UPDATE
from any2heliosdb.errors import Any2HeliosError

PK = {"ACTOR": ["actor_id"], "X": ["id"]}
KNOWN = {"ACTOR", "X"}

# A composite-PK table (K3): the parser must never let a PK component become None
# when its (unchanged, TOASTed) value is omitted from the UPDATE's after-image.
CPK = {"DOC": ["tenant", "docid"]}
CKNOWN = {"DOC"}


def test_slot_name_sanitized():
    assert slot_name("pagila_cdc") == "a2h_pagila_cdc"
    assert slot_name("My-CDC.1") == "a2h_my_cdc_1"
    assert len(slot_name("z" * 200)) <= 63


def test_parse_fields_types_and_quoting():
    s = ("actor_id[integer]:201 first_name[character varying]:'Penny' "
         "last_name[character varying]:'O''Brien' "
         "last_update[timestamp without time zone]:'2026-06-28 10:00:00' "
         "active[boolean]:t amount[numeric]:9.99 note[text]:null")
    f = parse_fields(s)
    assert f["actor_id"] == 201 and isinstance(f["actor_id"], int)
    assert f["first_name"] == "Penny"
    assert f["last_name"] == "O'Brien"                       # '' un-escaped
    assert f["last_update"] == "2026-06-28 10:00:00"         # spaces+colons inside quotes
    assert f["active"] is True
    assert f["amount"] == Decimal("9.99")
    assert f["note"] is None


def test_parse_fields_strips_tz_offset_only_for_timestamps():
    # HeliosDB downgrades timestamptz to a plain TIMESTAMP and its literal cast
    # rejects a zone offset, so timestamp values are normalized to offset-free.
    f = parse_fields("ts[timestamp with time zone]:'2026-06-28 05:52:42.692688+00' "
                     "ts2[timestamp with time zone]:'2026-06-28 05:52:42+05:30' "
                     "label[text]:'ends+00' n[integer]:5")
    assert f["ts"] == "2026-06-28 05:52:42.692688"     # +00 stripped
    assert f["ts2"] == "2026-06-28 05:52:42"           # +05:30 stripped
    assert f["label"] == "ends+00"                      # text untouched
    assert f["n"] == 5


def test_parse_change_insert_update_delete():
    ins = parse_change(
        "table public.actor: INSERT: actor_id[integer]:201 "
        "first_name[character varying]:'Penny'", "public", PK, KNOWN)
    assert ins is not None and ins.op == INSERT and ins.table == "actor"
    assert ins.key == {"actor_id": 201}
    assert ins.after["first_name"] == "Penny"

    upd = parse_change(
        "table public.actor: UPDATE: actor_id[integer]:201 "
        "first_name[character varying]:'Penelope'", "public", PK, KNOWN)
    assert upd is not None and upd.op == UPDATE
    assert upd.key == {"actor_id": 201} and upd.after["first_name"] == "Penelope"

    dele = parse_change("table public.actor: DELETE: actor_id[integer]:201",
                        "public", PK, KNOWN)
    assert dele is not None and dele.op == DELETE
    assert dele.key == {"actor_id": 201} and dele.after == {}


def test_parse_change_skips_txn_and_unknown_tables():
    assert parse_change("BEGIN 553", "public", PK, KNOWN) is None
    assert parse_change("COMMIT 553", "public", PK, KNOWN) is None
    assert parse_change("table public.payment: INSERT: payment_id[integer]:1",
                        "public", PK, KNOWN) is None       # not in KNOWN


def test_parse_change_key_changed_update_form():
    # A PK-changing UPDATE emits "old-key: <old pk> new-tuple: <new image>". The
    # record's key is the NEW identity (from new-tuple); the OLD identity is
    # carried in before_key so the replicat can delete the orphaned old-key row.
    rec = parse_change(
        "table public.actor: UPDATE: old-key: actor_id[integer]:201 "
        "new-tuple: actor_id[integer]:202 first_name[character varying]:'New'",
        "public", PK, KNOWN)
    assert rec is not None and rec.op == UPDATE
    assert rec.key == {"actor_id": 202}                 # new identity
    assert rec.before_key == {"actor_id": 201}          # old identity to delete
    assert rec.after["actor_id"] == 202 and rec.after["first_name"] == "New"


def test_parse_change_replica_identity_full_non_key_update_has_no_before_key():
    # Under REPLICA IDENTITY FULL every UPDATE emits old-key/new-tuple with the
    # full pre-image, but when the PK did NOT change there is nothing to delete —
    # before_key must be empty (only the changed-PK case carries it).
    rec = parse_change(
        "table public.actor: UPDATE: old-key: actor_id[integer]:201 "
        "first_name[character varying]:'Old' new-tuple: actor_id[integer]:201 "
        "first_name[character varying]:'New'",
        "public", PK, KNOWN)
    assert rec is not None and rec.op == UPDATE
    assert rec.key == {"actor_id": 201}
    assert rec.before_key == {}
    assert rec.after["first_name"] == "New"


def test_parse_fields_omits_unchanged_toast_datum():
    # test_decoding emits the bare 'unchanged-toast-datum' sentinel for a TOASTed
    # column left untouched by an UPDATE. It must be OMITTED (not stored as the
    # literal string), so the replicat leaves the target's real value alone.
    f = parse_fields("id[integer]:5 body[text]:unchanged-toast-datum note[text]:'kept'")
    assert "body" not in f                       # omitted, never the literal marker
    assert f["id"] == 5 and f["note"] == "kept"


def test_parse_fields_keeps_quoted_lookalike_toast_string():
    # A genuine text value equal to the sentinel arrives single-quoted and MUST be
    # preserved — only the bare token is the marker.
    f = parse_fields("id[integer]:5 body[text]:'unchanged-toast-datum'")
    assert f["body"] == "unchanged-toast-datum"


def test_parse_change_update_omits_unchanged_toast_column():
    rec = parse_change(
        "table public.actor: UPDATE: actor_id[integer]:201 "
        "first_name[character varying]:'Penny' bio[text]:unchanged-toast-datum",
        "public", PK, KNOWN)
    assert rec is not None and rec.op == UPDATE
    assert rec.key == {"actor_id": 201}
    assert "bio" not in rec.after                # omitted -> partial (column-subset) update
    assert rec.after["first_name"] == "Penny"


# --- K3: composite-PK with an unchanged-TOAST key component -------------------


def test_parse_change_composite_pk_keymove_recovers_omitted_pk_from_old_key():
    # A keymove that changes docid while leaving the TOASTed `tenant` key component
    # untouched: test_decoding omits `tenant` from the new-tuple (unchanged TOAST).
    # The recovered value comes from the old-key pre-image, so neither the record
    # key NOR its insertable after-image ever carries a None PK component.
    rec = parse_change(
        "table public.doc: UPDATE: old-key: tenant[text]:'acme' docid[integer]:1 "
        "new-tuple: docid[integer]:2 tenant[text]:unchanged-toast-datum body[text]:'x'",
        "public", CPK, CKNOWN)
    assert rec is not None and rec.op == UPDATE
    assert rec.key == {"tenant": "acme", "docid": 2}        # no None component
    assert rec.before_key == {"tenant": "acme", "docid": 1}  # PK moved -> old identity
    assert rec.after["tenant"] == "acme"                    # backfilled -> insertable
    assert rec.after["docid"] == 2 and rec.after["body"] == "x"


def test_parse_change_composite_pk_keymove_unchanged_tenant_only_docid_moves():
    # Symmetric: the moving component is present, the unchanged component recovered.
    rec = parse_change(
        "table public.doc: UPDATE: old-key: tenant[text]:'acme' docid[integer]:7 "
        "new-tuple: tenant[text]:unchanged-toast-datum docid[integer]:8",
        "public", CPK, CKNOWN)
    assert rec.key == {"tenant": "acme", "docid": 8}
    assert rec.before_key == {"tenant": "acme", "docid": 7}
    assert rec.after["tenant"] == "acme"


def test_parse_change_composite_pk_non_key_update_omitted_pk_fails_closed():
    # Non-key-changing UPDATE under REPLICA IDENTITY DEFAULT: no pre-image line, so
    # an omitted (unchanged-TOAST) PK component is unrecoverable -> fail closed with
    # an actionable REPLICA IDENTITY FULL message, never a None-keyed record.
    with pytest.raises(Any2HeliosError) as ei:
        parse_change(
            "table public.doc: UPDATE: docid[integer]:1 "
            "tenant[text]:unchanged-toast-datum body[text]:'y'",
            "public", CPK, CKNOWN)
    msg = str(ei.value)
    assert "REPLICA IDENTITY FULL" in msg and "tenant" in msg


def test_parse_change_composite_pk_keymove_unrecoverable_pk_fails_closed():
    # Even in the keymove form, if the pre-image itself lacks a PK component that is
    # also omitted from the after-image, it is unrecoverable -> fail closed.
    with pytest.raises(Any2HeliosError) as ei:
        parse_change(
            "table public.doc: UPDATE: old-key: docid[integer]:1 "
            "new-tuple: docid[integer]:2 tenant[text]:unchanged-toast-datum",
            "public", CPK, CKNOWN)
    assert "tenant" in str(ei.value)


def test_parse_change_keymove_old_key_missing_pk_fails_closed():
    # S4(1): under REPLICA IDENTITY USING INDEX (a non-PK index) the pre-image
    # (old-key) carries index columns, not the PK. A key-changing UPDATE whose
    # new-tuple supplies the (new) PK but whose old-key lacks a PK component would
    # yield a partial/empty before_key -> the old-key row leaks or a duplicate
    # logical row appears. Fail closed with the actionable REPLICA IDENTITY message.
    with pytest.raises(Any2HeliosError) as ei:
        parse_change(
            "table public.doc: UPDATE: old-key: idxcol[integer]:99 "
            "new-tuple: tenant[text]:'acme' docid[integer]:2 body[text]:'x'",
            "public", CPK, CKNOWN)
    msg = str(ei.value)
    assert "REPLICA IDENTITY" in msg and ("tenant" in msg or "docid" in msg)


def test_parse_change_composite_pk_full_image_unaffected():
    # A full after-image (both PK components present) keeps working unchanged.
    rec = parse_change(
        "table public.doc: UPDATE: tenant[text]:'acme' docid[integer]:3 body[text]:'z'",
        "public", CPK, CKNOWN)
    assert rec.key == {"tenant": "acme", "docid": 3}
    assert rec.before_key == {}


def test_parse_change_single_pk_partial_update_still_parses():
    # Existing single-PK behaviour is unchanged: a partial UPDATE that omits a
    # NON-key TOAST column keys fine (the PK itself is present).
    rec = parse_change(
        "table public.actor: UPDATE: actor_id[integer]:5 bio[text]:unchanged-toast-datum "
        "first_name[character varying]:'Q'", "public", PK, KNOWN)
    assert rec.key == {"actor_id": 5} and "bio" not in rec.after


# --- K2: capture tags each record with the LSN as source_pos ------------------


class _FakeAdapter:
    def __init__(self, rows):
        self._rows = rows

    def _q1(self, sql, *p):
        return (1,) if "pg_replication_slots" in sql else None

    def _qall(self, sql, *p):
        return self._rows


def _actor_table():
    from any2heliosdb.core.catalog_model import Column, DataType, PrimaryKey, Table
    return Table(name="actor", schema="public",
                 columns=[Column("actor_id", DataType.decimal(10, 0))],
                 primary_key=PrimaryKey(columns=["actor_id"]))


def test_capture_tags_records_with_lsn_source_pos():
    from any2heliosdb.cdc.sources.postgres_logical import PostgresLogicalSource, lsn_to_int
    rows = [
        ("0/1A0", "BEGIN 700"),
        ("0/1A2", "table public.actor: INSERT: actor_id[integer]:1"),
        ("0/1B4", "table public.actor: UPDATE: actor_id[integer]:1"),
        ("0/1C0", "COMMIT 700"),
    ]
    records, last_lsn, skipped = PostgresLogicalSource(
        _FakeAdapter(rows), "public", [_actor_table()], "e1").capture()
    # Distinct LSNs -> each base seen once -> bare int (wire-compatible).
    assert [r.source_pos for r in records] == [lsn_to_int("0/1A2"), lsn_to_int("0/1B4")]
    assert records[0].source_pos < records[1].source_pos   # monotonic for dedup
    assert last_lsn == "0/1C0" and skipped == []


def test_capture_records_sharing_one_lsn_get_compound_source_pos():
    # S2 on the PG path: several change lines can share an LSN within a transaction.
    # They must stay totally ordered per RECORD via a compound [base, seq] (the
    # first at a base keeps the bare int), so a prefix crash re-appends only the
    # never-trailed tail instead of dropping the shared-LSN remainder forever.
    from any2heliosdb.cdc.sources.postgres_logical import PostgresLogicalSource, lsn_to_int
    from any2heliosdb.core.change_record import source_pos_key
    base = lsn_to_int("0/2A0")
    rows = [
        ("0/2A0", "table public.actor: INSERT: actor_id[integer]:1"),
        ("0/2A0", "table public.actor: INSERT: actor_id[integer]:2"),
        ("0/2A0", "table public.actor: INSERT: actor_id[integer]:3"),
    ]
    records, _, _ = PostgresLogicalSource(
        _FakeAdapter(rows), "public", [_actor_table()], "e1").capture()
    assert [r.source_pos for r in records] == [base, [base, 1], [base, 2]]
    keys = [source_pos_key(r.source_pos) for r in records]
    assert keys == sorted(keys) and len(set(keys)) == 3    # strict total order


def test_capture_tags_records_with_begin_xid_as_txn_id():
    # P2 slice 3: every change between BEGIN <xid> and COMMIT is tagged with that
    # xid (txn_id), so the replicat can regroup source transactions. Two txns get
    # two distinct ids; a record between them (after COMMIT, before the next BEGIN)
    # would be untagged.
    from any2heliosdb.cdc.sources.postgres_logical import PostgresLogicalSource
    rows = [
        ("0/10", "BEGIN 700"),
        ("0/12", "table public.actor: INSERT: actor_id[integer]:1"),
        ("0/14", "table public.actor: INSERT: actor_id[integer]:2"),
        ("0/16", "COMMIT 700"),
        ("0/20", "BEGIN 701"),
        ("0/22", "table public.actor: INSERT: actor_id[integer]:3"),
        ("0/26", "COMMIT 701"),
    ]
    records, _, _ = PostgresLogicalSource(
        _FakeAdapter(rows), "public", [_actor_table()], "e1").capture()
    assert [r.key["actor_id"] for r in records] == [1, 2, 3]
    assert [r.txn_id for r in records] == [700, 700, 701]
    # The COMMIT line terminates each transaction's LAST record (txn_end), so the
    # replicat can certify a whole txn vs a torn/partial prefix. Non-final records
    # of a txn are not terminated.
    assert [r.txn_end for r in records] == [False, True, True]


def test_capture_untagged_before_any_begin_and_after_malformed_xid():
    # A change with no enclosing BEGIN (or a BEGIN with a non-integer xid) is
    # untagged (txn_id None) -> it applies per-record via the barrier, never
    # mis-grouped into a fake transaction.
    from any2heliosdb.cdc.sources.postgres_logical import PostgresLogicalSource
    rows = [
        ("0/12", "table public.actor: INSERT: actor_id[integer]:1"),  # no BEGIN yet
        ("0/14", "BEGIN notanumber"),
        ("0/16", "table public.actor: INSERT: actor_id[integer]:2"),
    ]
    records, _, _ = PostgresLogicalSource(
        _FakeAdapter(rows), "public", [_actor_table()], "e1").capture()
    assert [r.txn_id for r in records] == [None, None]
    # Untagged records are never terminators (they apply per-record via the barrier).
    assert [r.txn_end for r in records] == [False, False]
