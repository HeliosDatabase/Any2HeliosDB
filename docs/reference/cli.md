# CLI reference (`a2h`)

`a2h` (alias `any2heliosdb`) is the single entry point — a modern, Python
successor to Ora2Pg for migrating **Oracle / MySQL / SQL Server** into **HeliosDB**
(Nano / Lite / Full). It is built with Typer + Rich; run `a2h --help` or
`a2h <command> --help` for the live signature of any command.

```bash
a2h [--version|-V] <command> [options]
```

Heavy imports are deferred into each command, so `a2h --help` and `a2h doctor`
work with only the core dependencies installed (you do not need `oracledb` /
`pymysql` / `pyodbc` just to read help or run `doctor`).

This page is the **authoritative reference** for every command, argument, option
(with its default), what each prints, exit codes, and prerequisites. It is
derived from [`src/any2heliosdb/cli.py`](../../src/any2heliosdb/cli.py). For
copy-pasteable end-to-end walkthroughs see the
[worked examples](../guides/examples.md); for the config file itself see
[configuration](../guides/configuration.md); the docs index is
[docs/README.md](../README.md).

---

## Commands at a glance

| Command | One-liner |
|---|---|
| `doctor` | Check the local environment: core deps + which source-DB drivers are available. |
| `wizard` | Interactively configure source + target and run a smoke test (replaces `ora2pg.conf`). |
| `assess` | Object inventory, type mapping, cost estimate, target gaps (read-only). |
| `export` | Export the source schema as HeliosDB DDL to a file (does not touch the target). |
| `migrate` | Full schema + data migration end to end (the primary command). |
| `load` | Alias that currently runs the full `migrate`. |
| `status` | Show progress of the current/last migration run (from the manifest). |
| `resume` | Resume an interrupted migration's data load from its manifest. |
| `monitor` | Live full-screen dashboard of a migration run (read-only; attaches to a running `migrate`). |
| `test` | TEST: object-inventory diff (source vs target). |
| `test-count` | TEST_COUNT: row counts on both sides. |
| `test-data` | TEST_DATA: ordered, sampled row comparison + checksums. |
| `test-index` | TEST_INDEX: target-side FK-index sanity (catches a stale/unbackfilled FK index). |
| `report` | Render the assessment report (text alias of `assess`). |
| `extract` | Capture source changes into a named CDC trail. |
| `replicat` | Apply a named trail to the target (idempotent). |
| `extracts` | List registered CDC extracts and their capture/apply positions. |
| `mcp serve` | Run the MCP server so AI agents can drive a2h remotely (Bearer auth + RBAC). |
| `mcp auth` | Generate a Bearer token into a private (0600) token file. |

---

## Global options & conventions

| Option | Purpose |
|---|---|
| `--version`, `-V` | Print the version (`a2h (any2heliosdb) <ver>`) and exit. Eager — works with no command. |
| `--help` | Show the (auto-generated) help for the app or a command and exit. |
| `-c`, `--config PATH` | Project config TOML. **Default: `config.toml`.** Accepted by every command **except** `doctor` and `wizard`. |

- **No-args behavior.** Running `a2h` with no command prints help (and exits 0).
- **Exit codes (uniform).** `0` = success / validation passed. `1` = a tool error
  (bad config, connection/introspection failure) **or** a validation mismatch
  (`test` / `test-count` / `test-data`) **or** a partial data load (`migrate`
  with failed chunks). Commands that compare or load therefore gate CI directly.
- **Friendly errors.** Tool exceptions are rendered as `error: <message>` rather
  than a Python traceback.

### Config file resolution

Every config-aware command loads the TOML named by `-c/--config` (default
`config.toml` in the working directory). If the file is missing you get
`config not found: <path> (run \`a2h wizard\`)`; if it is malformed you get
`could not parse <path>: <detail>`. See [configuration](../guides/configuration.md)
for the full schema (`[source]` / `[target]` / `[options]` / `[data_type]` /
`[modify_type]` / `[capability]`).

### Passwords come from environment variables

Passwords are **never stored in the config**. Each side names an env var via
`password_env`, resolved at runtime:

```bash
export ORACLE_PW='hr'
export HELIOS_PW='heliosdb'      # omit for a trust target
a2h migrate -c config.toml
```

