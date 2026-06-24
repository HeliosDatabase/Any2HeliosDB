# Worked examples

Copy-pasteable, end-to-end scenarios for `a2h`. Each one shows the commands, the
`config.toml` (or the relevant slice), and the **shape** of the output you should
see. Every command and behavior here is grounded in the code and the integration
tests; for the exhaustive per-command spec see the
[CLI reference](../reference/cli.md), and for every config field see
[configuration](../guides/configuration.md). The docs index is
[docs/README.md](../README.md).

All scenarios use the same small **`hr`** sample schema the test fixtures build —
two tables, `departments` (3 rows) and `employees` (5 rows, with a FK to
`departments`, a `NUMBER(1)`/`TINYINT(1)`/`BIT` "active" flag, a `salary`
`NUMBER(10,2)`/`DECIMAL(10,2)`, a CLOB/TEXT, and a BLOB/`VARBINARY` photo). The
rows deliberately include NULLs, an empty string, a unicode name, and an embedded
tab/newline, so they exercise null/empty-string, numeric, boolean, and binary
fidelity. Object counts in the transcripts (`2 tables, 8 rows`) reflect that
fixture.

> Output is shown as **shapes** — exact identifiers, row counts, SCNs, and binlog
> coordinates depend on your data and server. The `migrated 2 tables, 8 rows`
> style lines and the `TEST*: PASS … metrics: {…}` lines are verbatim from the
> tool.

**Contents**

