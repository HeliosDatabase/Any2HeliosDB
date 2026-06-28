"""Unit tests for the PostgreSQL logical-decoding (test_decoding) CDC parser."""
from __future__ import annotations

from decimal import Decimal

from any2heliosdb.cdc.sources.postgres_logical import (
    parse_change, parse_fields, slot_name)
from any2heliosdb.core.change_record import DELETE, INSERT, UPDATE

PK = {"ACTOR": ["actor_id"], "X": ["id"]}
KNOWN = {"ACTOR", "X"}


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
    # When the PK changes (or REPLICA IDENTITY FULL), test_decoding emits
    # "old-key: ... new-tuple: ...". The key must come from old-key, the image
    # from new-tuple.
    rec = parse_change(
        "table public.actor: UPDATE: old-key: actor_id[integer]:201 "
        "new-tuple: actor_id[integer]:202 first_name[character varying]:'New'",
        "public", PK, KNOWN)
    assert rec is not None and rec.op == UPDATE
    assert rec.key == {"actor_id": 201}
    assert rec.after["actor_id"] == 202 and rec.after["first_name"] == "New"
