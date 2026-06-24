"""Canonical intermediate representation (IR) for a database schema.

Mirrors the struct shapes in HeliosDB's Rust migration toolkit
(``Full/tools/migration/src/schema/mod.rs``) so the two tools stay conceptually
interoperable, but expressed as Python dataclasses. Source adapters populate
this IR; the ``emit`` layer renders HeliosDB DDL from it.

The IR is dialect-neutral. :class:`DataType` carries the *resolved* target type
(what the type map produced) and can render its HeliosDB/PostgreSQL DDL via
:meth:`DataType.sql`, mirroring the reference ``generate_data_type_ddl``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class DataTypeKind(str, Enum):
    SMALLINT = "smallint"
    INTEGER = "integer"
    BIGINT = "bigint"
    DECIMAL = "decimal"
    NUMERIC = "numeric"
    REAL = "real"
    DOUBLE_PRECISION = "double_precision"
    SERIAL = "serial"
    BIGSERIAL = "bigserial"
    CHAR = "char"
    VARCHAR = "varchar"
    TEXT = "text"
    BYTEA = "bytea"
    DATE = "date"
    TIME = "time"
    TIMESTAMP = "timestamp"
    TIMESTAMPTZ = "timestamptz"
    INTERVAL = "interval"
    BOOLEAN = "boolean"
    JSON = "json"
    JSONB = "jsonb"
    UUID = "uuid"
    ARRAY = "array"
    CUSTOM = "custom"


@dataclass(frozen=True)
class DataType:
    """A resolved target data type.

    ``precision``/``scale`` apply to DECIMAL/NUMERIC, ``length`` to CHAR/VARCHAR,
    ``element`` to ARRAY, and ``custom`` carries a verbatim type name for
    CUSTOM (an unmapped source type passed through with a gap recorded).
    """

    kind: DataTypeKind
    precision: Optional[int] = None
    scale: Optional[int] = None
    length: Optional[int] = None
    element: Optional["DataType"] = None
    custom: Optional[str] = None

    # --- convenience constructors mirroring the reference enum variants ---
    @classmethod
    def varchar(cls, length: int = 255) -> "DataType":
        return cls(DataTypeKind.VARCHAR, length=length)

    @classmethod
    def char(cls, length: int = 1) -> "DataType":
        return cls(DataTypeKind.CHAR, length=length)

    @classmethod
    def decimal(cls, precision: int = 38, scale: int = 0) -> "DataType":
        return cls(DataTypeKind.DECIMAL, precision=precision, scale=scale)

    @classmethod
    def numeric(cls, precision: int = 38, scale: int = 0) -> "DataType":
        return cls(DataTypeKind.NUMERIC, precision=precision, scale=scale)

    @classmethod
    def array_of(cls, element: "DataType") -> "DataType":
        return cls(DataTypeKind.ARRAY, element=element)

    @classmethod
    def of(cls, kind: DataTypeKind) -> "DataType":
        return cls(kind)

    def sql(self) -> str:
        """Render the HeliosDB/PostgreSQL DDL fragment for this type.

        Mirrors ``generate_data_type_ddl`` in the reference toolkit.
        """
        k = self.kind
        simple = {
            DataTypeKind.SMALLINT: "SMALLINT",
            DataTypeKind.INTEGER: "INTEGER",
            DataTypeKind.BIGINT: "BIGINT",
            DataTypeKind.REAL: "REAL",
            DataTypeKind.DOUBLE_PRECISION: "DOUBLE PRECISION",
            DataTypeKind.SERIAL: "SERIAL",
            DataTypeKind.BIGSERIAL: "BIGSERIAL",
            DataTypeKind.TEXT: "TEXT",
            DataTypeKind.BYTEA: "BYTEA",
            DataTypeKind.DATE: "DATE",
            DataTypeKind.TIME: "TIME",
            DataTypeKind.TIMESTAMP: "TIMESTAMP",
            DataTypeKind.TIMESTAMPTZ: "TIMESTAMP WITH TIME ZONE",
            DataTypeKind.INTERVAL: "INTERVAL",
            DataTypeKind.BOOLEAN: "BOOLEAN",
            DataTypeKind.JSON: "JSON",
            DataTypeKind.JSONB: "JSONB",
            DataTypeKind.UUID: "UUID",
        }
        if k in simple:
            return simple[k]
        if k in (DataTypeKind.DECIMAL, DataTypeKind.NUMERIC):
            base = "DECIMAL" if k is DataTypeKind.DECIMAL else "NUMERIC"
            # Unconstrained numeric (no precision): emit it bare so an arbitrary-
            # precision source value is not pinned to scale 0 (which truncates the
            # fraction). PostgreSQL/HeliosDB accept a bare NUMERIC.
            if self.precision is None:
                return base
            s = self.scale if self.scale is not None else 0
            return "{}({}, {})".format(base, self.precision, s)
        if k is DataTypeKind.CHAR:
            return "CHAR({})".format(self.length if self.length is not None else 1)
        if k is DataTypeKind.VARCHAR:
            return "VARCHAR({})".format(self.length if self.length is not None else 255)
        if k is DataTypeKind.ARRAY:
            inner = self.element.sql() if self.element is not None else "TEXT"
            return "{}[]".format(inner)
        if k is DataTypeKind.CUSTOM:
            return self.custom or "TEXT"
        return "TEXT"


class ReferentialAction(str, Enum):
    CASCADE = "cascade"
    SET_NULL = "set_null"
    SET_DEFAULT = "set_default"
    RESTRICT = "restrict"
    NO_ACTION = "no_action"


class SortOrder(str, Enum):
    ASC = "asc"
    DESC = "desc"


class NullsOrder(str, Enum):
    FIRST = "first"
    LAST = "last"


class IndexType(str, Enum):
    BTREE = "btree"
    HASH = "hash"
    GIN = "gin"
    GIST = "gist"
    BITMAP = "bitmap"  # source-only; downgraded to BTREE on emit + gap


class ConstraintKind(str, Enum):
    CHECK = "check"
    UNIQUE = "unique"
    NOT_NULL = "not_null"


class ParameterMode(str, Enum):
    IN = "in"
    OUT = "out"
    INOUT = "inout"


class PartitionType(str, Enum):
    RANGE = "range"
    LIST = "list"
    HASH = "hash"


class RoutineKind(str, Enum):
    FUNCTION = "function"
    PROCEDURE = "procedure"


@dataclass
class Column:
    name: str
    data_type: DataType
    nullable: bool = True
    default: Optional[str] = None
    auto_increment: bool = False
    comment: Optional[str] = None
    # Verbatim source type (e.g. "NUMBER(10,2)") for assessment/provenance.
    source_type: Optional[str] = None


@dataclass
class PrimaryKey:
    columns: List[str]
    name: Optional[str] = None


@dataclass
class ForeignKey:
    columns: List[str]
    references_table: str
    references_columns: List[str]
    name: Optional[str] = None
    on_delete: Optional[ReferentialAction] = None
    on_update: Optional[ReferentialAction] = None


@dataclass
class IndexColumn:
    name: str
    order: Optional[SortOrder] = None
    nulls: Optional[NullsOrder] = None


@dataclass
class Index:
    name: str
    columns: List[IndexColumn]
    unique: bool = False
    index_type: IndexType = IndexType.BTREE
    condition: Optional[str] = None  # partial-index WHERE clause


@dataclass
class Constraint:
    constraint_type: ConstraintKind
    name: Optional[str] = None
    # For CHECK: the expression; for UNIQUE: column list joined; for NOT_NULL: column.
    expression: Optional[str] = None
    columns: List[str] = field(default_factory=list)


@dataclass
class Partition:
    name: str
    value: str


@dataclass
class PartitionInfo:
    partition_type: PartitionType
    columns: List[str]
    partitions: List[Partition] = field(default_factory=list)


@dataclass
class TableOptions:
    storage_engine: Optional[str] = None
    tablespace: Optional[str] = None
    partition: Optional[PartitionInfo] = None


@dataclass
class Table:
    name: str
    schema: Optional[str] = None
    columns: List[Column] = field(default_factory=list)
    primary_key: Optional[PrimaryKey] = None
    foreign_keys: List[ForeignKey] = field(default_factory=list)
    indexes: List[Index] = field(default_factory=list)
    constraints: List[Constraint] = field(default_factory=list)
    options: TableOptions = field(default_factory=TableOptions)
    comment: Optional[str] = None

    @property
    def fqn(self) -> str:
        return "{}.{}".format(self.schema, self.name) if self.schema else self.name

    def target_name(self, preserve_case: bool = False) -> str:
        """The unqualified table name as it lands in the HeliosDB target.

        The migration loads into the target's default schema (no source-schema
        prefix); identifiers are lowercased unless ``preserve_case``. This is the
        single source of truth shared by the emitter, the loader, and the
        validators so they can never disagree on a table's target name.
        """
        return self.name if preserve_case else self.name.lower()


@dataclass
class View:
    name: str
    definition: str
    materialized: bool = False
    schema: Optional[str] = None


@dataclass
class Sequence:
    name: str
    start: int = 1
    increment: int = 1
    min_value: Optional[int] = None
    max_value: Optional[int] = None
    cache: int = 1
    cycle: bool = False
    schema: Optional[str] = None


@dataclass
class Parameter:
    name: str
    data_type: DataType
    mode: ParameterMode = ParameterMode.IN
    default: Optional[str] = None


@dataclass
class Routine:
    """A standalone function or procedure (PACKAGE members expand to these)."""

    name: str
    kind: RoutineKind
    parameters: List[Parameter] = field(default_factory=list)
    return_type: Optional[DataType] = None
    body: str = ""
    language: str = "plpgsql"
    schema: Optional[str] = None


@dataclass
class Trigger:
    name: str
    table: str
    timing: str = "BEFORE"  # BEFORE | AFTER | INSTEAD OF
    events: List[str] = field(default_factory=list)  # INSERT/UPDATE/DELETE
    body: str = ""
    schema: Optional[str] = None


@dataclass
class UserType:
    name: str
    definition: str
    schema: Optional[str] = None


@dataclass
class Synonym:
    name: str
    target_object: str
    schema: Optional[str] = None


@dataclass
class Grant:
    grantee: str
    privilege: str
    object_name: str


@dataclass
class Schema:
    name: str
    tables: List[Table] = field(default_factory=list)
    views: List[View] = field(default_factory=list)
    sequences: List[Sequence] = field(default_factory=list)
    routines: List[Routine] = field(default_factory=list)
    triggers: List[Trigger] = field(default_factory=list)
    types: List[UserType] = field(default_factory=list)
    synonyms: List[Synonym] = field(default_factory=list)
    grants: List[Grant] = field(default_factory=list)
