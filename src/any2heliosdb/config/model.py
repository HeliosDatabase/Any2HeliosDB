"""Project configuration model (the modern replacement for ora2pg.conf).

Passwords are never stored in the file: each side names an environment variable
(``password_env``) resolved at runtime. A literal ``password`` is accepted only
as a dev convenience (e.g. trust targets) and should stay empty in committed
configs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Optional

from ..constants import SourceDialect, TargetDriverKind
from ..sources.base import SourceDsn
from ..target.base import TargetDsn


def _resolve_password(password_env: Optional[str], password: Optional[str]) -> Optional[str]:
    if password_env:
        return os.environ.get(password_env)
    return password or None


@dataclass
class SourceConfig:
    dialect: SourceDialect = SourceDialect.ORACLE
    host: str = "127.0.0.1"
    port: int = 1521
    service_name: Optional[str] = None
    sid: Optional[str] = None
    database: Optional[str] = None
    user: str = ""
    password_env: Optional[str] = None
    password: Optional[str] = None  # dev only
    schema: Optional[str] = None
    # Oracle connection options (ignored for other dialects):
    thick: bool = False             # python-oracledb thick mode (Instant Client) — for NNE servers
    client_dir: Optional[str] = None  # Instant Client lib dir (else PATH/LD_LIBRARY_PATH)
    sysdba: bool = False            # connect with SYSDBA privilege (the SYS user)

    def to_dsn(self) -> SourceDsn:
        return SourceDsn(
            host=self.host, port=self.port, service_name=self.service_name, sid=self.sid,
            database=self.database, user=self.user,
            password=_resolve_password(self.password_env, self.password) or "",
            schema=self.schema,
            thick=self.thick, client_dir=self.client_dir, sysdba=self.sysdba,
        )


@dataclass
class TargetConfig:
    driver: TargetDriverKind = TargetDriverKind.PSYCOPG
    host: str = "127.0.0.1"
    port: int = 5432
    dbname: str = "postgres"
    user: str = "postgres"
    password_env: Optional[str] = None
    password: Optional[str] = None
    sslmode: Optional[str] = None

    def to_dsn(self) -> TargetDsn:
        return TargetDsn(
            host=self.host, port=self.port, dbname=self.dbname, user=self.user,
            password=_resolve_password(self.password_env, self.password),
            sslmode=self.sslmode,
        )


@dataclass
class Options:
    output_dir: str = "./migration_output"
    batch_size: int = 1000
    parallelism: int = 4
    prefer_copy: bool = True
    preserve_case: bool = False
    drop_existing: bool = True
    # Resumable-load ledger backend: "sqlite" (stdlib, zero-friction default) or
    # "nano" (embedded HeliosDB-Nano via the any2heliosdb[nano-manifest] extra —
    # dogfoods the engine; the manifest becomes a RocksDB directory).
    manifest_backend: str = "sqlite"


@dataclass
class ProjectConfig:
    source: SourceConfig = field(default_factory=SourceConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    options: Options = field(default_factory=Options)
    # Ora2Pg-style overrides
    data_type: Dict[str, str] = field(default_factory=dict)
    modify_type: Dict[str, str] = field(default_factory=dict)
    # Cached capability snapshot from the wizard's smoke test (informational).
    capability: Dict[str, object] = field(default_factory=dict)
