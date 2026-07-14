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
    # Seconds to wait to establish the source connection (all dialects) before
    # failing — bounds a firewalled/unreachable source instead of hanging forever.
    connect_timeout: int = 10

    def to_dsn(self) -> SourceDsn:
        return SourceDsn(
            host=self.host, port=self.port, service_name=self.service_name, sid=self.sid,
            database=self.database, user=self.user,
            password=_resolve_password(self.password_env, self.password) or "",
            schema=self.schema,
            thick=self.thick, client_dir=self.client_dir, sysdba=self.sysdba,
            connect_timeout=self.connect_timeout,
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
    # Seconds to wait to establish the target connection before failing (consumed
    # by the psycopg conninfo and the native Oracle-wire driver's connect).
    connect_timeout: int = 10

    def to_dsn(self) -> TargetDsn:
        return TargetDsn(
            host=self.host, port=self.port, dbname=self.dbname, user=self.user,
            password=_resolve_password(self.password_env, self.password),
            sslmode=self.sslmode, connect_timeout=self.connect_timeout,
        )


@dataclass
class Options:
    output_dir: str = "./migration_output"
    batch_size: int = 1000
    parallelism: int = 4
    # Target chunks per worker: the resumable loader splits each table into
    # ~``parallelism * chunks_per_worker`` PK-range chunks. More chunks smooth
    # per-worker skew (a bigger chunk finishing last) at the cost of more chunk
    # bookkeeping. Plan-affecting: it joins the loader's config hash so changing
    # it resets the run rather than silently mixing two chunk plans.
    chunks_per_worker: int = 2
    prefer_copy: bool = True
    preserve_case: bool = False
    drop_existing: bool = True
    # Native (Oracle-wire) target only: the per-round-trip ``call_timeout`` (ms)
    # set on the oracledb connection — a generous safety net so a bulk array-INSERT
    # never blocks forever on a stalled HeliosDB TTC response. Unused by the
    # psycopg/PG-wire path.
    native_call_timeout_ms: int = 300_000
    # Resumable-load ledger backend: "sqlite" (stdlib, zero-friction default) or
    # "nano" (embedded HeliosDB-Nano via the any2heliosdb[nano-manifest] extra —
    # dogfoods the engine; the manifest becomes a RocksDB directory).
    manifest_backend: str = "sqlite"
    # TEST_DATA row-mismatch budget: run_test_data stops after this many mismatches
    # per table so a badly-diverged table fails fast instead of hashing the whole
    # sample. Honored identically by the CLI `test-data` command and the MCP
    # `test_data` tool.
    test_data_max_errors: int = 10


@dataclass
class CdcConfig:
    """Tunables for the CDC spine (Extract → trail → Replicat).

    Every knob here bounds a resource the CDC pipeline would otherwise let grow
    without limit (the reason tier-2 hardening is a priority after the host OOM):

    * ``capture_batch`` — max change events a single ``extract`` cycle pulls from a
      log-based source (PG logical peek ``LIMIT`` / MySQL binlog event stop). The
      server-side cursor (slot LSN / binlog pos) is only advanced past what was
      captured, so anything beyond the cap is simply picked up next cycle. ``0``
      means "no cap" (the pre-tier-2 unbounded behaviour). The 50k default caps a
      cycle's resident change list at tens of MB rather than a whole backlog.
    * ``apply_batch`` — max trail LINES the ``replicat`` reads and applies per
      bounded chunk, advancing the apply cursor per chunk. The keymove barrier
      composes: each keymove is still flushed alone within its chunk. ``0`` means
      "read the whole slice at once" (pre-tier-2 behaviour).
    * ``poison_retries`` — how many times the replicat retries a single failing
      (non-keymove) record before moving it to ``dead_letter.jsonl`` and advancing
      past it, so one bad record can't wedge replication forever. ``0`` disables
      the dead-letter policy (a failing record raises, as before). Keymoves are
      NEVER dead-lettered — a keymove failure always fails closed. Before parking a
      record the replicat ``ping()``s the target and re-raises (cursor unmoved) if
      it is unreachable, so a transient target outage never dead-letters the whole
      backlog.
    * ``poison_max_per_run`` — a mass-poison circuit breaker: if a single replicat
      run would dead-letter MORE than this many records it raises instead (cursor
      unmoved for the offending chunk). A flood of "poison" almost always means an
      environment problem (wrong target, a schema drift) rather than genuinely bad
      data, so fail closed and let the operator look. ``0`` disables the breaker.
    * ``trail_rotate_mb`` — rotate the active trail segment once it reaches this
      many MB (closed segments become ``trail.NNNNN.jsonl``); the apply cursor
      stays a single global line index across segments, so legacy single-file
      trails keep working unchanged. ``0`` disables rotation (one ``trail.jsonl``).
    """
    capture_batch: int = 50_000
    apply_batch: int = 10_000
    poison_retries: int = 3
    poison_max_per_run: int = 25
    trail_rotate_mb: int = 256


@dataclass
class ProjectConfig:
    source: SourceConfig = field(default_factory=SourceConfig)
    target: TargetConfig = field(default_factory=TargetConfig)
    options: Options = field(default_factory=Options)
    cdc: CdcConfig = field(default_factory=CdcConfig)
    # Ora2Pg-style overrides
    data_type: Dict[str, str] = field(default_factory=dict)
    modify_type: Dict[str, str] = field(default_factory=dict)
    # Cached capability snapshot from the wizard's smoke test (informational).
    capability: Dict[str, object] = field(default_factory=dict)
