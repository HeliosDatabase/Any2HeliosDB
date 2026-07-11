"""Target-driver abstraction.

A single :class:`TargetDriver` interface with two implementations:

* ``psycopg`` — portable PG-wire path (every edition; the tool translates the
  source dialect). See :mod:`any2heliosdb.target.psycopg_driver`.
* ``native`` — same-protocol-as-source path so HeliosDB performs the dialect
  translation. See :mod:`any2heliosdb.target.native_driver` (M3).

The :class:`CapabilityMatrix` is what makes the "fix in HeliosDB, not the tool"
principle operational: emitters and the PL/SQL rewrite layer consult it at
runtime so they only translate what *this* target cannot accept, and record a
gap for the rest.
"""
from __future__ import annotations

import abc
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..constants import Edition


@dataclass
class TargetDsn:
    """Connection coordinates for a HeliosDB target."""

    host: str = "127.0.0.1"
    port: int = 5432
    dbname: str = "postgres"
    user: str = "postgres"
    password: Optional[str] = None
    sslmode: Optional[str] = None  # e.g. "require"
    connect_timeout: int = 10

    def conninfo_kwargs(self) -> Dict[str, Any]:
        kw: Dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "connect_timeout": self.connect_timeout,
        }
        if self.password is not None:
            kw["password"] = self.password
        if self.sslmode:
            kw["sslmode"] = self.sslmode
        return kw


@dataclass
class CapabilityMatrix:
    """What a specific HeliosDB target/edition actually accepts, discovered by
    the capability probe at connect time (never assumed from the edition name)."""

    edition: Edition = Edition.UNKNOWN
    server_version: str = ""
    raw_banner: str = ""
    # Data-movement
    copy_from_stdin: bool = False
    copy_binary: bool = False
    returning: bool = False
    on_conflict: bool = False
    merge: bool = False
    # Whether the target services concurrent write transactions. The Apache
    # editions (Nano/Lite) do not: a second concurrent writer *blocks* (it does
    # not error) until the first commits, which would hang the loader's parallel
    # pass indefinitely. Edition-derived, not probed: a probe would have to risk
    # the very hang it is testing for. Drives serial loading on those targets.
    concurrent_writes: bool = True
    # Functions / procedural
    has_version_function: bool = False
    gen_random_uuid: bool = False
    plpgsql_control_flow: bool = False
    materialized_views: bool = False
    # Constraint enforcement (Lite historically parse-only)
    enforces_check: bool = False
    enforces_fk: bool = False
    # Fine-grained extras keyed by probe name.
    accepts: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["edition"] = self.edition.value
        return d


def detect_edition(banner: str) -> Edition:
    """Classify a PostgreSQL-wire version banner into a target edition.

    Examples seen on the wire:
      * Lite: ``17.0 (HeliosDB-Lite 2.0)``
      * Full: ``14.0 (HeliosDB)``
      * Nano: ``… (HeliosDB-Nano …)``
      * stock PostgreSQL: ``PostgreSQL 16.13 on x86_64-pc-linux-musl``

    HeliosDB banners also carry a ``PostgreSQL <n>`` compatibility prefix, so the
    HeliosDB checks run first; a banner that names PostgreSQL but *not* HeliosDB
    is a real PostgreSQL server (a valid a2h target in its own right).
    """
    b = (banner or "").lower()
    if "nano" in b:
        return Edition.NANO
    if "lite" in b:
        return Edition.LITE
    if "helios" in b:
        # Bare "HeliosDB" with no edition qualifier is the Full server banner.
        return Edition.FULL
    if "postgres" in b:
        return Edition.POSTGRES
    return Edition.UNKNOWN


# Nano gained working concurrent write transactions in 3.60.7 (the same-row
# write-conflict 60s-stall fix). Older Nano — and Lite, which still lacks it —
# must serialize the parallel load.
_NANO_CONCURRENT_WRITES_MIN = (3, 60, 7)
_SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def supports_concurrent_writes(edition: Edition, server_version: str = "") -> bool:
    """Whether *edition* (at *server_version*) services concurrent write txns.

    Full and stock PostgreSQL always do. Lite never does. Nano did not until
    **3.60.7** — before that a second concurrent writer stalled, so a parallel
    load hung; the loader serializes for Lite and pre-3.60.7 Nano. An unparseable
    Nano version is treated as too-old (serialize) to stay safe. See
    :attr:`CapabilityMatrix.concurrent_writes`.
    """
    if edition is Edition.LITE:
        return False
    if edition is Edition.NANO:
        m = _SEMVER_RE.search(server_version or "")
        if not m:
            return False
        return tuple(int(g) for g in m.groups()) >= _NANO_CONCURRENT_WRITES_MIN
    return True


