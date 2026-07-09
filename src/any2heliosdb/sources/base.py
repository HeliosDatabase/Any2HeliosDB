"""Source-adapter abstraction (introspection + extraction).

One ABC, three implementations (Oracle first; MySQL/MSSQL follow). Introspection
populates the canonical IR (:mod:`any2heliosdb.core.catalog_model`); extraction
streams rows for the data engine. The two are deliberately separate so the
parallel/CDC engine can drive extraction without re-introspecting.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Iterator, List, Optional, Sequence, Tuple

from ..constants import SourceDialect
from ..core.catalog_model import Schema, Table


@dataclass
class SourceDsn:
    host: str = "127.0.0.1"
    port: int = 1521
    # Oracle uses service_name (or sid); MySQL/MSSQL use database.
    service_name: Optional[str] = None
    sid: Optional[str] = None
    database: Optional[str] = None
    user: str = ""
    password: str = ""
    schema: Optional[str] = None  # schema to migrate (defaults to the user's own)
    # Oracle-only connection options:
    #   thick      — use python-oracledb thick mode (Oracle Instant Client). Required
    #                for servers that mandate Native Network Encryption / Data
    #                Integrity (thin mode raises DPY-3001).
    #   client_dir — Instant Client lib dir (else found via PATH/LD_LIBRARY_PATH).
    #   sysdba     — connect with SYSDBA privilege (needed for the SYS user).
    thick: bool = False
    client_dir: Optional[str] = None
    sysdba: bool = False


class SourceAdapter(abc.ABC):
    dialect: SourceDialect

    def __init__(self, dsn: SourceDsn) -> None:
        self.dsn = dsn

    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "SourceAdapter":
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @abc.abstractmethod
    def server_version(self) -> str: ...

    @abc.abstractmethod
    def default_schema(self) -> str: ...

    @abc.abstractmethod
    def list_schemas(self) -> List[str]: ...

    @abc.abstractmethod
    def introspect_schema(self, schema: Optional[str] = None) -> Schema:
        """Read the catalog and return the populated IR for one schema."""

    @abc.abstractmethod
    def exact_row_count(self, table: Table) -> int: ...

    def numeric_pk_bounds(self, table: Table, pk_col: str):
        """``(min, max)`` of an integer PK column, or ``None`` if the column is
        non-integer or the table is empty. Used to split a table into key-range
        chunks for parallel/resumable load. The default returns ``None`` (no
        range chunking — a single whole-table chunk); adapters override it."""
        return None

    def capture_snapshot(self) -> Optional[str]:
        """Capture a read-consistency token at plan time so a chunked + resumable
        load reads ONE consistent view of the source. Default ``None`` (no
        snapshot — quiesce the source); the Oracle adapter returns its SCN."""
        return None

    def use_snapshot(self, token: Optional[str]) -> None:
        """Pin subsequent reads to *token* from :meth:`capture_snapshot`. No-op by
        default; the Oracle adapter issues ``AS OF SCN`` reads."""
        return None

    @abc.abstractmethod
    def stream_rows(
        self,
        table: Table,
        columns: Sequence[str],
        where: Optional[str] = None,
        arraysize: int = 1000,
    ) -> Iterator[Tuple]:
        """Yield row tuples for *columns* of *table* (optionally filtered)."""
