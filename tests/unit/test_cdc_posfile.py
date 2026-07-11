"""Unit tests for the CDC position-file: atomic write + fail-closed recovery.

Covers BLOCKER 3a — a crash mid-write must never silently re-anchor the MySQL
binlog cursor (which would skip every change since the last durable position and
lose data), and the pos file must be replaced atomically so a reader never sees a
torn value.
"""
from __future__ import annotations

import os

import pytest

from any2heliosdb.cdc.posfile import read_pos, write_pos_atomic
from any2heliosdb.errors import Any2HeliosError


def test_read_pos_none_when_file_never_existed(tmp_path):
    # Fresh extract: no file -> None (caller may anchor at the current coordinate).
    assert read_pos(str(tmp_path / "binlog.pos")) is None


def test_read_pos_returns_valid_binlog_coordinate(tmp_path):
    p = tmp_path / "binlog.pos"
    p.write_text("mysql-bin.000007:15473")
    assert read_pos(str(p)) == "mysql-bin.000007:15473"


def test_read_pos_returns_valid_lsn(tmp_path):
    p = tmp_path / "binlog.pos"
    p.write_text("16/B2C50A8\n")
    assert read_pos(str(p)) == "16/B2C50A8"


def test_read_pos_fails_closed_on_empty_file(tmp_path):
    # A crash that truncated the file to zero bytes must RAISE, not re-anchor.
    p = tmp_path / "binlog.pos"
    p.write_text("")
    with pytest.raises(Any2HeliosError) as ei:
        read_pos(str(p))
    assert "empty" in str(ei.value).lower()


def test_read_pos_fails_closed_on_blank_file(tmp_path):
    p = tmp_path / "binlog.pos"
    p.write_text("   \n\t ")
    with pytest.raises(Any2HeliosError):
        read_pos(str(p))


def test_read_pos_fails_closed_on_malformed_cursor(tmp_path):
    # Partially-written / corrupt coordinate (not '<file>:<int>' nor '<hi>/<lo>').
    p = tmp_path / "binlog.pos"
    p.write_text("mysql-bin.000007")
    with pytest.raises(Any2HeliosError) as ei:
        read_pos(str(p))
    assert "malformed" in str(ei.value).lower()


def test_read_pos_fails_closed_on_non_numeric_position(tmp_path):
    p = tmp_path / "binlog.pos"
    p.write_text("mysql-bin.000007:notanumber")
    with pytest.raises(Any2HeliosError):
        read_pos(str(p))


def test_write_then_read_roundtrip(tmp_path):
    p = str(tmp_path / "binlog.pos")
    write_pos_atomic(p, "mysql-bin.000009:4242")
    assert read_pos(p) == "mysql-bin.000009:4242"


def test_write_pos_atomic_uses_os_replace(tmp_path, monkeypatch):
    # Atomicity contract: the swap goes through os.replace (atomic rename), never a
    # truncating in-place rewrite.
    p = str(tmp_path / "binlog.pos")
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    write_pos_atomic(p, "mysql-bin.000009:4242")
    assert len(calls) == 1 and calls[0][1] == p
    assert read_pos(p) == "mysql-bin.000009:4242"


def test_write_pos_atomic_leaves_original_intact_on_crash(tmp_path, monkeypatch):
    # Simulate a crash at the rename step: the original pos file must be unchanged
    # and no temp file may be left behind.
    p = str(tmp_path / "binlog.pos")
    write_pos_atomic(p, "mysql-bin.000001:100")

    def boom(src, dst):
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_pos_atomic(p, "mysql-bin.000002:200")
    # Original value survived, and the new (torn) value never became visible.
    assert read_pos(p) == "mysql-bin.000001:100"
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".binlog.pos.")]
    assert leftovers == []


def test_write_pos_atomic_no_leftover_temp_on_success(tmp_path):
    p = str(tmp_path / "binlog.pos")
    write_pos_atomic(p, "mysql-bin.000009:4242")
    names = sorted(os.listdir(tmp_path))
    assert names == ["binlog.pos"]
