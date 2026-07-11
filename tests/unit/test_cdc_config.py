"""The [cdc] config section: round-trip, defaults, and legacy configs."""
from __future__ import annotations

import os
import tempfile

from any2heliosdb.config.model import CdcConfig, ProjectConfig
from any2heliosdb.config.store import load_config, save_config, to_toml_dict


def test_cdc_defaults():
    c = CdcConfig()
    assert c.capture_batch == 50_000
    assert c.apply_batch == 10_000
    assert c.poison_retries == 3
    assert c.poison_max_per_run == 25
    assert c.trail_rotate_mb == 256


def test_cdc_section_roundtrips():
    cfg = ProjectConfig()
    cfg.cdc = CdcConfig(capture_batch=1234, apply_batch=77, poison_retries=5,
                        poison_max_per_run=9, trail_rotate_mb=64)
    d = to_toml_dict(cfg)
    assert d["cdc"]["capture_batch"] == 1234 and d["cdc"]["trail_rotate_mb"] == 64
    assert d["cdc"]["poison_max_per_run"] == 9
    fd, p = tempfile.mkstemp(suffix=".toml")
    os.close(fd)
    try:
        save_config(cfg, p)
        back = load_config(p)
        assert back.cdc.capture_batch == 1234
        assert back.cdc.apply_batch == 77
        assert back.cdc.poison_retries == 5
        assert back.cdc.poison_max_per_run == 9
        assert back.cdc.trail_rotate_mb == 64
    finally:
        os.remove(p)


def test_legacy_config_without_cdc_gets_defaults():
    fd, p = tempfile.mkstemp(suffix=".toml")
    os.close(fd)
    try:
        with open(p, "w") as f:
            f.write('[source]\ndialect = "oracle"\n[target]\ndriver = "psycopg"\n')
        cfg = load_config(p)
        assert cfg.cdc.capture_batch == 50_000 and cfg.cdc.poison_retries == 3
        assert cfg.cdc.poison_max_per_run == 25
    finally:
        os.remove(p)