- [Scenario 1 — Oracle → HeliosDB-Full](#scenario-1)
- [Scenario 2 — MySQL → HeliosDB-Lite](#scenario-2)
- [Scenario 3 — HeliosDB → MySQL (migrate-back)](#scenario-3)
- [Scenario 4 — SQL Server → HeliosDB](#scenario-4)
- [Scenario 5 — CDC: Oracle SCN-watermark & MySQL binlog](#scenario-5)
- [Scenario 6 — Interrupted load → status → resume](#scenario-6)
- [Scenario 7 — `[data_type]` / `[modify_type]` overrides](#scenario-7)

---

## Scenario 1 — Oracle → HeliosDB-Full {#scenario-1}

The canonical path: `wizard → assess → migrate → test-count → test-data`. Full is
the most capable edition (COPY, concurrent transactions, CDC), so this is the
smoothest target. See also the
[Oracle → Full migration guide](../migration/oracle-to-heliosdb-full.md).

**Install + passwords.**

```bash
pip install -e ".[oracle]"          # Oracle source + the psycopg PG-wire target
export ORACLE_PW='hr'
export HELIOS_PW='heliosdb'         # omit for a trust target
a2h doctor                          # confirm psycopg + oracledb are 'available'
```

**1. Configure with the wizard** (writes `config.toml`):

```
$ a2h wizard
Any2HeliosDB setup wizard
Source database
  dialect [oracle/mysql/mssql] (oracle): oracle
  host (127.0.0.1): db.internal
  port (1521): 1521
  user: hr
  password env var (recommended; leave blank to enter literal): ORACLE_PW
  service_name (XEPDB1): XEPDB1
  schema to migrate (blank = the user's own): HR
HeliosDB target
  driver [psycopg/native] (psycopg): psycopg
  host (127.0.0.1): 127.0.0.1
  port (5432): 5432
  database (postgres): postgres
  user (postgres): postgres
  password env var (blank for trust): HELIOS_PW

Running smoke test...
  source_version         21.0.0.0.0
  source_schema          HR
  target_banner          PostgreSQL 14.0 (HeliosDB) on ...
  target_edition         full
  copy_from_stdin        True OK
  enforces_check         True
  enforces_fk            True
  null_empty_fidelity    True OK

wrote config.toml. Next: a2h assess then a2h migrate.
```

The resulting `config.toml`:

```toml
[source]
dialect = "oracle"
host = "db.internal"
port = 1521
service_name = "XEPDB1"
user = "hr"
password_env = "ORACLE_PW"
schema = "HR"

[target]
driver = "psycopg"
host = "127.0.0.1"
port = 5432
dbname = "postgres"
user = "postgres"
password_env = "HELIOS_PW"

[options]
output_dir = "./migration_output"
batch_size = 1000
parallelism = 8                # Full handles concurrent transactions well
prefer_copy = true
preserve_case = false
drop_existing = true
```

**2. Assess** (read-only; target untouched):

```bash
a2h assess -c config.toml                          # text to stdout
a2h assess -c config.toml -f html -o assessment.html
```

```
============================================================
HeliosDB Migration Assessment
============================================================
Source dialect : oracle
Schema         : HR
Object inventory
------------------------------------------------------------
  tables        : 2
  columns       : 11
  views         : 1
  sequences     : 1
Tables
------------------------------------------------------------
  EMPLOYEES (9 columns)
      EMP_ID                   NUMBER(10) -> bigint
      SALARY                   NUMBER(10,2) -> numeric(10,2)
      ACTIVE                   NUMBER(1) -> smallint
      PHOTO                    BLOB -> bytea
      ...
Estimated migration cost: 0.0 person-days
============================================================
```

**3. Migrate** (schema + parallel, resumable data load; COPY on Full):

```
$ a2h migrate -c config.toml
migrated 2 tables, 8 rows (load_mode=copy)
  DEPARTMENTS -> 3
  EMPLOYEES -> 5
```

**4. Validate** (each exits non-zero on mismatch — gate CI by `&&`-chaining):

```
$ a2h test-count -c config.toml
TEST_COUNT: PASS
  metrics: {'tables_checked': 2, 'tables_matched': 2, 'tables_mismatched': 0}

$ a2h test-data -c config.toml --sample 0          # 0 = compare every row
TEST_DATA: PASS
  metrics: {'rows_compared': 3, 'mismatches': 0}
TEST_DATA: PASS
  metrics: {'rows_compared': 5, 'mismatches': 0}
```

`test-data` is per-row SHA-256 over PK-ordered rows; binary, numeric, and boolean
values are canonicalized so they compare equal across drivers (so the BLOB photo
and the `NUMBER(10,2)` salary match exactly).

---

## Scenario 2 — MySQL → HeliosDB-Lite {#scenario-2}

MySQL is a first-class source; the adapter reads `information_schema` into the
same IR as Oracle, so the whole pipeline works unchanged. Lite is validated
end-to-end via the `psycopg` driver. The one MySQL-specific mapping to know:
**`TINYINT(1)` → `BOOLEAN`** (the MySQL boolean convention), and unlike Oracle,
MySQL keeps `''` distinct from `NULL` — `test-data` normalizes both so checksums
still agree.

```bash
pip install -e ".[mysql]"           # pulls in PyMySQL
export MYSQL_PW='root'
```

Hand-written `config.toml` (or run `a2h wizard` and pick `mysql`):

```toml
[source]
dialect = "mysql"
host = "127.0.0.1"
port = 3306
database = "hr"        # the MySQL database/schema to migrate
schema = "hr"
user = "root"
password_env = "MYSQL_PW"

[target]
driver = "psycopg"
host = "127.0.0.1"
port = 5432
dbname = "postgres"
user = "postgres"
# password_env = "HELIOS_PW"   # omit for a trust target

[options]
output_dir = "./out"
parallelism = 1        # Lite rejects concurrent txns today; 1 avoids wasted
                       # parallel attempts (the serial-retry pass converges anyway)
```

```bash
a2h migrate    -c config.toml
a2h test-count -c config.toml
a2h test-data  -c config.toml
```

```
$ a2h migrate -c config.toml
migrated 2 tables, 8 rows (load_mode=copy)
  departments -> 3
  employees -> 5

$ a2h test-data -c config.toml
TEST_DATA: PASS
  metrics: {'rows_compared': 3, 'mismatches': 0}
TEST_DATA: PASS
  metrics: {'rows_compared': 5, 'mismatches': 0}
```

The `active TINYINT(1)` column lands as `boolean`; check it in the assessment:

```
$ a2h assess -c config.toml | grep -i active
      ACTIVE                   tinyint(1) -> boolean
```

> **Lite notes.** Lite rejects concurrent transactions today, so the loader runs a
> serial-retry mop-up pass — the load converges with no duplicates. On a current
> Lite build, migrate, validate, **and CDC apply** are all green. See the
> [Oracle → Lite guide](../migration/oracle-to-heliosdb-lite.md) (the
> Lite-specific behavior is identical for a MySQL source).

---

## Scenario 3 — HeliosDB → MySQL (migrate-back) {#scenario-3}

The engine runs **any-to-any**: a PostgreSQL-wire **source** adapter reads a
HeliosDB (or stock PostgreSQL) server *out*, and the **MySQL target** driver
writes to a MySQL 8 server — data flows **back out of HeliosDB** (the
GoldenGate-reverse direction). This is **not offered by the wizard** — hand-write
the config with `dialect = "postgresql"` and `driver = "mysql"`.

```bash
pip install -e ".[mysql]"           # PyMySQL for the MySQL target; psycopg is core
export HELIOS_PW='heliosdb'
export MYSQL_PW='root'
```

`config.toml` — HeliosDB as the **source**, MySQL as the **target**:

```toml
[source]
dialect = "postgresql"   # a PG-wire source: read HeliosDB (or PostgreSQL) out
host = "127.0.0.1"
port = 5432              # HeliosDB's PG-wire port
database = "postgres"
schema = "public"
user = "postgres"
password_env = "HELIOS_PW"

[target]
driver = "mysql"         # the heterogeneous / migrate-back sink (MySQL 8)
host = "127.0.0.1"
port = 3306
dbname = "hr_back"       # destination MySQL database (create it first)
user = "root"
password_env = "MYSQL_PW"

[options]
output_dir = "./out-back"
```

```bash
a2h migrate    -c config.toml
a2h test-count -c config.toml          # row-parity is the gate here (see below)
```

```
$ a2h migrate -c config.toml
migrated 2 tables, 8 rows (load_mode=insert)
  departments -> 3
  employees -> 5

$ a2h test-count -c config.toml
TEST_COUNT: PASS
  metrics: {'tables_checked': 2, 'tables_matched': 2, 'tables_mismatched': 0}
```

Two things differ from a load *into* HeliosDB:

- **`load_mode=insert`.** The MySQL target has no `COPY FROM STDIN`; bulk load is
  array INSERT, and idempotent upsert uses `INSERT … ON DUPLICATE KEY UPDATE`.
- **`test-count` is the structural gate.** When HeliosDB is the source its PG-wire
  catalog exposes no PK/FK/index metadata and no column precision/scale today, so
  chunking falls back to a single whole-table chunk and `test-data` self-handles
  the PK-less case (sorted-multiset compare). Row-count parity is the reliable
  check when HeliosDB is the source, because its PG-wire catalog does not expose
  PK/FK/index metadata. `a2h test` (column count) still works (it is
  catalog-free).

> A full Oracle → MySQL forward run uses the same MySQL target — set
> `[source].dialect = "oracle"` and `[target].driver = "mysql"`; on that path
> `test-data --sample 0` passes (the Oracle source exposes PKs).

---

## Scenario 4 — SQL Server → HeliosDB {#scenario-4}

SQL Server is a validated source via `pyodbc` (`sys.*` introspection). Notable
mappings: `BIT` → `BOOLEAN`, `DATETIME2` → `TIMESTAMP`, `NVARCHAR(MAX)` /
`VARBINARY(MAX)` carried byte-perfect; FK *referenced* columns are resolved from
`sys.foreign_key_columns` (not assumed to be the PK). A SQL Server instance nests
*database → schema (`dbo`) → table*; bind to one `database` and name the `schema`.

```bash
pip install -e ".[mssql]"           # pyodbc + you need a Microsoft ODBC driver
export MSSQL_PW='Strong!Passw0rd'
export HELIOS_PW='heliosdb'
a2h doctor                          # pyodbc should be 'available'
```

`config.toml`:

```toml
[source]
dialect = "mssql"
host = "127.0.0.1"
port = 1433
database = "hr"        # the SQL Server database
schema = "dbo"         # the schema within it (defaults to dbo if blank)
user = "sa"
password_env = "MSSQL_PW"

[target]
driver = "psycopg"
host = "127.0.0.1"
port = 5432
dbname = "postgres"
user = "postgres"
password_env = "HELIOS_PW"

[options]
output_dir = "./out-mssql"
```

```bash
a2h migrate    -c config.toml
a2h test       -c config.toml
a2h test-count -c config.toml
a2h test-data  -c config.toml --sample 0
```

```
$ a2h migrate -c config.toml
migrated 2 tables, 8 rows (load_mode=copy)
  departments -> 3
  employees -> 5

$ a2h test-data -c config.toml --sample 0
TEST_DATA: PASS
  metrics: {'rows_compared': 3, 'mismatches': 0}
TEST_DATA: PASS
  metrics: {'rows_compared': 5, 'mismatches': 0}
```

The adapter auto-selects the newest installed `ODBC Driver NN for SQL Server` and
connects with `TrustServerCertificate=yes` so a default-TLS SQL Server 2022 is
reachable without provisioning a CA on the migration host. CDC for SQL Server
(`fn_cdc_get_all_changes`) is roadmap; use migrate/resume for refreshes.

---

## Scenario 5 — CDC: Oracle SCN-watermark & MySQL binlog {#scenario-5}

After an initial `migrate`, keep the target current with the **Extract → trail →
Replicat** cycle. Apply (`replicat`) is **validated on Full and Lite**, and on
**Nano ≥ 3.58.3** (older Nano is refused at runtime with a clear error) — see the
[CDC doc](../cdc.md). The three verbs are detailed in the
[CLI reference](../reference/cli.md#change-data-capture).

### 5a. Oracle SCN-watermark with delete reconciliation

v1 Oracle capture re-reads changed rows via `ORA_ROWSCN`; a watermark scan can't
*see* deleted rows, so `replicat` reconciles deletes with a source/target key-set
diff (**on by default**). Use it against an Oracle source + HeliosDB-Full target
(reuse Scenario 1's `config.toml`):

```
# First cycle: a full snapshot becomes upsert records in the trail.
$ a2h extract cdc1 -c config.toml
extract cdc1: captured 8 change(s) (full snapshot); watermark=2547881

$ a2h replicat cdc1 -c config.toml      # delete reconciliation ON by default
replicat cdc1: applied 8 change(s), deleted 0, from 8 read; cursor=8

# ... rows change in Oracle: 2 updated, 1 deleted ...

$ a2h extract cdc1 -c config.toml
extract cdc1: captured 2 change(s) (incremental since SCN 2547881); watermark=2547990

$ a2h replicat cdc1 -c config.toml
replicat cdc1: applied 2 change(s), deleted 1, from 2 read; cursor=10

$ a2h extracts -c config.toml
  cdc1             schema=HR tables=2 watermark=2547990 cursor=10 state=applying
```

The capture **watermark** and the apply **cursor** advance independently (tracked
in `<output_dir>/cdc.db`), so you can schedule extract and replicat separately.
Apply is idempotent (`INSERT … ON CONFLICT DO UPDATE`), so re-running a trail
slice never duplicates. To skip reconciliation (apply-only):

```bash
a2h replicat cdc1 -c config.toml --no-deletes
```

### 5b. MySQL binlog (log-based, real deletes)

For a MySQL source, capture reads the ROW-format **binlog** directly, producing
real `I`/`U`/`D` records — **including deletes** — so you apply with
`--no-deletes` (the binlog already carries the `D` records; no key-set diff
needed).

**Install + server prerequisites:**

```bash
pip install -e ".[mysql-cdc]"       # PyMySQL + mysql-replication
```

On the MySQL server: `log_bin=ON`, `binlog_format=ROW`, and
**`binlog_row_metadata=FULL`** (required so binlog row events carry real column
names; the extract sets it best-effort when anchoring, otherwise set it
server-side), plus a user with `REPLICATION SLAVE` / `REPLICATION CLIENT`. Use
the Scenario 2 MySQL `config.toml` (target can be Full or Lite).

```
# 1. Baseline load, then anchor the binlog position (captures nothing yet).
$ a2h migrate -c config.toml
migrated 2 tables, 8 rows (load_mode=copy)

$ a2h extract b1 -c config.toml
extract b1: captured 0 change(s) (binlog since (current)); watermark=mysql-bin.000003:1421

# ... in MySQL: INSERT emp 6, UPDATE emp 1 salary, DELETE emp 5 ...

$ a2h extract b1 -c config.toml
extract b1: captured 3 change(s) (binlog since mysql-bin.000003:1421); watermark=mysql-bin.000003:1987

$ a2h replicat b1 -c config.toml --no-deletes   # binlog D records do the delete
replicat b1: applied 3 change(s), deleted 0, from 3 read; cursor=3
```

After this the target has the inserted row, the updated salary, and the deleted
row gone — propagated through the log. (`a2h extracts` shows the binlog coordinate
as the watermark.)

> **Nano gating.** CDC apply requires **Nano ≥ 3.58.3**. Against an older Nano,
> `a2h replicat` fails fast with a clear error rather than a cryptic mid-apply SQL
> error; upgrade Nano, or use migrate/resume for refreshes.

---

## Scenario 6 — Interrupted load → status → resume {#scenario-6}

The data load is tracked chunk-by-chunk in `<output_dir>/manifest.db`. If a load
is interrupted (Ctrl-C, a crashed worker, a transient target error), `migrate`
**exits non-zero** so automation notices, and you continue with `resume` — which
never duplicates rows because each chunk is idempotent (range-DELETE + load in one
transaction).

A partial `migrate` looks like this (note the non-zero-exit error line):

```
$ a2h migrate -c config.toml
migrated 2 tables, 6 rows (load_mode=copy)
  departments -> 3
  employees -> 3
  warn: chunk employees:1 not loaded: <transient target error>
error: 1 chunk(s) failed to load — the target is INCOMPLETE. Run `a2h status` then `a2h resume`.
$ echo $?
1
```

**Inspect** what's pending:

```
$ a2h status -c config.toml
run run_a1b2c3d4e5  states={'loaded': 3, 'failed': 1}  rows_loaded=6
  hr.departments -> 3
  hr.employees -> 3
  1 chunk(s) pending/failed — run `a2h resume`
```

**Resume** — skips DDL, re-runs only pending/failed chunks:

```
$ a2h resume -c config.toml
resumed: 8 rows across 2 tables

$ a2h status -c config.toml
run run_a1b2c3d4e5  states={'loaded': 4}  rows_loaded=8
  hr.departments -> 3
  hr.employees -> 5
  all chunks loaded
```

`status` and `resume` find the same run automatically — the `run_id` is derived
from the source→target coordinates, so you don't pass it. Re-running `resume`
when everything is loaded is a safe no-op. (`resume` reports the resumed counts;
re-run `a2h status` to confirm `all chunks loaded`.)

---

## Scenario 7 — `[data_type]` / `[modify_type]` overrides {#scenario-7}

Ora2Pg's two type-override knobs, applied on top of the
[default type map](../reference/type-mapping.md). Resolution precedence is
**`[modify_type]` → `[data_type]` → default mapping**.

- **`[data_type]`** — *global* remap by source-type **name**. Matched on the base
  name, so `NUMBER = "bigint"` remaps every `NUMBER` (including `NUMBER(10,2)`).
- **`[modify_type]`** — *per-column* override, keyed `schema.table.column` (or
  `table.column` without the schema), matched case-insensitively. Highest
  precedence.

Add them to any config (here, Scenario 1's Oracle → Full):

```toml
[data_type]
# Global: every CLOB becomes text (already the default; shown for shape).
CLOB = "text"

[modify_type]
# Per-column: pin the salary's precision/scale explicitly, and force the
# boolean-ish NUMBER(1) "active" flag to a real boolean on the target.
"hr.employees.salary" = "numeric(12,2)"
"hr.employees.active" = "boolean"
```

Confirm the result and its **provenance** before loading — `assess` shows, per
column, what the type resolved to and whether it came from a default or one of
your overrides (provenance is in the JSON/HTML report):

```bash
a2h assess -c config.toml -f json -o assessment.json
```

```json
{
  "type_provenance": [
    {"table": "HR.EMPLOYEES", "column": "SALARY",
     "source_type": "NUMBER(10,2)", "target_sql": "numeric(12,2)",
     "provenance": "modify_type_override"},
    {"table": "HR.EMPLOYEES", "column": "ACTIVE",
     "source_type": "NUMBER(1)", "target_sql": "boolean",
     "provenance": "modify_type_override"},
    {"table": "HR.EMPLOYEES", "column": "FULL_NAME",
     "source_type": "VARCHAR2(100)", "target_sql": "varchar(100)",
     "provenance": "default"}
  ]
}
```

Then `a2h migrate` emits the overridden types. The target-type string you supply
is parsed for `VARCHAR(n)`, `CHAR(n)`, `NUMERIC(p,s)`, `DECIMAL(p,s)`, and the
base types (`INTEGER`, `BIGINT`, `TEXT`, `BYTEA`, `TIMESTAMP`, `TIMESTAMPTZ`,
`BOOLEAN`, `JSON`, `JSONB`, `UUID`, `INTERVAL`, `SERIAL`, …); anything else is
passed through verbatim. See [configuration](../guides/configuration.md#type-overrides-data_type--modify_type)
for more.

---

## See also

- [CLI reference](../reference/cli.md) — every command, option, and exit code.
- [Configuration](../guides/configuration.md) — the full `config.toml` schema.
- [Getting started](../guides/getting-started.md) — install → first migration.
- Per-target migration guides:
  [Oracle → Lite](../migration/oracle-to-heliosdb-lite.md) ·
  [→ Full](../migration/oracle-to-heliosdb-full.md) ·
  [→ Nano](../migration/oracle-to-heliosdb-nano.md) ·
  [MySQL & SQL Server](../migration/mysql-and-mssql.md).
- [CDC](../cdc.md) — the Extract → trail → Replicat model.
- [docs/README.md](../README.md) — the documentation index.