A literal `password` field is honored only when `password_env` is **absent** (a
dev convenience for trust targets); keep it empty in committed configs. If
`password_env` is set but the variable is unset, the resolved password is empty
(you'll get an auth failure).

### `output_dir` layout

`[options].output_dir` (default `./migration_output`) holds the durable
bookkeeping a run needs:

```
<output_dir>/
  manifest.db          # resumable-load ledger (SQLite-WAL): runs, tables, chunks
  manifest.nano/       # ...instead, when [options] manifest_backend = "nano"
                       #    (an embedded HeliosDB-Nano/RocksDB directory, same ledger)
  cdc.db               # CDC registry: each extract's watermark + apply cursor
  trail/<name>/
    trail.jsonl        # durable, append-only, fsync'd change records (segment 0)
    trail.00001.jsonl  # rotated segments when [cdc] trail_rotate_mb > 0
    trail.meta         # purge bookkeeping (purged_lines) so the global cursor
                       #    stays valid after --purge-applied
    trail.lock         # advisory flock a replicat / --purge-applied run takes
    dead_letter.jsonl  # poison records parked by the replicat (tier-2 policy)
    binlog.pos         # log-based sources: MySQL "<file>:<pos>", or the mirrored
                       #    PostgreSQL LSN (the slot is the real cursor) for display
    epoch.id           # PostgreSQL logical only: "system_identifier:timeline_id"
                       #    so a PITR/cluster swap fails closed instead of corrupting
```

`status` / `resume` locate the same manifest (`manifest.db`, or the `manifest.nano`
directory) + a deterministic `run_id` derived from the source→target coordinates,
so they always find the ledger the `migrate` that created it wrote. The backend is
auto-detected on read (file ⇒ sqlite, directory ⇒ nano), so no extra flag is
needed. Each artifact is created on first use of its subsystem — `manifest.db`/
`manifest.nano` by the first `migrate`/`load`, `cdc.db` by the first CDC verb,
and each `trail/<name>/` artifact once the relevant feature is exercised.

### Supported source dialects × target drivers

`[source].dialect` selects the source adapter; `[target].driver` selects how data
reaches the destination.

| `dialect` | Driver dep | Notes |
|---|---|---|
| `oracle` | `oracledb` (`.[oracle]`) | python-oracledb thin mode; reads `ALL_*`. |
| `mysql` | `PyMySQL` (`.[mysql]`) | reads `information_schema`; `+mysql-replication` for binlog CDC (`.[mysql-cdc]`). |
| `mssql` | `pyodbc` + an ODBC driver (`.[mssql]`) | SQL Server; reads `sys.*`. |
| `postgresql` | `psycopg` (core) | a PG-wire **source** — read a HeliosDB/PostgreSQL server *out* (migrate-back). Not offered by the `wizard`; hand-write the config. |
| `heliosdb` | — | reserved enum for HeliosDB-as-source; use `postgresql` in practice. |

| `driver` | Wire protocol | Editions | Status |
|---|---|---|---|
| `psycopg` | PostgreSQL | Nano / Lite / Full | **validated, default** — the tool translates the source dialect; COPY fast path when the probe allows it, else INSERT. |
| `mysql` | MySQL (PyMySQL) | — (a MySQL 8 server) | **validated** heterogeneous / migrate-back sink — `INSERT … ON DUPLICATE KEY UPDATE`, no COPY. |
| `native` | Oracle TNS (`oracledb`) | Lite / Full | **experimental** — HeliosDB does the dialect translation; live parity test blocked on a TNS handshake fix. Oracle source only; selecting it with a non-Oracle source raises a config error. |

See the [README compatibility matrix](../../README.md#compatibility-matrix) for the
per-edition validation status and minimum HeliosDB builds.

---

## Environment & setup

### `a2h doctor`

```bash
a2h doctor
```

**Purpose.** Check the local Python environment: which core deps and source-DB
drivers are importable. Needs no config and connects to nothing.

**Arguments / options.** None.

**Prints.** A Rich table with one row per probed module (Component / Status /
Detail). `Status` is `available` (with the module version when exposed) or
`missing`.

```
$ a2h doctor
                    Any2HeliosDB environment
┏━━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Component     ┃ Status    ┃ Detail                                  ┃
┡━━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ psycopg (v3)  │ available │ target: PG-wire driver [core]           │
│ oracledb      │ available │ source: Oracle / native target [extra…] │
│ pymysql       │ missing   │ source: MySQL [extra: mysql]            │
│ pyodbc        │ missing   │ source: SQL Server [extra: mssql]       │
│ typer         │ available │ CLI [core]                              │
│ rich          │ available │ CLI UX [core]                           │
└───────────────┴───────────┴─────────────────────────────────────────┘
```

Probed modules: `psycopg`, `oracledb`, `pymysql`, `pyodbc`, `typer`, `rich`.
(MySQL **binlog** CDC additionally needs `mysql-replication`, not probed here;
install it with `pip install -e ".[mysql-cdc]"`.)

**Exit codes.** Always `0` — `doctor` reports, it does not fail.

### `a2h wizard`

```bash
a2h wizard
```

**Purpose.** Interactively configure source + target, run a connection **smoke
test**, and write `config.toml`. The modern replacement for hand-editing
`ora2pg.conf`.

**Arguments / options.** None (it is fully interactive). It always writes to
`config.toml` in the working directory.

**What it does.** Prompts for the source (dialect `oracle`/`mysql`/`mssql`, host,
port — defaulted per dialect: 1521/3306/1433 — user, `password_env` or a literal
password, then `service_name` for Oracle or `database` for MySQL/MSSQL, then the
schema to migrate) and the target (driver `psycopg`/`native`, host, port 5432,
dbname, user, `password_env`). Then it runs `smoke_test`: connect both ends, read
the source version + default schema, read the target banner, **probe
capabilities**, detect the **edition**, and round-trip a tiny `('', NULL)` COPY
(or INSERT) to prove the target keeps empty string distinct from `NULL`
(`null_empty_fidelity`).

**Prints.** The smoke-test report (one line per probe; `OK` flag on
`copy_from_stdin` and `null_empty_fidelity` when true), then
`wrote config.toml. Next: a2h assess then a2h migrate.`

```
$ a2h wizard
Any2HeliosDB setup wizard
Source database
  dialect [oracle/mysql/mssql] (oracle): oracle
  ...
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

A subset of the probe (`copy*`, `enforces*`, `target_edition`) is cached into the
config's `[capability]` block (informational only — the real probe re-runs at
every `migrate`). If the smoke test fails (e.g. the target is down) the wizard
asks whether to save the config anyway so you can fix connectivity and re-run.

> The wizard's dialect menu offers `oracle` / `mysql` / `mssql` only. For a
> **migrate-back** (PostgreSQL/HeliosDB source) hand-write `config.toml` with
> `dialect = "postgresql"` (see the [examples](../guides/examples.md#scenario-3)).

**Exit codes.** `0` on success or when you choose to save after a failed smoke
test; otherwise it returns without writing.

---

## Assessment & export

### `a2h assess`

```bash
a2h assess [-c config.toml] [-f text|json|html] [-o OUTPUT]
```

**Purpose.** Assess the migration before loading anything: object inventory,
per-column type mapping with provenance, and a coarse cost estimate. Read-only —
it connects and introspects the **source** only; the target is never touched.

| Option | Default | Purpose |
|---|---|---|
| `-f`, `--format` | `text` | Output format: `text` \| `json` \| `html`. An unknown value errors. |
| `-o`, `--output PATH` | — | Write the rendered report to a file instead of stdout (prints `wrote <path>`). |

**Prints.** For `text`: an inventory (counts of tables/columns/views/sequences/
etc.) and, per table, every column as `source_type -> target_sql`, plus the
estimated migration cost in person-days. `json` is the full structured report;
`html` is a standalone page. The per-column **provenance** (`default`,
`data_type_override`, or `modify_type_override`) is carried in the JSON/HTML and
lets you confirm your `[data_type]` / `[modify_type]` overrides took effect.

```
$ a2h assess -c config.toml
============================================================
HeliosDB Migration Assessment
============================================================
Source dialect : oracle
Target edition : unknown
Schema         : HR

Object inventory
------------------------------------------------------------
  tables        : 2
  columns       : 11
  views         : 1
  sequences     : 1
  ...
Tables
------------------------------------------------------------
  EMPLOYEES (9 columns)
      EMP_ID                   NUMBER(10) -> bigint
      SALARY                   NUMBER(10,2) -> numeric(10,2)
      ...
Estimated migration cost: 0.0 person-days
============================================================
```

> The cost estimate is a deliberately simple placeholder (weights per routine +
> trigger); the assess path does not probe the target, so `Target edition` shows
> `unknown` here.

**Prerequisites.** A reachable source and a config. **Exit codes.** `0` on
success; `1` on an unknown `--format` or a source connection/introspection error.

### `a2h export`

```bash
a2h export [-c config.toml] [-o schema.sql]
```

**Purpose.** Emit the source schema as HeliosDB DDL to a file. Connects the
**source** only (no target connection).

| Option | Default | Purpose |
|---|---|---|
| `-o`, `--output PATH` | `schema.sql` | DDL output file. |

**What it writes.** In load-safe order: `CREATE TABLE`s, sequences, indexes,
views, then foreign keys **last** (so data can load before they are enforced).
Identifiers follow `[options].preserve_case` (lowercased unless `true`).

**Prints.** `wrote <path> (<n> tables, <n> sequences, <n> views)`.

**Exit codes.** `0` on success; `1` on a source error.

> Over MCP the same DDL is available as the `export` tool (viewer role), which
> **returns the DDL + review text** instead of writing files — see
> [MCP server](../mcp.md). The CLI and the tool share one engine path
> (`core.export.build_ddl`), so both produce byte-identical DDL.

### `a2h report`

```bash
a2h report [-c config.toml] [-o OUTPUT]
```

**Purpose.** Convenience alias of `assess` pinned to `--format text`. Same
options as `assess` minus `--format`. Useful for a quick terminal summary or to
dump the text report to a file (`-o`).

---

## Migrate, load, status & resume

### `a2h migrate`

```bash
a2h migrate [-c config.toml]
```

**Purpose.** The primary command — run a full schema + data migration end to end.

**What it does.** Introspects the source; probes the target's capabilities (if
not already known); emits + applies DDL (tables, sequences, indexes — **foreign
keys are added after the data**, views last); then loads data. With `[options]`
present (always, via a config) it uses the **resumable, parallel,
manifest-tracked** loader: each table is split into integer-PK-range chunks,
chunks load concurrently (`parallelism`), and each chunk is **idempotent**
(range-DELETE + load in one transaction). The load mode is `copy` when
`prefer_copy` is on **and** the probe reports `copy_from_stdin`, else `insert`
(e.g. on Nano, or for the `mysql`/`native` targets which have no COPY). On Lite,
chunks that fail under concurrent-transaction contention are mopped up by an
automatic **serial-retry** pass.

| Option | Default | Purpose |
|---|---|---|
| `-c`, `--config PATH` | `config.toml` | Project config. |

(Tuning — `parallelism`, `batch_size`, `prefer_copy`, `preserve_case`,
`drop_existing`, `output_dir` — lives in the config's `[options]`, not as flags.
See [configuration](../guides/configuration.md#options).)

**Prints.** A summary line, per-table row counts, and any warnings:

```
$ a2h migrate -c config.toml
migrated 2 tables, 8 rows (load_mode=copy)
  DEPARTMENTS -> 3
  EMPLOYEES -> 5
```

**Exit codes.** `0` on a clean migration. **`1` if any chunk failed to load
after every retry** — the message is `error: N chunk(s) failed to load — the
target is INCOMPLETE. Run \`a2h status\` then \`a2h resume\`.` A partial load
must never look like success, so this fails loudly for CI/automation. (Other tool
errors — bad config, connection failure — also exit `1`.)

**Prerequisites.** A reachable source and target, and a config. Run
[`a2h doctor`](#a2h-doctor) first to confirm the driver for your dialect is
installed.

### `a2h load`

```bash
a2h load [-c config.toml]
```

**Purpose.** Currently delegates to `migrate` (a data-only path lands with the
data engine). Same options, output, and exit codes as `migrate`.

### `a2h status`

```bash
a2h status [-c config.toml]
```

**Purpose.** Show the progress of this config's migration run, read from the
manifest. Connects to nothing — it only reads `<output_dir>/manifest.db`.

**Prints.** The `run_id`, the chunk-state histogram, total rows loaded, per-table
rows loaded, and whether any chunks are pending/failed (prompting `a2h resume`):

```
$ a2h status -c config.toml
run run_a1b2c3d4e5  states={'loaded': 3, 'failed': 1}  rows_loaded=6
  hr.DEPARTMENTS -> 3
  hr.EMPLOYEES -> 3
  1 chunk(s) pending/failed — run `a2h resume`
```

When everything is loaded the last line is `all chunks loaded` (green).

**Exit codes.** `0` normally; `1` (`error: no manifest at <path> …`) if no run
has been started for this config yet.

### `a2h resume`

```bash
a2h resume [-c config.toml]
```

**Purpose.** Continue an interrupted load from its manifest. Skips DDL
(`do_schema=False`) and never drops (`drop_existing=False`); it recovers any
`in_progress` chunks to `pending` and re-runs only the pending/failed chunks.
Because chunks are idempotent (per-chunk range-DELETE + load in one transaction),
resuming **never duplicates rows**.

**Replays the recorded plan.** A resume **does not recompute** the chunk plan
from the live source — it replays the chunk predicates the manifest recorded when
the run was first planned, byte-for-byte. So if rows were inserted or deleted at
the source's primary-key edges between the original run and the resume, the load
still covers exactly the ranges it set out to cover (no silently skipped or
double-loaded PK ranges). The plan is only (re)computed from the current source on
a **fresh** `migrate` or when the plan-affecting config changed (which resets the
run). A table that appears in the source only on resume is planned fresh and
added; a table that was recorded but has since **vanished** from the source makes
`resume` **fail closed** (`resume aborted: … table(s) … no longer in the source`)
rather than silently drop its unloaded rows — fix the source or start a fresh run.

**Prints.** `resumed: <rows> rows across <tables> tables`, plus warnings.

```
$ a2h resume -c config.toml
resumed: 8 rows across 2 tables
```

**Exit codes.** `0` on success; `1` if no manifest exists for this config
(`error: no manifest to resume …`), if a recorded table vanished from the source
or the PK column changed mid-run (the fail-closed cases above), or if chunks are
still failed after the resume. After a resume that exits `0` with warnings, run
`a2h status` to confirm it reports `all chunks loaded`.

### `a2h monitor`

```bash
a2h monitor [-c config.toml] [--once] [--interval 1.0]
a2h monitor --manifest <path> --run-id <id>     # explicit, without a config
```

**Purpose.** A live, full-screen dashboard of a migration run. It reads the
manifest **read-only** (sqlite WAL allows a concurrent reader), so
`a2h monitor -c <cfg>` attaches to the very run a concurrent `a2h migrate -c
<cfg>` is writing — it takes no lock and never mutates the ledger. It derives the
manifest path + `run_id` from the config exactly like `status`/`resume`, or pass
`--manifest` and `--run-id` together to point at one explicitly.

**Shows.** A per-table grid (source → target, live status, chunks loaded/total,
rows loaded, a progress bar, and **volume left**) plus a totals panel (tables
done, overall %, aggregate volume left, elapsed, and an ETA). In-flight tables
are highlighted and any failed chunks are flagged in red.

**Options.** `--interval` — refresh seconds (default `1.0`). `--once` — render a
single frame and exit; this is the non-TTY / CI fallback and is also selected
automatically when stdout is not a terminal.

**Exit codes.** `0` once every table is loaded/verified; `1` while the run is
still incomplete (so `--once` is pollable from CI); `1` (`error: no manifest …`)
if no run exists for this config yet.

---

## Validation

All three validators introspect the source, query the target, print a PASS/FAIL
line via the shared formatter, and **exit non-zero on any failure**, so they gate
CI directly. A result *passes* iff it carries no `BLOCKER` findings; `DEGRADED` /
`COSMETIC` findings are reported but not fatal. Identifiers are lowercased on the
target side unless `[options].preserve_case` is set.

### `a2h test`

```bash
a2h test [-c config.toml]
```

**Purpose.** TEST — object-inventory / structural diff. For each source table,
confirm the target has it **and** carries the same number of columns. Existence +
column count are read catalog-free (`SELECT * … LIMIT 0` + the result
description), so it works on any PG-wire edition regardless of
`information_schema` support. A missing table or a column-count mismatch is a
`BLOCKER`.

**Prints.** `TEST: PASS|FAIL`, any findings, and metrics
(`tables_checked`, `tables_ok`).

```
$ a2h test -c config.toml
TEST: PASS
  metrics: {'tables_checked': 2, 'tables_ok': 2}
```

**Exit codes.** `0` if passed, `1` on any BLOCKER finding (or a tool error).

### `a2h test-count`

```bash
a2h test-count [-c config.toml]
```

**Purpose.** TEST_COUNT — row-count parity. Compare each table's exact source row
count to `SELECT count(*)` on the target. Any disagreement is a `BLOCKER`
recording the source/target/delta.

**Prints.** `TEST_COUNT: PASS|FAIL`, findings, and metrics
(`tables_checked`, `tables_matched`, `tables_mismatched`).

```
$ a2h test-count -c config.toml
TEST_COUNT: PASS
  metrics: {'tables_checked': 2, 'tables_matched': 2, 'tables_mismatched': 0}
```

**Exit codes.** `0` if all counts match, `1` otherwise.

> For a **migrate-back** (HeliosDB-as-source) run, the source catalog exposes no
> PK/FK metadata, so `test-count` (row parity) is the structural gate — see
> `test-data` below and the [examples](../guides/examples.md#scenario-3).

### `a2h test-data`

```bash
a2h test-data [-c config.toml] [--sample N]
```

**Purpose.** TEST_DATA — content-level parity. For each table, sample rows
ordered by primary key from both sides and compare them by per-row SHA-256.
Rendering is canonicalized so equal values hash equal across drivers: binary is
normalized to hex (an Oracle `bytes` BLOB and a psycopg `bytea` agree), numerics
to a trailing-zero-free decimal string (`125000.50` ≡ `125000.5`), and booleans
to `1`/`0` (MySQL `TINYINT(1)` ≡ HeliosDB `BOOLEAN`).

| Option | Default | Purpose |
|---|---|---|
| `--sample N` | `1000` | Rows per table to compare. **`0` (or any ≤ 0) = all rows.** |

A table with **no primary key** can't be aligned row-for-row, so instead of
skipping it (which could mask corruption) it compares the **sorted multiset** of
per-row checksums — a real, order-independent parity check. It stops after 10 row
mismatches per table (`stopped_early` in metrics) so a badly diverged table fails
fast.

**Prints.** One `TEST_DATA: PASS|FAIL` block **per table**, each with metrics
(`rows_compared`, `mismatches`; `no_pk_multiset`/`stopped_early` when relevant).

```
$ a2h test-data -c config.toml
TEST_DATA: PASS
  metrics: {'rows_compared': 3, 'mismatches': 0}
TEST_DATA: PASS
  metrics: {'rows_compared': 5, 'mismatches': 0}
```

**Exit codes.** `0` if every table passed; `1` if any table had a BLOCKER. Use
`--sample 0` for a full, exact comparison (slower on large tables).

### `a2h test-index`

```bash
a2h test-index [-c config.toml]
```

**Purpose.** TEST_INDEX — a target-side index-correctness check that `test-data`
cannot do. For each foreign-key column it compares an **index-eligible** count
(`WHERE col = (SELECT min(col) …)`) against an **index-defeated** full-scan count
(`WHERE col::text = (…)::text`). They must agree; a mismatch means the target
served the equality lookup from a stale or empty index — e.g. an FK index
auto-created by `ADD FOREIGN KEY` but never backfilled from the rows loaded before
the FK was added (a2h loads data first, adds FKs after). Because `test-data` never
filters or joins on an FK column, only this check catches that class of silent,
target-side regression. Target-only (the source supplies the FK metadata; the
probe runs on the target) and type-agnostic via the `::text` defeat.

**Prints.** One `TEST_INDEX: PASS|FAIL` block per table, with metrics
(`fk_columns_checked`, `mismatches`). A table with no FKs or no non-null FK values
reports `fk_columns_checked: 0` (nothing to probe).

```
$ a2h test-index -c config.toml
TEST_INDEX: PASS
  metrics: {'fk_columns_checked': 2, 'mismatches': 0}
```

**Exit codes.** `0` if every FK-column probe agreed; `1` if any table had a
mismatch (BLOCKER), so it gates CI alongside the other `test-*` checks.

---

## Change data capture

A GoldenGate-style **Extract → trail → Replicat** pipeline where capture and
apply advance on independent, durable cursors. v1 capture is Oracle
**SCN-watermark**; a log-based **MySQL binlog** source is also implemented. See
[docs/cdc.md](../cdc.md) for the full model and
[examples scenario 5](../guides/examples.md#scenario-5) for runnable cycles.

> **Edition support.** Apply (`replicat`) is **validated on HeliosDB-Full and
> HeliosDB-Lite**, and on **HeliosDB-Nano ≥ 3.58.5** — against an older Nano it is
> **refused** at runtime with a clear error (not a cryptic mid-apply failure). The
> resumable [`migrate` + `resume`](#a2h-resume) path is also available for
> idempotent refreshes against any target. See
> [HeliosDB compatibility](../heliosdb-compatibility.md).

### `a2h extract NAME`

```bash
a2h extract NAME [-c config.toml] [--refresh-tables]
a2h extract NAME --drop [--purge-trail]     # tear down
a2h extract NAME --purge-applied            # reclaim applied trail segments
```

**Purpose.** Capture source changes into the trail named `NAME`, then advance the
capture cursor. `NAME` (a required positional argument) is the extract / capture
process name — yours to choose; the first run **registers** it (capturing every
table in the configured schema). The registered table set is then **pinned**.

| Option | Purpose |
|---|---|
| `--refresh-tables` | Adopt tables that appeared in the source since registration and **snapshot-load** their current rows into the trail (as INSERT records) before their CDC events flow. Without it, a new source table is reported (a warning each cycle) but not captured. PK-less new tables are reported and skipped. |
| `--drop` | Tear the extract down: drop the PostgreSQL logical slot (so it stops pinning WAL) and remove the registry entry. Keeps the trail unless `--purge-trail`. |
| `--purge-trail` | With `--drop`, also delete the trail directory (a clean slate). |
| `--purge-applied` | Delete fully-applied **closed** trail segments (never the active one, never past the apply cursor) to reclaim disk. Manual — there is no automatic purge. |

The tier-2 tunables that shape capture live in the [`[cdc]` config section](../guides/configuration.md#cdc-change-data-capture-tuning)
(`capture_batch` caps events per cycle; `trail_rotate_mb` bounds segment size).
See [Operational hardening](../cdc.md#operational-hardening-tier-2) for the full
workflow. Over MCP, the `extract` tool takes `refresh_tables` / `drop` /
`purge_trail` / `purge_applied` booleans.

**Behavior by source dialect.**
- **Oracle (SCN-watermark).** First cycle (watermark 0) is a **full snapshot**;
  later cycles capture only rows where `ORA_ROWSCN > watermark`. The new watermark
  is anchored *before* the scan. Tables with no primary key are **skipped** (and
  listed). If no flashback/SCN function is permitted, it falls back to a full
  re-capture each cycle (still correct — apply is idempotent).
- **MySQL (binlog).** Log-based ROW-event capture (real I/U/D incl. **deletes**).
  The cursor is the binlog coordinate `"<file>:<pos>"` persisted in
  `trail/<name>/binlog.pos`; the first run anchors at the server's current
  position and captures nothing. Needs the [`mysql-cdc` extra](#a2h-doctor) and
  server prerequisites (below).

**Prints.**

```
# Oracle, first cycle
$ a2h extract cdc1 -c config.toml
extract cdc1: captured 8 change(s) (full snapshot); watermark=2547881

# Oracle, later cycle
extract cdc1: captured 2 change(s) (incremental since SCN 2547881); watermark=2547990

# MySQL binlog, first cycle (anchor only)
extract b1: captured 0 change(s) (binlog since (current)); watermark=mysql-bin.000003:1421
```

Skipped (PK-less) tables are listed as `skipped <table> (no primary key)`.

**Prerequisites.** A config + reachable source. For MySQL binlog:
`log_bin=ON`, `binlog_format=ROW`, `binlog_row_metadata=FULL` (the extract sets
it best-effort when anchoring; otherwise set it server-side), and a user with
`REPLICATION SLAVE` / `REPLICATION CLIENT`.

**Exit codes.** `0` on success; `1` on a source/config error.

### `a2h replicat NAME`

```bash
a2h replicat NAME [-c config.toml] [--reconcile-deletes | --no-deletes]
```

**Purpose.** Apply the captured changes from `NAME`'s trail to the target,
**idempotently**, then advance the apply cursor independently of the capture
watermark. Records are bucketed by table and routed through the driver's
keyed upsert (`INSERT … ON CONFLICT DO UPDATE` on psycopg;
`ON DUPLICATE KEY UPDATE` on the MySQL target) and delete-by-key. PK-less tables
are skipped with a warning.

| Option | Default | Purpose |
|---|---|---|
| `--reconcile-deletes` / `--no-deletes` | **mode-aware**: on for the Oracle SCN source, off for the log-based sources (MySQL binlog, PostgreSQL logical) | Reconcile deletes via a source/target key-set diff: target rows whose PK is absent from the source's current key set are removed (a full O(keys) pass). The Oracle SCN source emits no delete events, so reconciliation defaults ON there; the log-based sources already carry explicit `D` records, so it defaults OFF (reconciling against the *live* source key set can also race a not-yet-applied PK-changing UPDATE and delete its old-key row ahead of the move). Passing either flag explicitly overrides the default for any source. |

**Prints.** `replicat NAME: applied <n> change(s), deleted <d>, from <r> read;
cursor=<c>`, plus warnings (e.g. PK-less tables skipped). If any record was parked
by the [poison policy](../cdc.md#poison-record-policy-poison_retries--poison_max_per_run) it also
prints `dead-lettered <n> poison record(s)`.

```
$ a2h replicat cdc1 -c config.toml
replicat cdc1: applied 2 change(s), deleted 0, from 2 read; cursor=10
```

The apply reads and applies the trail in memory-bounded chunks
([`apply_batch`](../guides/configuration.md#cdc-change-data-capture-tuning)); the
keymove barrier composes, so the final state and cursor match an unbounded run. A
record the target rejects `poison_retries` times is moved to `dead_letter.jsonl`
beside the trail and the cursor advances past it (key-moves are never
dead-lettered — they fail closed).

**Prerequisites.** A registered extract (`a2h extract NAME` first, else
`error: no such extract '<name>' …`), a reachable source (it supplies the
apply-side schema and, for reconciliation, the live key set) and target.

**Exit codes.** `0` on success. `1` on error — including the explicit refusal the
tool raises when the live probe detects a Nano target below the CDC-apply minimum
(3.58.5), so you get a clear message instead of a cryptic mid-apply failure.

### `a2h extracts`

```bash
a2h extracts [-c config.toml] [--lag]
```

**Purpose.** List registered CDC extracts and their positions, read from the CDC
registry (`<output_dir>/cdc.db`). Connects to nothing **unless** `--lag` is given.

| Option | Purpose |
|---|---|
| `--lag` | Also compute each extract's replication lag by querying the source: PostgreSQL slot `confirmed_flush_lsn` vs `pg_current_wal_lsn()` (bytes of WAL still pinned), MySQL trailed binlog coordinate vs the server head, Oracle watermark SCN vs current SCN. Best-effort — an unreachable source prints `lag: unavailable`. |

**Prints.** One line per extract — name, schema, table count, capture watermark,
apply cursor, state (`registered` → `capturing` → `applying`), and (when any are
parked) `dead_letters=N`; with `--lag`, a second line per extract:

```
$ a2h extracts -c config.toml --lag
  cdc1             schema=HR tables=2 watermark=2547990 cursor=10 state=applying
      lag: mode=scn watermark_scn=2547990 current_scn=2548400 scn_behind=410
```

Prints `no extracts registered` when the registry is empty.

**Exit codes.** `0`.

---

## MCP server (AI-agent remote administration)

The `mcp` command group runs an MCP (Model Context Protocol) server that exposes
the a2h engine as MCP **tools**, so an AI agent can assess / migrate / validate /
resume / run CDC remotely with Bearer-token auth + RBAC. Full protocol, tool
catalog, and RBAC matrix: [docs/mcp.md](../mcp.md).

### `a2h mcp serve`

```bash
a2h mcp serve [--transport http|stdio] [--host H] [--port P] \
              [--tokens TOK] [--tokens-file FILE] [--stdio-role ROLE]
```

**Purpose.** Start the MCP server. `http` (default) serves JSON-RPC 2.0 at
`POST /mcp` (streamable HTTP; `GET /mcp` opens the SSE channel) with an
unauthenticated liveness probe at `GET /healthz`; `stdio` is a trusted local
launch for an agent that spawns the server as a subprocess.

| Option | Default | Purpose |
|---|---|---|
| `--transport` | `http` | `http` (remote, Bearer-auth) or `stdio` (local, newline-delimited JSON-RPC on stdin/stdout). |
| `--host` | `127.0.0.1` | Bind address (HTTP transport). Use `0.0.0.0` to accept remote agents. |
| `--port` | `8080` | Bind port (HTTP transport). |
| `--tokens` | — | `token:role[,token:role…]` inline (else `$A2H_MCP_TOKENS`). Roles: `viewer` \| `operator` \| `admin`. |
| `--tokens-file` | — | Path to a `token:role`-per-line file (else `$A2H_MCP_TOKENS_FILE`). The file wins over the env var on a clash. |
| `--stdio-role` | `admin` | Role granted to the local stdio caller (stdio has no Bearer check). |

**Notes.** The HTTP transport **refuses to start with zero tokens** (it would be
an open relay); generate one with `a2h mcp auth`. The server is a built-in,
wire-compatible JSON-RPC endpoint on every Python version (stdlib only — no
extra dependency to run it); whether the official `mcp` SDK is importable is
reported informationally in the banner/`sdk_path`, but the serving path does
not use it.

**Prints.** A one-line banner (`… (http) on http://H:P/mcp`, or on stderr for
stdio so stdout stays the JSON-RPC channel), then serves until interrupted.

**Exit codes.** Runs until `Ctrl-C` (`stopped`); `1` on a startup error (e.g. no
tokens for HTTP).

### `a2h mcp auth`

```bash
a2h mcp auth [--role viewer|operator|admin] [--file FILE] [--rotate] [--show]
```

**Purpose.** Generate a cryptographically-strong Bearer token and store it in a
private (`0600`) token file — the same file `mcp serve --tokens-file` reads, so
the secret stays off the command line and out of the project config.

| Option | Default | Purpose |
|---|---|---|
| `-r`, `--role` | `admin` | Role the token grants: `viewer` \| `operator` \| `admin`. An unknown role errors. |
| `-f`, `--file` | `$A2H_MCP_TOKENS_FILE` else `~/.config/a2h/mcp-tokens` | Token file to write. |
| `--rotate` | off | Replace the file's contents instead of appending a new token. |
| `--show` | off | Also print the raw token (otherwise it stays only in the file). |

**Prints.** Confirmation of the role/path/mode, the token **fingerprint** (never
the raw token unless `--show`), and the `mcp serve` + client `Authorization:
Bearer` lines to copy. Clients read the token as the first `:`-field of a line.

**Exit codes.** `0` on success; `1` on an unknown role.

---

## Exit codes (summary)

| Code | Meaning |
|---|---|
| `0` | Success / validation passed / all chunks loaded. |
| `1` | A tool error (missing or malformed config, connection/introspection failure), **or** a validation mismatch (`test` / `test-count` / `test-data`), **or** a partial data load (`migrate` reported failed chunks), **or** an edition refusal (`replicat` on Nano). |

---

## See also

- [Worked examples](../guides/examples.md) — copy-pasteable end-to-end scenarios.
- [Configuration](../guides/configuration.md) — every `config.toml` field.
- [Getting started](../guides/getting-started.md) — install → first migration.
- [CDC](../cdc.md) — the Extract → trail → Replicat model in depth.
- [CDC operations runbook](../guides/cdc-operations.md) — scheduling, lag, trail retention, dead-letter triage, recovery.
- [MCP server](../mcp.md) — the tool catalog, auth, and RBAC for the `mcp` group.
- [Type mapping](type-mapping.md) — the source→HeliosDB type tables + overrides.
- [docs/README.md](../README.md) — the documentation index.
