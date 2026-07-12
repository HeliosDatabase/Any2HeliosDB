# Configuration (`config.toml`)

`config.toml` is the modern replacement for `ora2pg.conf`. The
[`wizard`](../reference/cli.md#a2h-wizard) writes it; you can also hand-edit it.
This page documents every field, derived from
[`config/model.py`](../../src/any2heliosdb/config/model.py) and
[`config/store.py`](../../src/any2heliosdb/config/store.py).

The file has up to seven tables: `[source]`, `[target]`, `[options]`, `[cdc]`,
and the optional `[data_type]`, `[modify_type]`, and `[capability]`.

## `[source]`

| Field | Type | Default | Notes |
|---|---|---|---|
| `dialect` | string | `oracle` | `oracle` \| `mysql` \| `postgresql` \| `mssql` — all four validated end-to-end (see the [README compatibility matrix](../../README.md#compatibility-matrix)). `postgresql` reads a PostgreSQL/HeliosDB server *out* (the migrate-back / PG-wire source); the wizard menu offers oracle/mysql/mssql, so hand-write `postgresql`. |
| `host` | string | `127.0.0.1` | Source host. |
| `port` | int | `1521` | Source port (Oracle 1521, MySQL 3306, PostgreSQL 5432, MSSQL 1433). |
| `service_name` | string | — | **Oracle**: connect by service name (e.g. `XEPDB1`). |
| `sid` | string | — | **Oracle**: connect by SID (alternative to `service_name`). |
| `database` | string | — | **MySQL / PostgreSQL / MSSQL**: database name (used instead of `service_name`/`sid`). |
| `user` | string | `""` | Login user. Needs read on the `ALL_*` views + `SELECT` on the schema's tables. |
| `password_env` | string | — | **Recommended.** Name of the env var holding the password (resolved at runtime). |
| `password` | string | — | Literal password — dev convenience only; keep empty in committed configs. |
| `schema` | string | — | Schema to migrate. Blank = the connecting user's own schema (upper-cased internally). |
| `thick` | bool | `false` | **Oracle**: use python-oracledb **thick mode** (Oracle Instant Client). Default is **thin mode** — pure-Python, no client, no external deps. Enable only for servers that mandate **Native Network Encryption** (thin mode can't do NNE → `DPY-3001`). |
| `client_dir` | string | — | **Oracle** (thick): Instant Client lib directory. Omit to find it via `PATH` / `LD_LIBRARY_PATH`. |
| `sysdba` | bool | `false` | **Oracle**: connect with SYSDBA privilege (required for the `SYS` user). |
| `connect_timeout` | int | `10` | Seconds to wait to **establish** the source connection before failing — bounds a firewalled/unreachable source so `assess`/`migrate` fail fast instead of hanging forever. Threaded into each driver's own parameter: oracledb `tcp_connect_timeout`, pymysql `connect_timeout`, psycopg `connect_timeout`, pyodbc `timeout` (= `SQL_ATTR_LOGIN_TIMEOUT`). |

For Oracle, set exactly one of `service_name` or `sid`. The DSN built is
`host:port/service_name`, or `makedsn(host, port, sid=…)`, or bare `host:port`.

By default a2h connects in **thin mode** (pure-Python, no Oracle client — the
lightweight, dependency-free path). Set `thick = true` **only** if the server
mandates Native Network Encryption / Data Integrity, which thin mode cannot do;
that path needs the Oracle Instant Client installed (point `client_dir` at it, or
put it on `LD_LIBRARY_PATH`). The env vars `A2H_ORACLE_THICK=1` and
`ORACLE_CLIENT_DIR=…` are honored as an alternative to the config keys.

## `[target]`

| Field | Type | Default | Notes |
|---|---|---|---|
| `driver` | string | `psycopg` | `psycopg` (PG-wire, every edition, default), `mysql` (MySQL-wire migrate-back sink), or `native` (Oracle-wire, experimental) — see [Driver selection](#driver-selection). |
| `host` | string | `127.0.0.1` | HeliosDB host. |
| `port` | int | `5432` | HeliosDB PG-wire port. |
| `dbname` | string | `postgres` | Target database name. |
| `user` | string | `postgres` | Target user. |
| `password_env` | string | — | Env var with the target password; omit for a **trust** target. |
| `password` | string | — | Literal password (dev only). |
| `sslmode` | string | — | e.g. `require`. Omitted when unset. |
| `connect_timeout` | int | `10` | Seconds to wait to **establish** the target connection before failing (psycopg `connect_timeout`; the native Oracle-wire driver's `tcp_connect_timeout`). |

## `[options]`

| Field | Type | Default | Notes |
|---|---|---|---|
| `output_dir` | string | `./migration_output` | Holds the resume manifest (`manifest.db`, or `manifest.nano/` — see `manifest_backend`), the CDC registry `cdc.db`, and the per-extract `trail/`. |
| `batch_size` | int | `1000` | Source fetch `arraysize`/`prefetchrows` and the INSERT-fallback batch size. Honored on the default resumable load **and** on `resume`. |
| `parallelism` | int | `4` | Number of parallel load workers; the loader aims for ~`parallelism × chunks_per_worker` chunks per table. |
| `chunks_per_worker` | int | `2` | Target PK-range chunks **per worker**: the loader splits each table into ~`parallelism × chunks_per_worker` chunks. More chunks smooth per-worker skew (one big chunk finishing last) at the cost of more chunk bookkeeping. **Plan-affecting**: it joins the loader's config hash, so changing it resets the run rather than replaying a stale chunk plan (contrast `batch_size`, which does not). |
| `prefer_copy` | bool | `true` | Use COPY when the target's probe reports `copy_from_stdin`; otherwise INSERT. |
| `preserve_case` | bool | `false` | `false` lowercases all identifiers (Ora2Pg `PRESERVE_CASE` off) so they stay unquoted; `true` keeps source case (quoted). |
| `drop_existing` | bool | `true` | `DROP TABLE … CASCADE` before re-creating on `migrate` (ignored by `resume`). |
| `manifest_backend` | string | `sqlite` | Resumable-load ledger store: `sqlite` (stdlib, zero-friction default) or `nano` (embedded HeliosDB-Nano, in-process). See below. |
| `native_call_timeout_ms` | int | `300000` | **Native (Oracle-wire) target only.** Per-round-trip `call_timeout` (milliseconds) on the oracledb connection — a generous safety net so a bulk array-INSERT never blocks forever on a stalled HeliosDB TTC response. Unused by the psycopg/PG-wire path. (The short close-handshake wait stays a fixed internal bound: by close time the data is already committed, so there is nothing to tune.) |

### `manifest_backend` — SQLite vs embedded Nano

The crash-safe resume ledger (runs / tables / chunks / watermarks) defaults to a
stdlib **SQLite** file at `<output_dir>/manifest.db` — no extra dependency, works
everywhere. Setting `manifest_backend = "nano"` runs the same ledger on an
**embedded HeliosDB-Nano** (RocksDB) store at `<output_dir>/manifest.nano` (a
directory), which dogfoods the engine on a2h's own state. Install the backend with
the extra:

```bash
pip install 'any2heliosdb[nano-manifest]'   # adds heliosdb-nano-embedded
# NOTE: heliosdb-nano-embedded is not yet on PyPI — this extra needs the wheel
# installed out-of-band / from a private index first. It is deliberately NOT
# part of [all], so [all] installs cleanly from PyPI.
```

Both backends are functionally identical — same resume, status, and live monitor.
The backend is auto-detected on read (file ⇒ sqlite, directory ⇒ nano), so `a2h
status` / `a2h monitor` / `a2h resume` need no extra flag. `migrate`, `status`,
and `monitor` can run concurrently on the Nano backend: the loader holds the single
RocksDB writer while read-only commands open their own read-only view. Keep
`sqlite` unless you specifically want the embedded engine.

## `[cdc]` (change-data-capture tuning)

Tunables for the CDC spine (`a2h extract` / `replicat`). Every one bounds a
resource the pipeline would otherwise let grow without limit — the tier-2
hardening that followed a host OOM. All are optional; omit the whole section to
accept the defaults.

| Field | Type | Default | Notes |
|---|---|---|---|
| `capture_batch` | int | `50000` | Max change events one `extract` cycle pulls from a **log-based** source (PostgreSQL logical peek `upto_nchanges` / MySQL binlog event stop). The server-side cursor (slot LSN / binlog pos) only advances past what was captured, so anything beyond the cap is picked up next cycle. `0` = no cap (the pre-tier-2 unbounded behaviour). Does not apply to the Oracle SCN-watermark scan. **PostgreSQL caveat:** the cap is checked at transaction boundaries, so it bounds the backlog of *committed transactions* — one huge transaction is still materialized whole. |
| `apply_batch` | int | `10000` | Max trail **lines** the `replicat` reads and applies per bounded chunk, advancing the apply cursor per chunk. The keymove barrier composes — each keymove is still flushed alone within its chunk. Also the surplus-key delete batch size for delete reconciliation, and the buffer size for `--refresh-tables` snapshot appends. `0` = read the whole slice at once (pre-tier-2 behaviour). |
| `poison_retries` | int | `3` | How many times the replicat retries a single failing **non-keymove** record before moving it to `dead_letter.jsonl` (beside the trail) and advancing past it, so one bad record can't wedge replication forever. Before parking a record the replicat `ping()`s the target and re-raises (cursor unmoved) if it is unreachable — a transient outage never dead-letters the backlog. `0` disables the policy — a failing record raises, as before. **Keymoves are never dead-lettered** (skipping one diverges key state); a keymove failure always fails closed. |
| `poison_max_per_run` | int | `25` | Mass-poison circuit breaker: if one `replicat` run would dead-letter more than this many records it raises instead (cursor unmoved for the offending chunk). A flood of poison usually means an environment fault, not bad data. `0` disables the breaker. |
| `trail_rotate_mb` | int | `256` | Rotate the active trail segment once it reaches this many MB; closed segments become `trail.NNNNN.jsonl`. The apply cursor stays a single **global line index** across segments, so a legacy single-file trail and its integer cursor keep working unchanged. `0` disables rotation (one `trail.jsonl`). Reclaim applied segments with `a2h extract NAME --purge-applied`. |

```toml
[cdc]
capture_batch = 50000
apply_batch = 10000
poison_retries = 3
poison_max_per_run = 25
trail_rotate_mb = 256
```

See [Change data capture](../cdc.md) for the operational workflow behind each
knob (bounded capture/apply, the poison dead-letter file, trail rotation +
`--purge-applied`, new-table adoption with `--refresh-tables`, slot teardown
with `--drop`, and lag reporting with `a2h extracts --lag`).

## Type overrides: `[data_type]` & `[modify_type]`

Ora2Pg's two override knobs, applied on top of the
[default type map](../reference/type-mapping.md):

```toml
[data_type]
# Global: source type NAME -> target type. Matches the base name, so this
# remaps every NUMBER (incl. NUMBER(10,2)) to bigint.
NUMBER = "bigint"

[modify_type]
# Per-column: schema.table.column -> target type. Highest precedence.
"hr.emp.salary" = "numeric(12,2)"
"hr.emp.bonus"  = "numeric(10,2)"
```

Resolution precedence is **`MODIFY_TYPE` → `DATA_TYPE` → default mapping**.
`MODIFY_TYPE` keys are matched case-insensitively as `schema.table.column` (or
`table.column` if you omit the schema). Run `a2h assess` to see, per column,
whether the resolved type came from a default or one of your overrides
(`provenance`).

The target-type string you supply is parsed by `parse_target_type` — it
understands `VARCHAR(n)`, `CHAR(n)`, `NUMERIC(p,s)`, `DECIMAL(p,s)`, and the base
types (`INTEGER`, `BIGINT`, `TEXT`, `BYTEA`, `TIMESTAMP`, `TIMESTAMPTZ`,
`BOOLEAN`, `JSON`, `JSONB`, `UUID`, `INTERVAL`, `SERIAL`, `BIGSERIAL`, …). Anything
else is passed through verbatim.

## `[capability]` (informational)

The wizard caches a subset of the smoke-test probe here (e.g. `target_edition`,
`copy_from_stdin`, `enforces_check`, `enforces_fk`). It is **informational** — the
real probe runs fresh at the start of every `migrate`, so a stale `[capability]`
block never drives behavior. You can delete it safely.

## Password handling (env vars)

`password_env` names an environment variable resolved at runtime:

```toml
[source]
password_env = "ORACLE_PW"

[target]
password_env = "HELIOS_PW"
```

```bash
export ORACLE_PW='hr'
export HELIOS_PW='heliosdb'
a2h migrate -c config.toml
```

If `password_env` is set but the variable is unset, the resolved password is
empty (you'll get an auth failure). A literal `password` is only consulted when
`password_env` is absent — keep it out of anything committed.

## Driver selection

```toml
[target]
driver = "psycopg"     # default, validated, every edition
# driver = "mysql"     # validated, MySQL-wire migrate-back / heterogeneous sink
# driver = "native"    # experimental, Oracle-wire, Lite/Full only
```

| | `psycopg` | `mysql` | `native` |
|---|---|---|---|
| Wire protocol | PostgreSQL | MySQL (`PyMySQL`) | Oracle TNS (`oracledb`) |
| Target | Nano / Lite / Full / stock PostgreSQL | a MySQL 8 server | Lite / Full (Oracle-wire) |
| Bulk load | COPY (fast path) or INSERT | `INSERT … ON DUPLICATE KEY UPDATE` — no COPY | array INSERT (`executemany`) — no COPY |
| Identifiers | lowercased (unless `preserve_case`) | backtick-quoted | source (upper) case, quoted |
| Dialect translation | the **tool** translates the source dialect → PG | the **tool** translates → MySQL | **HeliosDB** translates (tool sends near-passthrough Oracle SQL) |
| Status | **validated** | **validated** — heterogeneous / migrate-back (data flows *out* of HeliosDB, the GoldenGate-reverse direction) | **experimental** — live parity test blocked on a HeliosDB Oracle-listener TNS-version handshake |

The `mysql` driver is the **migrate-back / heterogeneous** sink: it writes to a
MySQL 8 server over the MySQL wire, so data can flow back *out* of HeliosDB (or
straight Oracle → MySQL). It is validated as the sink for Oracle→MySQL and
HeliosDB→MySQL (see [Worked examples, scenario 3](examples.md#scenario-3)).

The `native` driver is the purest expression of the
[design principle](../../README.md#design-principle-a-thin-translation-layer-over-a-runtime-probe)
— it transforms almost nothing and lets HeliosDB's in-database Oracle
compatibility do the work. It is code-complete and unit-tested; use `psycopg` for
production until the TNS handshake fix ships. Selecting `native` with a non-Oracle
source raises a config error.

## Tuning

- **`parallelism`** — raise for wide PK ranges and a target that handles
  concurrent transactions (Full); the loader makes ~`parallelism × chunks_per_worker`
  chunks per table. On **Lite**, concurrent transactions are rejected today, so
  chunks may fail under contention and are then re-run by the automatic
  **serial-retry** pass — the load still converges. There is no benefit to a high
  `parallelism` on Lite; `1` avoids the wasted parallel attempt.
- **`chunks_per_worker`** — chunks per worker (default `2`); more chunks smooth
  per-worker skew but add bookkeeping. Because it sets the chunk **count**, it is
  plan-affecting — changing it across a `resume` resets the run (re-plans from
  scratch) rather than replaying the old plan.
- **`prefer_copy`** — leave `true`. The probe disables COPY automatically where the
  target lacks it (Nano), falling back to INSERT. Set `false` only to force INSERT
  for debugging.
- **`preserve_case`** — leave `false` unless you specifically need case-sensitive,
  quoted identifiers on the target. With `false`, `EMPLOYEES` becomes `employees`
  and needs no quoting.
- **`batch_size`** — the source-side fetch `arraysize` and the INSERT batch. The
  default of 1000 is a sensible balance; raise it for very wide tables to cut round
  trips.
- **`output_dir`** — keep it on durable local storage; it holds the crash-safe
  manifest (`manifest.db`) and the durable CDC trail.

## A complete example

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
output_dir = "/var/lib/a2h/hr"
batch_size = 2000
parallelism = 8
prefer_copy = true
preserve_case = false
drop_existing = true

[modify_type]
"hr.employees.salary" = "numeric(12,2)"
```
