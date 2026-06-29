"""Oracle connection options: thick mode (NNE), SYSDBA, and the connect hints."""
from __future__ import annotations

from any2heliosdb.config.model import SourceConfig
from any2heliosdb.config.store import load_config
from any2heliosdb.sources.base import SourceDsn
from any2heliosdb.sources.oracle.adapter import _oracle_connect_hint


def test_source_config_carries_oracle_connect_options():
    d = SourceConfig(thick=True, client_dir="/opt/instantclient", sysdba=True).to_dsn()
    assert d.thick is True and d.client_dir == "/opt/instantclient" and d.sysdba is True
    d0 = SourceConfig().to_dsn()  # safe defaults: thin, no client dir, not sysdba
    assert d0.thick is False and d0.sysdba is False and d0.client_dir is None


def test_connect_hint_for_nne_thick_mode():
    # The customer's exact failure: thin mode can't do Native Network Encryption.
    err = Exception("DPY-6005: cannot connect; DPY-3001: Native Network Encryption "
                    "and Data Integrity is only supported in python-oracledb thick mode")
    hint = _oracle_connect_hint(err, SourceDsn(thick=False))
    assert "DPY-3001" in hint and "THICK mode" in hint and "thick = true" in hint
    # already in thick mode -> the DPY-3001 advice is not repeated (different cause)
    assert "THICK mode" not in _oracle_connect_hint(Exception("DPY-3001: x"),
                                                    SourceDsn(thick=True))


def test_connect_hint_for_sys_without_sysdba():
    err = Exception("ORA-28009: connection as SYS should be as SYSDBA or SYSOPER")
    hint = _oracle_connect_hint(err, SourceDsn(user="SYS"))
    assert "sysdba = true" in hint


def test_connect_hint_passthrough_for_other_errors():
    err = Exception("ORA-01017: invalid username/password")
    assert _oracle_connect_hint(err, SourceDsn()) == "Oracle connect failed: {}".format(err)


def test_load_config_parses_thick_and_sysdba(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[source]\n'
        'dialect = "oracle"\nhost = "h"\nport = 1531\nservice_name = "S"\n'
        'user = "SYS"\npassword = "x"\nthick = true\nclient_dir = "/ic"\nsysdba = true\n'
        '[target]\n'
        'driver = "psycopg"\nhost = "t"\nport = 5432\ndbname = "postgres"\nuser = "postgres"\n'
        '[options]\noutput_dir = "/tmp/o"\n'
    )
    cfg = load_config(str(p))
    assert cfg.source.thick is True
    assert cfg.source.client_dir == "/ic"
    assert cfg.source.sysdba is True
    # and they survive into the DSN the adapter uses
    d = cfg.source.to_dsn()
    assert (d.thick, d.client_dir, d.sysdba) == (True, "/ic", True)
