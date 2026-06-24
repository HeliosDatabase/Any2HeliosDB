"""Default source→HeliosDB type mappings.

Seeded from the HeliosDB Rust migration toolkit
(``Full/tools/migration/src/schema/mapping.rs``) and hardened for real-world
catalog type strings (e.g. ``TIMESTAMP(6) WITH TIME ZONE``). Each mapper returns
a :class:`~any2heliosdb.core.catalog_model.DataType`. Unmapped types become
``DataType(CUSTOM, custom=<verbatim>)`` so the emitter can pass them through and
record a target-gap rather than guessing.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from ..core.catalog_model import DataType, DataTypeKind as K

_PARAMS = re.compile(r"\(([^)]*)\)")


def _params(type_str: str) -> Optional[str]:
    m = _PARAMS.search(type_str)
    return m.group(1) if m else None


def _pscale(type_str: str, default_p: int = 38, default_s: int = 0) -> Tuple[int, int]:
    p = _params(type_str)
    if not p:
        return default_p, default_s
    parts = [x.strip() for x in p.split(",")]
    try:
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
        if len(parts) == 1 and parts[0]:
            return int(parts[0]), 0
    except ValueError:
        pass
    return default_p, default_s


def _len(type_str: str, default: int) -> int:
    p = _params(type_str)
    if not p:
        return default
    try:
        return int(p.split(",")[0].strip())
    except ValueError:
        return default


def map_oracle_type(src: str) -> DataType:
    u = src.upper().strip()
    if u.startswith("NUMBER") or u.startswith("DECIMAL"):
        if _params(u) is None:
            # Bare NUMBER (no precision/scale): Oracle allows any magnitude/scale,
            # so pinning DECIMAL(38,0) would silently truncate fractions (0.125 ->
            # 0). Emit unconstrained NUMERIC and let the emitter pick a
            # high-fidelity, non-truncating target type.
            return DataType(K.NUMERIC)
        p, s = _pscale(u)
        return DataType.decimal(p, s)
    if u.startswith("VARCHAR2") or u.startswith("NVARCHAR2") or u.startswith("VARCHAR"):
        return DataType.varchar(_len(u, 255))
    if u.startswith("NCHAR") or u.startswith("CHAR"):
        return DataType.char(_len(u, 1))
    if u.startswith("FLOAT") or u == "BINARY_FLOAT":
        return DataType.of(K.REAL)
    if u == "BINARY_DOUBLE":
        return DataType.of(K.DOUBLE_PRECISION)
    if u in ("INTEGER", "INT"):
        return DataType.of(K.INTEGER)
    if u == "SMALLINT":
        return DataType.of(K.SMALLINT)
    if u in ("CLOB", "NCLOB", "LONG"):
        return DataType.of(K.TEXT)
    if u in ("BLOB", "RAW", "LONG RAW") or u.startswith("RAW"):
        return DataType.of(K.BYTEA)
    if u.startswith("INTERVAL"):
        return DataType.of(K.INTERVAL)
    if u.startswith("TIMESTAMP"):
        if "TIME ZONE" in u:  # WITH TIME ZONE or WITH LOCAL TIME ZONE
            return DataType.of(K.TIMESTAMPTZ)
        return DataType.of(K.TIMESTAMP)
    if u == "DATE":
        # Oracle DATE carries a time component → TIMESTAMP, per the toolkit.
        return DataType.of(K.TIMESTAMP)
    if u in ("ROWID", "UROWID", "XMLTYPE"):
        return DataType.of(K.TEXT)
    return DataType(K.CUSTOM, custom=src)


def map_mysql_type(src: str) -> DataType:
    u = src.upper().strip()
    if u == "TINYINT(1)" or u in ("BOOLEAN", "BOOL"):
        return DataType.of(K.BOOLEAN)
    if u.startswith("DECIMAL") or u.startswith("NUMERIC"):
        p, s = _pscale(u)
        return DataType.numeric(p, s)
    if u.startswith("VARCHAR"):
        return DataType.varchar(_len(u, 255))
    if u.startswith("CHAR"):
        return DataType.char(_len(u, 1))
    if u == "TINYINT" or u == "SMALLINT" or u == "YEAR":
        return DataType.of(K.SMALLINT)
    if u in ("MEDIUMINT", "INT", "INTEGER"):
        return DataType.of(K.INTEGER)
    if u == "BIGINT":
        return DataType.of(K.BIGINT)
    if u == "FLOAT":
        return DataType.of(K.REAL)
    if u in ("DOUBLE", "DOUBLE PRECISION", "REAL"):
        return DataType.of(K.DOUBLE_PRECISION)
    if u in ("TEXT", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT"):
        return DataType.of(K.TEXT)
    if u in ("BLOB", "TINYBLOB", "MEDIUMBLOB", "LONGBLOB", "BINARY", "VARBINARY"):
        return DataType.of(K.BYTEA)
    if u == "DATE":
        return DataType.of(K.DATE)
    if u == "TIME":
        return DataType.of(K.TIME)
    if u in ("DATETIME", "TIMESTAMP"):
        return DataType.of(K.TIMESTAMP)
    if u == "JSON":
        return DataType.of(K.JSONB)
    if u.startswith("ENUM") or u.startswith("SET"):
        return DataType.of(K.TEXT)
    return DataType(K.CUSTOM, custom=src)


def map_mssql_type(src: str) -> DataType:
    u = src.upper().strip()
    if u in ("VARCHAR(MAX)", "NVARCHAR(MAX)", "TEXT", "NTEXT"):
        return DataType.of(K.TEXT)
    if u.startswith("VARCHAR") or u.startswith("NVARCHAR"):
        return DataType.varchar(_len(u, 255))
    if u.startswith("NCHAR") or u.startswith("CHAR"):
        return DataType.char(_len(u, 1))
    if u.startswith("DECIMAL") or u.startswith("NUMERIC"):
        p, s = _pscale(u)
        return DataType.numeric(p, s)
    if u in ("MONEY", "SMALLMONEY"):
        return DataType.decimal(19, 4)
    if u == "TINYINT" or u == "SMALLINT":
        return DataType.of(K.SMALLINT)
    if u in ("INT", "INTEGER"):
        return DataType.of(K.INTEGER)
    if u == "BIGINT":
        return DataType.of(K.BIGINT)
    if u == "REAL":
        return DataType.of(K.REAL)
    if u == "FLOAT":
        return DataType.of(K.DOUBLE_PRECISION)
    if u in ("BINARY", "VARBINARY", "VARBINARY(MAX)", "IMAGE") or u.startswith("VARBINARY"):
        return DataType.of(K.BYTEA)
    if u == "DATE":
        return DataType.of(K.DATE)
    if u == "TIME":
        return DataType.of(K.TIME)
    if u in ("DATETIME", "DATETIME2", "SMALLDATETIME"):
        return DataType.of(K.TIMESTAMP)
    if u == "DATETIMEOFFSET":
        return DataType.of(K.TIMESTAMPTZ)
    if u == "BIT":
        return DataType.of(K.BOOLEAN)
    if u == "UNIQUEIDENTIFIER":
        return DataType.of(K.UUID)
    if u == "XML":
        return DataType.of(K.TEXT)
    return DataType(K.CUSTOM, custom=src)


def map_postgresql_type(src: str) -> DataType:
    u = src.upper().strip()
    if u.startswith("VARCHAR") or u.startswith("CHARACTER VARYING"):
        if _params(u) is None:
            # Unbounded varchar (no declared length): leave length unset so the
            # emitter can avoid pinning an arbitrary width that might truncate.
            return DataType(K.VARCHAR)
        return DataType.varchar(_len(u, 255))
    if u.startswith("CHAR") or u.startswith("CHARACTER"):
        return DataType.char(_len(u, 1))
    if u.startswith("NUMERIC") or u.startswith("DECIMAL"):
        if _params(u) is None:
            # Bare NUMERIC (no precision/scale, e.g. an unconstrained PG column or
            # a HeliosDB numeric that exposes no size): leave precision/scale
            # unset so the emitter can choose a high-fidelity, non-truncating type
            # rather than the (38,0)-scale-0 default, which would drop the fraction.
            return DataType(K.NUMERIC)
        p, s = _pscale(u)
        return DataType.numeric(p, s)
    simple = {
        "SMALLINT": K.SMALLINT, "INT2": K.SMALLINT,
        "INTEGER": K.INTEGER, "INT": K.INTEGER, "INT4": K.INTEGER,
        "BIGINT": K.BIGINT, "INT8": K.BIGINT,
        "REAL": K.REAL, "FLOAT4": K.REAL,
        "DOUBLE PRECISION": K.DOUBLE_PRECISION, "FLOAT8": K.DOUBLE_PRECISION,
        "TEXT": K.TEXT, "BYTEA": K.BYTEA, "DATE": K.DATE,
        "BOOLEAN": K.BOOLEAN, "BOOL": K.BOOLEAN, "JSON": K.JSON, "JSONB": K.JSONB,
        "UUID": K.UUID, "INTERVAL": K.INTERVAL,
        "TIMESTAMP": K.TIMESTAMP, "TIMESTAMPTZ": K.TIMESTAMPTZ,
        "TIME": K.TIME,
    }
    if u in simple:
        return DataType.of(simple[u])
    if u.startswith("TIMESTAMP"):
        # "TIMESTAMP WITHOUT TIME ZONE" contains the substring "TIME ZONE" too, so
        # match the full "WITH TIME ZONE" (which is NOT a substring of "WITHOUT
        # TIME ZONE") to avoid promoting a naive timestamp to tz-aware.
        return DataType.of(K.TIMESTAMPTZ if "WITH TIME ZONE" in u else K.TIMESTAMP)
    # PostgreSQL information_schema reports enums/composites/domains as
    # USER-DEFINED and arrays as ARRAY; tsvector/xml/network/bit types have no
    # portable HeliosDB equivalent. Fall back to TEXT (values are preserved as
    # text) so a real-PostgreSQL source migrates instead of emitting an
    # unparseable type name like "USER-DEFINED". ENUM/array values round-trip
    # losslessly as text; refine with a DATA_TYPE override when a target type exists.
    if u in ("USER-DEFINED", "ARRAY", "TSVECTOR", "TSQUERY", "XML",
             "CIDR", "INET", "MACADDR", "MACADDR8", "BIT", "BIT VARYING", "MONEY"):
        return DataType.of(K.TEXT)
    return DataType(K.CUSTOM, custom=src)


# HeliosDB/PostgreSQL target type-name parser (for DATA_TYPE/MODIFY_TYPE overrides).
def parse_target_type(spec: str) -> DataType:
    u = spec.upper().strip()
    if u.startswith("VARCHAR"):
        return DataType.varchar(_len(u, 255))
    if u.startswith("CHAR"):
        return DataType.char(_len(u, 1))
    if u.startswith("NUMERIC"):
        p, s = _pscale(u)
        return DataType.numeric(p, s)
    if u.startswith("DECIMAL"):
        p, s = _pscale(u)
        return DataType.decimal(p, s)
    base = {
        "SMALLINT": K.SMALLINT, "INTEGER": K.INTEGER, "INT": K.INTEGER, "BIGINT": K.BIGINT,
        "REAL": K.REAL, "DOUBLE PRECISION": K.DOUBLE_PRECISION, "TEXT": K.TEXT,
        "BYTEA": K.BYTEA, "DATE": K.DATE, "TIME": K.TIME, "TIMESTAMP": K.TIMESTAMP,
        "TIMESTAMPTZ": K.TIMESTAMPTZ, "BOOLEAN": K.BOOLEAN, "JSON": K.JSON, "JSONB": K.JSONB,
        "UUID": K.UUID, "INTERVAL": K.INTERVAL, "SERIAL": K.SERIAL, "BIGSERIAL": K.BIGSERIAL,
    }
    if u in base:
        return DataType.of(base[u])
    return DataType(K.CUSTOM, custom=spec)


MAPPERS = {
    "oracle": map_oracle_type,
    "mysql": map_mysql_type,
    "mssql": map_mssql_type,
    "postgresql": map_postgresql_type,
}
