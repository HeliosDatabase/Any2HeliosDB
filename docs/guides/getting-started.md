# Getting started

This walks you from a clean machine to a validated Oracle → HeliosDB migration and
a first CDC cycle, with real command transcripts. For per-edition specifics, jump
to the migration guide for your target:
[Lite](../migration/oracle-to-heliosdb-lite.md) ·
[Full](../migration/oracle-to-heliosdb-full.md) ·
[Nano](../migration/oracle-to-heliosdb-nano.md).

## Prerequisites

- **Python 3.9+**.
- **An Oracle source** you can read (the `oracledb` thin driver needs no Oracle
  client install). You need a user that can read the `ALL_*` data-dictionary views
  for the schema you're migrating, plus `SELECT` on its tables.
- **A running HeliosDB target** (Nano, Lite, or Full) reachable over the
  PostgreSQL wire protocol — see the [compatibility matrix](../../README.md#compatibility-matrix)
  for the minimum build per edition.
- For CDC capture, ideally `SELECT` on a flashback function (`dbms_flashback` or
  `timestamp_to_scn`) so the watermark advances; without it the engine falls back
  to a full re-capture each cycle (still correct — apply is idempotent).

## Install

```bash
pip install -e ".[oracle]"      # Oracle source + the psycopg (PG-wire) target
```

Extras: `.[mysql]`, `.[mssql]`, `.[all]`, `.[dev]`. The Oracle extra installs
`oracledb`; the core install already has `psycopg[binary]`, `typer`, `rich`,
`jinja2`, and the TOML libraries.

## Step 1 — `a2h doctor`

Confirm the environment before anything else:

```
$ a2h doctor
                    Any2HeliosDB environment
┏━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Component     ┃ Status    ┃ Detail                                ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ psycopg (v3)  │ available │ target: PG-wire driver [core]         │
│ oracledb      │ available │ source: Oracle / native target [oracle]│
│ typer         │ available │ CLI [core]                            │
│ rich          │ available │ CLI UX [core]                         │
└───────────────┴───────────┴───────────────────────────────────────┘
```

If `psycopg` is missing the core install failed; if `oracledb` is missing, install
the `oracle` extra.

## Step 2 — passwords go in environment variables

Passwords are **never stored in the config**. Each side names an environment
variable that is resolved at runtime:

```bash
export ORACLE_PW='hr'
export HELIOS_PW='heliosdb'    # omit for a trust target
```

(A literal `password` is accepted in the config as a dev convenience for trust
targets, but should stay empty in anything you commit.)

## Step 3 — the wizard

`a2h wizard` is the modern replacement for hand-editing `ora2pg.conf`. It prompts
for both ends, runs a **smoke test**, and writes `config.toml`.

```
$ a2h wizard
Any2HeliosDB setup wizard
Source database
  dialect [oracle/mysql/mssql] (oracle): oracle
  host (127.0.0.1): 127.0.0.1
  port (1521): 1521
  user: hr
  password env var (recommended; leave blank to enter literal): ORACLE_PW
  service_name (XEPDB1): XEPDB1
  schema to migrate (blank = the user's own):
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
  target_banner          PostgreSQL 17.0 (HeliosDB-Lite 2.0) on ...
  target_edition         lite
  copy_from_stdin        True OK
  enforces_check         True
  enforces_fk            True
  null_empty_fidelity    True OK

wrote config.toml. Next: a2h assess then a2h migrate.
```

The smoke test connects both ends, detects the **edition**, **probes
capabilities**, and round-trips a tiny `('', NULL)` COPY to prove the target keeps
empty string distinct from `NULL` (`null_empty_fidelity`). A subset of the probe
result is cached into the config's `[capability]` block for reference.

If the smoke test fails (e.g. the target is down) the wizard offers to save the
config anyway so you can fix connectivity and re-run.

## Step 4 — assess (optional but recommended)

A read-only inventory + type-mapping + cost estimate. The target is not touched.

```bash
a2h assess -c config.toml              # text to stdout
a2h assess -c config.toml -f json -o assessment.json
a2h assess -c config.toml -f html -o assessment.html
```

It lists every object, and for each column shows `source_type → target_sql` plus
the **provenance** (`default`, `data_type_override`, or `modify_type_override`),
so you can confirm the type mapping and your overrides before loading.

## Step 5 — migrate

```
$ a2h migrate -c config.toml
migrated 2 tables, 8 rows (load_mode=copy)
  DEPARTMENTS -> 3
  EMPLOYEES -> 5
```

`migrate` introspects the source, emits + applies DDL (tables, sequences, indexes;
foreign keys are added **after** data), then loads data using the **resumable,
parallel** engine: each table is split into PK-range chunks, chunks load
concurrently, and each chunk is idempotent (range-delete + load in one
transaction). `load_mode` is `copy` when the probe allowed COPY, else `insert`
(e.g. on Nano).

The run is tracked in a SQLite manifest at `<output_dir>/manifest.db`
(default `./migration_output/manifest.db`).

To watch a long migration live, run `a2h monitor -c config.toml` in another
terminal — a read-only full-screen dashboard (per-table progress, volume left,
ETA) that attaches to the same run (see the
[CLI reference](../reference/cli.md#a2h-monitor)).

## Step 6 — status & resume

If a load is interrupted, inspect and continue it:

```
$ a2h status -c config.toml
run run_a1b2c3d4e5  states={'loaded': 3, 'failed': 1}  rows_loaded=6
  hr.DEPARTMENTS -> 3
  hr.EMPLOYEES -> 3
  1 chunk(s) pending/failed — run `a2h resume`

$ a2h resume -c config.toml
resumed: 8 rows across 2 tables
```

`resume` skips DDL and re-runs only the pending/failed chunks. Because chunks are
idempotent, this never duplicates rows.

## Step 7 — validate

Three checks, each exiting non-zero on mismatch (so they gate CI):

```
$ a2h test -c config.toml
TEST: PASS
  metrics: {'tables_checked': 2, 'tables_ok': 2}

$ a2h test-count -c config.toml
TEST_COUNT: PASS
  metrics: {'tables_checked': 2, 'tables_matched': 2, 'tables_mismatched': 0}

$ a2h test-data -c config.toml
TEST_DATA: PASS
  metrics: {'rows_compared': 3, 'mismatches': 0}
TEST_DATA: PASS
  metrics: {'rows_compared': 5, 'mismatches': 0}
```

- **`test`** — every source table exists on the target with the same column count
  (catalog-free, works on any edition).
- **`test-count`** — exact row counts match.
- **`test-data`** — PK-ordered, per-row SHA-256 comparison. Use `--sample 0` to
  compare every row. Binary is hashed as hex so BLOB/BYTEA compares correctly
  across drivers.

## Step 8 — CDC (optional)

After the initial load, keep the target current with the Extract → trail →
Replicat cycle (full model in [docs/cdc.md](../cdc.md)):

```
$ a2h extract cdc1 -c config.toml
extract cdc1: captured 8 change(s) (full snapshot); watermark=2547881

# ... changes happen in Oracle ...

$ a2h extract cdc1 -c config.toml
extract cdc1: captured 2 change(s) (incremental since SCN 2547881); watermark=2547990

$ a2h replicat cdc1 -c config.toml
replicat cdc1: applied 2 change(s), deleted 0, from 2 read; cursor=10

$ a2h extracts -c config.toml
  cdc1             schema=HR tables=2 watermark=2547990 cursor=10 state=applying
```

> CDC apply (`replicat`) is **validated on HeliosDB-Full and HeliosDB-Lite**, and
> on **HeliosDB-Nano ≥ 3.58.5** (older Nano builds are refused with a clear
> error). MySQL binlog and PostgreSQL logical decoding are also supported as
> log-based sources. See [docs/cdc.md](../cdc.md), the
> [operations runbook](cdc-operations.md), and your migration guide.

## `config.toml` reference

The wizard writes this; you can also edit it by hand. Full details and tuning in
[configuration](configuration.md).

```toml
[source]
dialect = "oracle"           # oracle | mysql | mssql
host = "127.0.0.1"
port = 1521
service_name = "XEPDB1"      # Oracle: service name (or use `sid`)
# sid = "XE"                  # Oracle: alternative to service_name
# database = "appdb"          # MySQL/MSSQL instead of service_name/sid
user = "hr"
password_env = "ORACLE_PW"   # name of the env var holding the password
schema = "HR"                # blank = the connecting user's own schema

[target]
driver = "psycopg"           # psycopg (default, every edition) | native (Oracle-wire, experimental)
host = "127.0.0.1"
port = 5432
dbname = "postgres"
user = "postgres"
password_env = "HELIOS_PW"   # omit for a trust target
# sslmode = "require"

[options]
output_dir = "./migration_output"   # holds manifest.db and the CDC trail
batch_size = 1000                    # source fetch arraysize / INSERT batch
parallelism = 4                      # parallel load workers (chunks)
prefer_copy = true                   # use COPY when the target supports it
preserve_case = false                # false = lowercase identifiers (recommended)
drop_existing = true                 # DROP target tables before re-creating

# Optional Ora2Pg-style overrides:
[data_type]                          # source-type-name -> target-type (global)
# NUMBER = "bigint"
[modify_type]                        # schema.table.column -> target-type
# "hr.emp.salary" = "numeric(12,2)"
```

This is the starter set. Additional keys the wizard does not prompt for exist and
are documented in full in [configuration](configuration.md): a `mysql`
migrate-back [`[target].driver`](configuration.md#driver-selection),
[`manifest_backend = "nano"`](configuration.md#options) for the embedded-Nano
resume ledger, the Oracle thick-mode options (`thick` / `client_dir` / `sysdba`)
for Native-Network-Encryption servers, and a whole
[`[cdc]`](configuration.md#cdc-change-data-capture-tuning) section that tunes the
CDC pipeline. Prefer editing those there rather than duplicating this block.

## Where to next

- **[Configuration](configuration.md)** — every field, env-var passwords, driver
  selection, and tuning.
- **Your migration guide** — [Lite](../migration/oracle-to-heliosdb-lite.md),
  [Full](../migration/oracle-to-heliosdb-full.md),
  [Nano](../migration/oracle-to-heliosdb-nano.md).
- **[CLI reference](../reference/cli.md)** — every command and option.
- **[MCP server](../mcp.md)** — expose the whole toolkit as MCP tools
  (`a2h mcp serve`, Bearer auth + RBAC) so an AI agent can drive a migration
  remotely.
- **[Troubleshooting](../troubleshooting.md)** — common errors and the per-edition
  HeliosDB gaps.
