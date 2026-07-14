"""Codec fidelity for the CDC ChangeRecord type-tagged JSON codec.

F1 — a tz-aware ``timestamptz`` must be normalized to a canonical UTC instant on
encode (so a target in another session timezone reconstructs the same instant),
while a naive ``timestamp`` stays a bare wall-clock reading, and pre-fix trail
lines (wall time with their own offset) still decode unchanged — no wire break.

F2 — a MySQL binlog JSON column (Python dict/list) and SET column (Python set)
must round-trip as valid JSON text / a SET literal, never a Python ``repr``.

All hermetic: the codec is pure Python (no DB, no trail files).
"""
from __future__ import annotations

import datetime as dt
import json

from any2heliosdb.core.change_record import INSERT, ChangeRecord, _decode, _encode


# --- F1: timestamptz UTC normalization ---------------------------------------
def test_tz_aware_datetime_normalized_to_utc_on_encode():
    aware = dt.datetime(2020, 1, 15, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    enc = _encode(aware)
    assert enc == {"__t__": "ts", "v": "2020-01-15T10:00:00+00:00"}  # +02:00 -> UTC
    back = _decode(enc)
    assert back.tzinfo is not None and back.utcoffset() == dt.timedelta(0)  # tz-aware UTC
    assert back == aware                                                    # same instant


def test_tz_aware_several_zones_preserve_instant():
    for offset_h in (-8, -5, 0, 2, 5.5, 9, 13):
        tz = dt.timezone(dt.timedelta(hours=offset_h))
        v = dt.datetime(2021, 6, 30, 23, 59, 58, 123456, tzinfo=tz)
        back = _decode(_encode(v))
        assert back == v and back.tzinfo is not None
        # stored form is always the canonical +00:00 offset
        assert _encode(v)["v"].endswith("+00:00")


def test_dst_edge_same_instant_normalizes_identically():
    # US/Eastern springs forward 2021-03-14 02:00. 01:30 EST (-05:00) and 02:30 EDT
    # (-04:00) are the SAME instant (06:30Z) — both must store the identical UTC line.
    est = dt.datetime(2021, 3, 14, 1, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    edt = dt.datetime(2021, 3, 14, 2, 30, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
    assert _encode(est)["v"] == _encode(edt)["v"] == "2021-03-14T06:30:00+00:00"
    assert _decode(_encode(est)) == _decode(_encode(edt))


def test_naive_datetime_left_untouched():
    naive = dt.datetime(2020, 1, 15, 12, 0, 0)
    enc = _encode(naive)
    assert enc == {"__t__": "ts", "v": "2020-01-15T12:00:00"}  # no offset added
    back = _decode(enc)
    assert back == naive and back.tzinfo is None                # still naive


def test_old_format_walltime_line_decodes_unchanged():
    # A pre-fix trail stored source-session local wall time WITH its own offset;
    # decoding it must reproduce that exact value (no re-normalization) — no wire break.
    old_aware = {"__t__": "ts", "v": "2020-01-15T12:00:00+02:00"}
    assert _decode(old_aware) == dt.datetime(
        2020, 1, 15, 12, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    old_naive = {"__t__": "ts", "v": "2020-01-15T12:00:00"}
    assert _decode(old_naive) == dt.datetime(2020, 1, 15, 12, 0, 0)


def test_datetime_roundtrips_through_change_record():
    aware = dt.datetime(2022, 11, 6, 1, 30, 0, tzinfo=dt.timezone(dt.timedelta(hours=-7)))
    r = ChangeRecord(op=INSERT, schema="hr", table="t", key={"ID": 1},
                     after={"ID": 1, "TS": aware})
    got = ChangeRecord.from_json(r.to_json()).after["TS"]
    assert got == aware and got.utcoffset() == dt.timedelta(0)


# --- F2: MySQL binlog JSON / SET round-trip ----------------------------------
def test_mysql_json_dict_roundtrips_as_json_text():
    r = ChangeRecord(op=INSERT, schema="db", table="t", key={"ID": 1},
                     after={"ID": 1, "DATA": {"b": 2, "a": [1, 2]}})
    val = ChangeRecord.from_json(r.to_json()).after["DATA"]
    assert isinstance(val, str)
    assert json.loads(val) == {"a": [1, 2], "b": 2}      # valid JSON, not a repr
    assert val == '{"a":[1,2],"b":2}'                    # compact + key-sorted (deterministic)
    assert "'" not in val                                # no single-quoted Python repr


def test_mysql_json_list_roundtrips_as_json_text():
    r = ChangeRecord(op=INSERT, schema="db", table="t", key={"ID": 1},
                     after={"TAGS": [3, 1, 2]})
    val = ChangeRecord.from_json(r.to_json()).after["TAGS"]
    assert val == "[3,1,2]" and json.loads(val) == [3, 1, 2]


def test_mysql_set_roundtrips_as_set_literal():
    r = ChangeRecord(op=INSERT, schema="db", table="t", key={"ID": 1},
                     after={"PERMS": {"read", "write", "admin"}})
    val = ChangeRecord.from_json(r.to_json()).after["PERMS"]
    # apply-level: the bound literal is a valid, sorted MySQL SET literal, not a set repr
    assert isinstance(val, str)
    assert val == "admin,read,write"                     # sorted -> deterministic
    assert set(val.split(",")) == {"read", "write", "admin"}
    assert "{" not in val and "'" not in val             # not "{'read', ...}"


def test_json_encoding_is_deterministic_regardless_of_key_order():
    a = _encode({"x": 1, "y": 2, "z": 3})
    b = _encode({"z": 3, "y": 2, "x": 1})
    assert a == b                                        # sort_keys makes the trail line stable


def test_empty_json_and_set():
    assert _decode(_encode({})) == "{}"
    assert _decode(_encode([])) == "[]"
    assert _decode(_encode(set())) == ""