class TargetDriver(abc.ABC):
    """Abstract HeliosDB target driver."""

    def __init__(self, dsn: TargetDsn) -> None:
        self.dsn = dsn
        self.capabilities: CapabilityMatrix = CapabilityMatrix()

    # --- lifecycle -------------------------------------------------------
    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "TargetDriver":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- introspection ---------------------------------------------------
    @abc.abstractmethod
    def server_banner(self) -> str: ...

    @abc.abstractmethod
    def probe_capabilities(self) -> CapabilityMatrix: ...

    # --- statement execution --------------------------------------------
    @abc.abstractmethod
    def execute(self, sql: str, params: Optional[Sequence[object]] = None) -> None:
        """Execute a statement with no result rows (DDL/DML)."""

    @abc.abstractmethod
    def query(self, sql: str, params: Optional[Sequence[object]] = None) -> List[Tuple]:
        """Execute a query and return all rows."""

    @abc.abstractmethod
    def describe_columns(self, target_table: str) -> List[str]:
        """Return *target_table*'s column names without relying on a catalog.

        Catalog support (information_schema/pg_catalog) varies by edition, so
        this is implemented via ``SELECT * ... LIMIT 0`` + the result
        description, which works on any PG-wire target. Raises if the table
        does not exist."""

    @abc.abstractmethod
    def begin(self) -> None: ...

    @abc.abstractmethod
    def commit(self) -> None: ...

    @abc.abstractmethod
    def rollback(self) -> None: ...

    # --- bulk load (data engine seam) -----------------------------------
    @abc.abstractmethod
    def copy_rows(
        self, target_table: str, columns: Sequence[str], rows: Iterable[Sequence[object]]
    ) -> int:
        """Bulk-load rows via COPY FROM STDIN. Returns rows written."""

    @abc.abstractmethod
    def insert_rows(
        self,
        target_table: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
        on_conflict_do_nothing: bool = False,
    ) -> int:
        """Batched multi-row INSERT fallback. Returns rows written."""

    @abc.abstractmethod
    def load_range(
        self,
        target_table: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
        where: Optional[str] = None,
        use_copy: bool = True,
    ) -> int:
        """Idempotently (re)load one chunk: DELETE its range then load, in a
        single transaction. Returns rows written."""

    # --- CDC apply seam (idempotent) ------------------------------------
    @abc.abstractmethod
    def upsert(
        self,
        target_table: str,
        key_cols: Sequence[str],
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
    ) -> int: ...

    @abc.abstractmethod
    def update_columns(
        self,
        target_table: str,
        key_cols: Sequence[str],
        set_cols: Sequence[str],
        rows: Iterable[Sequence[object]],
    ) -> int:
        """Apply a keyed, column-subset UPDATE and return the rows actually matched.

        Each row in *rows* is a flat tuple in SQL order: the *set_cols* values
        first, then the *key_cols* values — i.e. ``UPDATE <table> SET <set_cols> =
        <set-values> WHERE <key_cols> = <key-values>``. The return value is the
        summed row count actually **matched** (0 when a key matched nothing), which
        the CDC apply uses both as an existence probe and to decide whether to fall
        back to an insert. Every implementation reports rows matched rather than
        rows changed (the psycopg/PG-wire and native/Oracle UPDATE naturally counts
        a matched-but-unchanged row; the MySQL driver connects with
        ``CLIENT_FOUND_ROWS`` to do the same), so a value-unchanged re-apply still
        reports the match.

        Unlike :meth:`upsert`, this touches *only* ``set_cols`` and leaves every
        other column of the matched row exactly as it was. That matters because the
        upsert implementations differ: the **native** (Oracle) upsert is a
        DELETE-by-key + INSERT that NULLs any column the caller omitted, whereas the
        psycopg (``ON CONFLICT DO UPDATE``) and MySQL (``ON DUPLICATE KEY UPDATE``)
        upserts merge and so preserve an omitted column on a conflict but still
        default it on a fresh insert. ``update_columns`` never deletes the row, so
        it keeps an unchanged-TOAST partial image safe on *every* driver, and lets a
        primary-key-changing UPDATE move a row (``set_cols`` may include the key
        columns) without deleting the parent row first.
        """

    @abc.abstractmethod
    def delete_keys(
        self, target_table: str, key_cols: Sequence[str], keys: Iterable[Sequence[object]]
    ) -> int: ...

    @abc.abstractmethod
    def truncate(self, target_table: str) -> None: ...
