"""Load/save :class:`ProjectConfig` as TOML, and build the runtime objects
(source adapter, target driver, type registry) from a config.
"""
from __future__ import annotations

import sys
from typing import Any, Dict

from ..constants import SourceDialect, TargetDriverKind
from ..errors import ConfigError
from ..sources.base import SourceAdapter
from ..target.base import TargetDriver
from ..typemap.registry import TypeRegistry
from .model import CdcConfig, Options, ProjectConfig, SourceConfig, TargetConfig

if sys.version_info >= (3, 11):  # pragma: no cover
    import tomllib as _toml_read
else:
    import tomli as _toml_read
import tomli_w as _toml_write


def _clean(d: Dict[str, Any]) -> Dict[str, Any]:
    """Drop None values (TOML has no null) and unwrap str-Enums."""
    out: Dict[str, Any] = {}
    for k, v in d.items():
        if v is None:
            continue
        if hasattr(v, "value") and not isinstance(v, (str, int, float, bool)):
            v = v.value
        if isinstance(v, dict):
            v = _clean(v)
        out[k] = v
    return out


def to_toml_dict(cfg: ProjectConfig) -> Dict[str, Any]:
    from dataclasses import asdict

    return {
        "source": _clean(asdict(cfg.source)),
        "target": _clean(asdict(cfg.target)),
        "options": _clean(asdict(cfg.options)),
        "cdc": _clean(asdict(cfg.cdc)),
        "data_type": dict(cfg.data_type),
        "modify_type": dict(cfg.modify_type),
        "capability": _clean(dict(cfg.capability)),
    }


def save_config(cfg: ProjectConfig, path: str) -> None:
    with open(path, "wb") as f:
        _toml_write.dump(to_toml_dict(cfg), f)


def _positive_timeout(value: object, key: str) -> int:
    """Reject 0/negative connect timeouts at config load. 0 is not portable:
    pymysql raises ValueError, other drivers treat it as OS-default/infinite —
    a knob whose meaning flips per driver is worse than no knob. Minimum 1s;
    there is deliberately no infinite option (the audit's original finding was
    sources hanging forever)."""
    t = int(value)  # type: ignore[call-overload]
    if t < 1:
        raise ConfigError(
            "{} must be >= 1 second (got {}). 0/negative is not portable across "
            "drivers (pymysql rejects it; others treat it as no timeout).".format(key, t))
    return t


def load_config(path: str) -> ProjectConfig:
    try:
        with open(path, "rb") as f:
            d = _toml_read.load(f)
    except FileNotFoundError as e:
        raise ConfigError("config not found: {} (run `a2h wizard`)".format(path)) from e
    except Exception as e:  # noqa: BLE001
        raise ConfigError("could not parse {}: {}".format(path, e)) from e

    src = d.get("source", {})
    tgt = d.get("target", {})
    opt = d.get("options", {})
    source = SourceConfig(
        dialect=SourceDialect(src.get("dialect", "oracle")),
        host=src.get("host", "127.0.0.1"), port=int(src.get("port", 1521)),
        service_name=src.get("service_name"), sid=src.get("sid"),
        database=src.get("database"), user=src.get("user", ""),
        password_env=src.get("password_env"), password=src.get("password"),
        schema=src.get("schema"),
        thick=bool(src.get("thick", False)), client_dir=src.get("client_dir"),
        sysdba=bool(src.get("sysdba", False)),
        connect_timeout=_positive_timeout(src.get("connect_timeout", 10), "[source] connect_timeout"),
    )
    target = TargetConfig(
        driver=TargetDriverKind(tgt.get("driver", "psycopg")),
        host=tgt.get("host", "127.0.0.1"), port=int(tgt.get("port", 5432)),
        dbname=tgt.get("dbname", "postgres"), user=tgt.get("user", "postgres"),
        password_env=tgt.get("password_env"), password=tgt.get("password"),
        sslmode=tgt.get("sslmode"),
        connect_timeout=_positive_timeout(tgt.get("connect_timeout", 10), "[target] connect_timeout"),
    )
    options = Options(
        output_dir=opt.get("output_dir", "./migration_output"),
        batch_size=int(opt.get("batch_size", 1000)),
        parallelism=int(opt.get("parallelism", 4)),
        chunks_per_worker=int(opt.get("chunks_per_worker", 2)),
        prefer_copy=bool(opt.get("prefer_copy", True)),
        preserve_case=bool(opt.get("preserve_case", False)),
        drop_existing=bool(opt.get("drop_existing", True)),
        manifest_backend=str(opt.get("manifest_backend", "sqlite")).lower(),
        native_call_timeout_ms=int(opt.get("native_call_timeout_ms", 300_000)),
    )
    cdc_d = d.get("cdc", {})
    cdc = CdcConfig(
        capture_batch=int(cdc_d.get("capture_batch", 50_000)),
        apply_batch=int(cdc_d.get("apply_batch", 10_000)),
        poison_retries=int(cdc_d.get("poison_retries", 3)),
        poison_max_per_run=int(cdc_d.get("poison_max_per_run", 25)),
        trail_rotate_mb=int(cdc_d.get("trail_rotate_mb", 256)),
    )
    return ProjectConfig(
        source=source, target=target, options=options, cdc=cdc,
        data_type=dict(d.get("data_type", {})), modify_type=dict(d.get("modify_type", {})),
        capability=dict(d.get("capability", {})),
    )


# --- runtime object builders -------------------------------------------------
def build_source_adapter(cfg: ProjectConfig) -> SourceAdapter:
    dialect = cfg.source.dialect
    if dialect is SourceDialect.ORACLE:
        from ..sources.oracle.adapter import OracleAdapter

        return OracleAdapter(cfg.source.to_dsn())
    if dialect is SourceDialect.MYSQL:
        from ..sources.mysql.adapter import MySQLAdapter

        return MySQLAdapter(cfg.source.to_dsn())
    if dialect is SourceDialect.MSSQL:
        from ..sources.mssql.adapter import MSSQLAdapter

        return MSSQLAdapter(cfg.source.to_dsn())
    if dialect is SourceDialect.POSTGRESQL:
        from ..sources.postgres.adapter import PostgresAdapter

        return PostgresAdapter(cfg.source.to_dsn())
    raise ConfigError("source dialect '{}' not yet available".format(dialect.value))


def build_target_driver(cfg: ProjectConfig) -> TargetDriver:
    if cfg.target.driver is TargetDriverKind.PSYCOPG:
        from ..target.psycopg_driver import PsycopgDriver

        return PsycopgDriver(cfg.target.to_dsn())
    if cfg.target.driver is TargetDriverKind.MYSQL:
        # Heterogeneous / migrate-back: a MySQL server over the MySQL wire.
        from ..target.mysql_driver import MySQLTargetDriver

        return MySQLTargetDriver(cfg.target.to_dsn())
    # native: connect over the same wire protocol as the source so HeliosDB does
    # the dialect translation.
    if cfg.source.dialect is SourceDialect.ORACLE:
        from ..target.native_driver import NativeOracleDriver

        return NativeOracleDriver(
            cfg.target.to_dsn(), call_timeout_ms=cfg.options.native_call_timeout_ms)
    raise ConfigError(
        "native target driver for source dialect '{}' is not implemented yet "
        "(Oracle only); use the psycopg driver".format(cfg.source.dialect.value))


def build_type_registry(cfg: ProjectConfig) -> TypeRegistry:
    reg = TypeRegistry(cfg.source.dialect)
    if cfg.data_type:
        reg.apply_data_type(cfg.data_type)
    if cfg.modify_type:
        reg.apply_modify_type(cfg.modify_type)
    return reg
