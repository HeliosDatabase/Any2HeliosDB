# Type mapping reference (Oracle → HeliosDB)

Type resolution is **data-driven and overridable**, mirroring Ora2Pg's two knobs
on top of a default mapper. The rules below are the verbatim behavior of
[`typemap/defaults.py`](../../src/any2heliosdb/typemap/defaults.py) (`map_oracle_type`)
and the DDL rendering in
[`core/catalog_model.py`](../../src/any2heliosdb/core/catalog_model.py) (`DataType.sql`).

The Oracle adapter records both a **resolved** target type and the **verbatim
source type** (e.g. `NUMBER(10,2)`) for every column, so the emitter can
re-resolve with your overrides and the assessment report can show provenance.

## Resolution order

For each column, the [`TypeRegistry`](../../src/any2heliosdb/typemap/registry.py)
resolves in this precedence:

1. **`MODIFY_TYPE`** — per-column override keyed `schema.table.column` (or
   `table.column`). Highest precedence.
2. **`DATA_TYPE`** — global override by source type name (matches the base name,
   so `NUMBER` matches `NUMBER(10,2)`).
3. **Default dialect mapping** — the table below.

Both overrides are configured in `config.toml`; see
[configuration](../guides/configuration.md#type-overrides-data_type--modify_type).

## Oracle → HeliosDB default type table

| Oracle source type | HeliosDB / PG target | Notes |
|---|---|---|
| `NUMBER(p,s)` | `DECIMAL(p,s)` | `p`/`s` parsed from the type; defaults `p=38, s=0` when unspecified. |
| `NUMBER(p)` | `DECIMAL(p,0)` | Scale defaults to 0. |
| `NUMBER(1)` | `DECIMAL(1,0)` | (No special boolean folding for Oracle `NUMBER(1)`.) |
| `NUMBER` (bare) | `DECIMAL(38,0)` | |
| `DECIMAL(...)` | `DECIMAL(p,s)` | Same path as `NUMBER`. |
| `VARCHAR2(n)` / `NVARCHAR2(n)` / `VARCHAR(n)` | `VARCHAR(n)` | Length parsed; default 255. |
| `CHAR(n)` / `NCHAR(n)` | `CHAR(n)` | Length parsed; default 1. |
| `FLOAT(...)` / `BINARY_FLOAT` | `REAL` | |
| `BINARY_DOUBLE` | `DOUBLE PRECISION` | |
| `INTEGER` / `INT` | `INTEGER` | |
| `SMALLINT` | `SMALLINT` | |
| `CLOB` / `NCLOB` / `LONG` | `TEXT` | |
| `BLOB` / `RAW(n)` / `LONG RAW` | `BYTEA` | |
| `INTERVAL ...` | `INTERVAL` | |
| `TIMESTAMP(p)` | `TIMESTAMP` | |
| `TIMESTAMP(p) WITH [LOCAL] TIME ZONE` | `TIMESTAMP WITH TIME ZONE` | `TIMESTAMPTZ`. |
| `DATE` | `TIMESTAMP` | Oracle `DATE` carries a time component, so it maps to `TIMESTAMP` (per the HeliosDB toolkit), **not** `DATE`. |
| `ROWID` / `UROWID` / `XMLTYPE` | `TEXT` | |
| *anything else* | passed through verbatim | Recorded as a `CUSTOM` type + a target gap, rather than guessed. |

> **Rendering note:** `DataType.sql()` renders `DECIMAL`/`NUMERIC` as
> `DECIMAL(p, s)` and `TIMESTAMPTZ` as `TIMESTAMP WITH TIME ZONE`.

## NULL vs empty string

Oracle folds the empty string `''` to `NULL`. The tool **preserves whatever
Oracle returns**: a column that is `NULL` in Oracle lands as SQL `NULL`; a genuine
empty string (where one exists) is kept distinct from `NULL`. The wizard's smoke
test explicitly round-trips a `('', NULL)` pair to prove the target keeps them
distinct (`null_empty_fidelity`).

Edition caveat: **HeliosDB-Full folds `'' → NULL` on COPY load** (Oracle-compatible,
benign for Oracle migrations); **Nano and Lite keep `'' ≠ NULL`**. See the
[Full migration guide](../migration/oracle-to-heliosdb-full.md).

## Defaults that are rewritten

Column defaults are mostly replayed verbatim. The PG-path emitter rewrites only the
always-safe Oracle defaults
([`emit/ddl.py`](../../src/any2heliosdb/emit/ddl.py) `_translate_default`):

| Oracle default | HeliosDB default |
|---|---|
| `SYSDATE` / `SYSTIMESTAMP` / `CURRENT_TIMESTAMP` | `CURRENT_TIMESTAMP` |
| `SYS_GUID()` / `SYS_GUID` | `gen_random_uuid()` |

A `NULL` or empty default string is dropped (the column simply has no default).

## Override provenance in the assessment report

`a2h assess` lists every column as `{table, column, source_type, target_sql,
provenance}`, where `provenance` is one of `default`, `data_type_override`, or
`modify_type_override` — so you can see exactly which columns you remapped and
which used the default rule.

## Native (Oracle-wire) path

On the **`native`** driver, the type map is largely bypassed: the emitter
([`emit/oracle_ddl.py`](../../src/any2heliosdb/emit/oracle_ddl.py)) replays the
**verbatim Oracle source type** (`NUMBER(10)`, `VARCHAR2(100)`, `DATE`, …) and
keeps source (upper) case identifiers, letting HeliosDB's Oracle surface perform
the translation. The table above therefore applies to the `psycopg` path; on
`native`, HeliosDB owns the type semantics.

## Other source dialects

The same engine ships mappers for MySQL, SQL Server, and PostgreSQL sources
(`map_mysql_type`, `map_mssql_type`, `map_postgresql_type` in
[`typemap/defaults.py`](../../src/any2heliosdb/typemap/defaults.py)). All three
source adapters are **validated end-to-end** (see the
[README compatibility matrix](../../README.md#compatibility-matrix)): the MySQL
and SQL Server mappings are detailed in
[MySQL & SQL Server](../migration/mysql-and-mssql.md), and PostgreSQL as a
*source* is validated end-to-end (Pagila — see the
[compatibility matrix](../../README.md#compatibility-matrix) and
[examples](../guides/examples.md)).
