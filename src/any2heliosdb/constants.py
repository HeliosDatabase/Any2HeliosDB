"""Cross-cutting enums and constants.

These are intentionally ``str``-valued enums so they serialize cleanly into
TOML/JSON (config files, run manifests, gap reports) without custom encoders.
"""
from __future__ import annotations

from enum import Enum


class SourceDialect(str, Enum):
    """A supported migration *source* database."""

    ORACLE = "oracle"
    MYSQL = "mysql"
    MSSQL = "mssql"
    POSTGRESQL = "postgresql"
    HELIOSDB = "heliosdb"  # for reverse / HeliosDB-as-source (CDC, migrate-back)


class Edition(str, Enum):
    """A PostgreSQL-wire target edition. Capability differences between these are
    discovered at runtime by the capability probe, never assumed from this value
    alone. ``POSTGRES`` is a stock PostgreSQL server — a2h targets it via the same
    ``psycopg`` driver as HeliosDB, so it is a first-class migration target too."""

    NANO = "nano"
    LITE = "lite"
    FULL = "full"
    POSTGRES = "postgres"
    UNKNOWN = "unknown"


class TargetDriverKind(str, Enum):
    """Which target driver moves data into HeliosDB.

    ``PSYCOPG`` is the portable PG-wire path (works on every edition; the tool
    translates the source dialect). ``NATIVE`` connects through HeliosDB's
    same-dialect listener (e.g. Oracle TNS) so HeliosDB performs the translation
    and the tool transforms little or nothing. ``MYSQL`` is a heterogeneous /
    migrate-back target: a MySQL 8 server reached over the MySQL wire protocol
    (the GoldenGate-reverse direction — data flows out of HeliosDB to MySQL).
    """

    PSYCOPG = "psycopg"
    NATIVE = "native"
    MYSQL = "mysql"


class ExportType(str, Enum):
    """Object/work categories, mirroring Ora2Pg's ``-t TYPE`` taxonomy."""

    # Schema objects
    TABLE = "TABLE"
    VIEW = "VIEW"
    MVIEW = "MVIEW"
    SEQUENCE = "SEQUENCE"
    SEQUENCE_VALUES = "SEQUENCE_VALUES"
    INDEXES = "INDEXES"
    CONSTRAINT = "CONSTRAINT"
    FOREIGN_KEY = "FOREIGN_KEY"
    TRIGGER = "TRIGGER"
    FUNCTION = "FUNCTION"
    PROCEDURE = "PROCEDURE"
    PACKAGE = "PACKAGE"
    TYPE = "TYPE"
    PARTITION = "PARTITION"
    GRANT = "GRANT"
    SYNONYM = "SYNONYM"
    TABLESPACE = "TABLESPACE"
    # Data
    INSERT = "INSERT"
    COPY = "COPY"
    # Assessment / validation
    SHOW_VERSION = "SHOW_VERSION"
    SHOW_SCHEMA = "SHOW_SCHEMA"
    SHOW_TABLE = "SHOW_TABLE"
    SHOW_COLUMN = "SHOW_COLUMN"
    SHOW_REPORT = "SHOW_REPORT"
    TEST = "TEST"
    TEST_COUNT = "TEST_COUNT"
    TEST_DATA = "TEST_DATA"


# Export types that produce schema DDL (loaded before data).
SCHEMA_EXPORT_TYPES = frozenset(
    {
        ExportType.TABLE,
        ExportType.VIEW,
        ExportType.MVIEW,
        ExportType.SEQUENCE,
        ExportType.INDEXES,
        ExportType.CONSTRAINT,
        ExportType.FOREIGN_KEY,
        ExportType.TRIGGER,
        ExportType.FUNCTION,
        ExportType.PROCEDURE,
        ExportType.PACKAGE,
        ExportType.TYPE,
        ExportType.PARTITION,
        ExportType.GRANT,
        ExportType.SYNONYM,
        ExportType.TABLESPACE,
    }
)

# Procedural objects written to review files by default (not auto-loaded).
REVIEW_EXPORT_TYPES = frozenset(
    {
        ExportType.FUNCTION,
        ExportType.PROCEDURE,
        ExportType.PACKAGE,
        ExportType.TRIGGER,
    }
)


class WriteMode(str, Enum):
    """How a chunk of rows is written to the target."""

    COPY = "copy"
    INSERT = "insert"


class Severity(str, Enum):
    """Severity for target-gap items and validation findings."""

    BLOCKER = "blocker"
    DEGRADED = "degraded"
    COSMETIC = "cosmetic"


# Numbered output files, matching the documented Ora2Pg→HeliosDB workflow so
# load order is correct (data before FKs; sequence resets last).
OUTPUT_FILE_ORDER = {
    ExportType.TABLE: "01_tables.sql",
    ExportType.SEQUENCE: "02_sequences.sql",
    ExportType.VIEW: "03_views.sql",
    ExportType.MVIEW: "04_mviews.sql",
    ExportType.INDEXES: "05_indexes.sql",
    ExportType.TYPE: "06_types.sql",
    ExportType.GRANT: "80_grants.sql",
    ExportType.SEQUENCE_VALUES: "90_sequence_values.sql",
    ExportType.FOREIGN_KEY: "95_fk_constraints.sql",
}

# Default local TCP ports HeliosDB editions listen on for the PG-wire protocol.
DEFAULT_PG_PORT = 5432
