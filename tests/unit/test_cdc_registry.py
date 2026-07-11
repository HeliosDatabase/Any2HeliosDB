"""CdcRegistry storage-format guards (tier-2 fix round).

The registry stores the captured table set as a single comma-separated
``tables_csv`` column, so a table name containing a comma would be silently split
on load and excluded from capture. Registration/adoption must fail closed instead.
"""
from __future__ import annotations

import os

import pytest

from any2heliosdb.cdc.registry import CdcRegistry
from any2heliosdb.errors import Any2HeliosError


def _reg(tmp_path):
    return CdcRegistry(os.path.join(str(tmp_path), "cdc.db"))


def test_register_refuses_comma_in_table_name(tmp_path):
    reg = _reg(tmp_path)
    try:
        with pytest.raises(Any2HeliosError):
            reg.register("e1", "hr", ["ok", "a,b"])
        # Nothing was persisted (fail closed before the write).
        assert reg.get("e1") is None
    finally:
        reg.close()


def test_adopt_refuses_comma_in_table_name(tmp_path):
    reg = _reg(tmp_path)
    try:
        reg.register("e2", "hr", ["ok"])            # clean first registration
        with pytest.raises(Any2HeliosError):
            reg.register("e2", "hr", ["ok", "bad,name"], adopt_tables=True)
        # The pinned set is unchanged — the bad adopt did not corrupt it.
        assert reg.get("e2").tables == ["ok"]
    finally:
        reg.close()


def test_schema_only_update_tolerates_comma_free_tables(tmp_path):
    # A routine (non-adopt) cycle does not persist the table list, so it must not
    # be blocked by a table that appeared with an odd name — only writes are guarded.
    reg = _reg(tmp_path)
    try:
        reg.register("e3", "hr", ["ok"])
        reg.register("e3", "hr", ["ok"], adopt_tables=False)   # schema-only, no raise
        assert reg.get("e3").tables == ["ok"]
    finally:
        reg.close()
