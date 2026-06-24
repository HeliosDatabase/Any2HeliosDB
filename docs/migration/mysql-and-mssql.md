# Migrating MySQL and SQL Server

## MySQL → HeliosDB — validated ✅

MySQL is a first-class source: the adapter introspects `information_schema` into
the same canonical IR as Oracle, so the whole pipeline (assess → migrate →
validate → resume) and the `psycopg` target driver work unchanged across
**Nano / Lite / Full**. Validated end-to-end (migrate + `test-count` +
`test-data`, 0 mismatches) on all three editions.

### Quick start

```bash
pip install -e ".[mysql]"           # pulls in PyMySQL
export MYSQL_PW='...'
a2h wizard                           # choose dialect: mysql
# or hand-write config.toml:
```

```toml
[source]
dialect = "mysql"
host = "127.0.0.1"
port = 3306
database = "hr"        # the MySQL schema/database to migrate
schema   = "hr"
user = "root"
password_env = "MYSQL_PW"

[target]
driver = "psycopg"
host = "127.0.0.1"
port = 5432
dbname = "postgres"
user = "postgres"

[options]
output_dir = "./out"
```

```bash
a2h migrate    -c config.toml
a2h test-count -c config.toml
a2h test-data  -c config.toml
```

### What the adapter handles

- **Introspection** — tables, columns, primary keys, foreign keys, indexes, and
  views from `information_schema`.
- **Type mapping** (`typemap/defaults.map_mysql_type`): `INT`→`INTEGER`,
  `BIGINT`→`BIGINT`, `DECIMAL(p,s)`/`NUMERIC`→`NUMERIC(p,s)`, `VARCHAR(n)`→`VARCHAR(n)`,
  `DATETIME`/`TIMESTAMP`→`TIMESTAMP`, `DATE`→`DATE`, `TIME`→`TIME`, `TEXT*`→`TEXT`,
  `BLOB*`/`BINARY`/`VARBINARY`→`BYTEA`, `JSON`→`JSONB`, `ENUM`/`SET`→`TEXT`, and
  **`TINYINT(1)`→`BOOLEAN`** (the MySQL boolean convention).
- **Streaming extraction** via a server-side cursor (`SSCursor`), so large tables
  don't materialize in memory.
- **Identifiers** — the connection runs in `ANSI_QUOTES` mode so the chunker's
  double-quoted range predicates parse as identifiers; the parallel + resumable
  loader works exactly as for Oracle.
- **Semantics that differ from Oracle**: MySQL keeps `''` distinct from `NULL`
  (Oracle folds `''`→`NULL`), and `TINYINT(1)` values arrive as `1`/`0` and land
  as `BOOLEAN`. Validation normalizes both so checksums agree.

### Current limitations

- **View bodies** are best-effort: MySQL emits them with backtick-quoted,
  schema-qualified identifiers, which the adapter strips to portable SQL; complex
  views may still need manual review (the migrate step warns, non-fatal).
- **Column defaults**: only `CURRENT_TIMESTAMP` and numeric literals are replayed;
  string/expression defaults are skipped (the data carries the values).
- **CDC**: change capture is Oracle-only today (SCN-watermark). MySQL **binlog**
  (ROW + GTID) capture is on the [CDC roadmap](../cdc.md); the Extract → trail →
  Replicat spine is source-agnostic, so it drops in there.

## SQL Server → HeliosDB — validated ✅

The MSSQL adapter (`sources/mssql/adapter.py`, via **pyodbc**) introspects
`sys.*` / `INFORMATION_SCHEMA` into the canonical IR — tables, columns (with the
NVARCHAR byte→char and `MAX`-sentinel handling so the type map matches), primary
keys, foreign keys (the **actual referenced columns**), indexes, and views — and
the whole pipeline runs unchanged. Validated end-to-end (migrate + `test` /
`test-count` / `test-data`) against **SQL Server 2022 → HeliosDB-Full**.

```bash
pip install -e ".[mssql]"            # pyodbc + a Microsoft ODBC driver (msodbcsql18)
export MSSQL_PW='...'
```

```toml
[source]
dialect = "mssql"
host = "127.0.0.1"
port = 1433
database = "hr"
schema = "dbo"
user = "sa"
password_env = "MSSQL_PW"

[target]
driver = "psycopg"
host = "127.0.0.1"
port = 5432
dbname = "postgres"
user = "postgres"
```

Verified fidelity: `BIT`→`BOOLEAN`, `DATETIME2`→`TIMESTAMP`, `DECIMAL(p,s)` exact,
`NVARCHAR(MAX)`/`VARBINARY(MAX)` byte-perfect (incl. embedded NULs), unicode, and
`''` kept distinct from NULL. `[bracket]` identifier quoting throughout.

**CDC** for MSSQL (`fn_cdc_get_all_changes`) is the remaining log-based roadmap
item; bulk migrate + validate are fully supported today.
