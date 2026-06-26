"""Unit tests for copy_codec binary (bytea) encoding and NUL handling.

Covers CODEX finding #60: bytes were stringified via repr() (invalid bytea)
and embedded-NUL strings were emitted raw (unstorable by PostgreSQL text).
The codec is the file-export path (``export -t COPY``) / wizard fidelity check;
the live psycopg migrate path adapts Python values itself.
"""
import pytest

from any2heliosdb.target import copy_codec


# --- bytes -> bytea hex literal in COPY TEXT --------------------------------
def test_bytes_encode_as_bytea_hex():
    # b'\x00\xff\x1a' -> on-wire ``\x00ff1a``; in COPY TEXT the leading
    # backslash is doubled, so the field token is ``\\x00ff1a``.
    assert copy_codec.encode_field(b"\x00\xff\x1a") == "\\\\x00ff1a"


def test_bytearray_and_memoryview_encode_as_bytea_hex():
    expected = "\\\\x00ff1a"
    assert copy_codec.encode_field(bytearray(b"\x00\xff\x1a")) == expected
    assert copy_codec.encode_field(memoryview(b"\x00\xff\x1a")) == expected


def test_empty_bytes_encode_as_bytea_hex():
    # Empty BLOB/RAW is a valid (zero-length) bytea, distinct from NULL.
    assert copy_codec.encode_field(b"") == "\\\\x"


def test_bytea_hex_token_decodes_back_to_literal_string():
    # COPY parser round-trip: the doubled backslash unescapes to ``\x00ff1a``.
    token = copy_codec.encode_field(b"\x00\xff\x1a")
    assert copy_codec.unescape_copy_text(token) == "\\x00ff1a"


# --- embedded NUL in str: fail closed (do not silently corrupt) -------------
def test_str_with_embedded_nul_raises():
    with pytest.raises(ValueError) as exc:
        copy_codec.encode_field("ab\x00cd")
    # message names the problem (NUL) and is descriptive
    assert "NUL" in str(exc.value)


def test_str_with_embedded_nul_raises_in_row():
    with pytest.raises(ValueError):
        copy_codec.encode_row(["ok", "bad\x00value"])


# --- existing semantics for normal strings stay EXACTLY the same ------------
def test_normal_string_unchanged():
    original = "a\tb\nc\\d\re"
    assert copy_codec.encode_field(original) == "a\\tb\\nc\\\\d\\re"


def test_none_is_null_string_and_empty_string_is_empty_field():
    assert copy_codec.encode_field(None) == copy_codec.DEFAULT_NULL  # \N
    assert copy_codec.encode_field(None) == "\\N"
    assert copy_codec.encode_field("") == ""  # empty != NULL


def test_plain_string_passthrough():
    assert copy_codec.encode_field("hello world") == "hello world"
