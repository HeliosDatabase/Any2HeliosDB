# Configuration (`config.toml`)

`config.toml` is the modern replacement for `ora2pg.conf`. The
[`wizard`](../reference/cli.md#a2h-wizard) writes it; you can also hand-edit it.
This page documents every field, derived from
[`config/model.py`](../../src/any2heliosdb/config/model.py) and
[`config/store.py`](../../src/any2heliosdb/config/store.py).

The file has up to six tables: `[source]`, `[target]`, `[options]`, and the
optional `[data_type]`, `[modify_type]`, and `[capability]`.

## `[source]`

| Field | Type | Default | Notes |
|---|---|---|---|
| `dialect` | string | `oracle` | `oracle` \| `mysql` \| `mssql`. Only `oracle` is validated; MySQL/MSSQL are [scaffolded](../migration/mysql-and-mssql.md). |
| `host` | string | `127.0.0.1` | Source host. |
| `port` | int | `1521` | Source port (Oracle 1521, MySQL 3306, MSSQL 1433). |
| `service_name` | string | — | **Oracle**: connect by service name (e.g. `XEPDB1`). |
| `sid` | string | — | **Oracle**: connect by SID (alternative to `service_name`). |
| `database` | string | — | **MySQL/MSSQL**: database name (used instead of `service_name`/`sid`). |
| `user` | string | `""` | Login user. Needs read on the `ALL_*` views + `SELECT` on the schema's tables. |
| `password_env` | string | — | **Recommended.** Name of the env var holding the password (resolved at runtime). |
| `password` | string | — | Literal password — dev convenience only; keep empty in committed configs. |
| `schema` | string | — | Schema to migrate. Blank = the connecting user's own schema (upper-cased internally). |
| `thick` | bool | `false` | **Oracle**: use python-oracledb **thick mode** (Oracle Instant Client). Default is **thin mode** — pure-Python, no client, no external deps. Enable only for servers that mandate **Native Network Encryption** (thin mode can't do NNE → `DPY-3001`). |
| `client_dir` | string | — | **Oracle** (thick): Instant Client lib directory. Omit to find it via `PATH` / `LD_LIBRARY_PATH`. |
| `sysdba` | bool | `false` | **Oracle**: connect with SYSDBA privilege (required for the `SYS` user). |

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
| `driver` | string | `psycopg` | `psycopg` (PG-wire, every edition, default) or `native` (Oracle-wire, experimental — see below). |
| `host` | string | `127.0.0.1` | HeliosDB host. |
| `port` | int | `5432` | HeliosDB PG-wire port. |
| `dbname` | string | `postgres` | Target database name. |
| `user` | string | `postgres` | Target user. |
| `password_env` | string | — | Env var with the target password; omit for a **trust** target. |
| `password` | string | — | Literal password (dev only). |
| `sslmode` | string | — | e.g. `require`. Omitted when unset. |

> A connection timeout of 10s applies to the target by default
> (`TargetDsn.connect_timeout`).

## `[options]`

| Field | Type | Default | Notes |
|---|---|---|---|
| `output_dir` | string | `./migration_output` | Holds the resume `manifest.db`, the CDC registry `cdc.db`, and the per-extract `trail/`. |
| `batch_size` | int | `1000` | Source fetch `arraysize`/`prefetchrows` and the INSERT-fallback batch size. |
| `parallelism` | int | `4` | Number of parallel load workers; the loader aims for ~`parallelism × 2` chunks per table. |
| `prefer_copy` | bool | `true` | Use COPY when the target's probe reports `copy_from_stdin`; otherwise INSERT. |
| `preserve_case` | bool | `false` | `false` lowercases all identifiers (Ora2Pg `PRESERVE_CASE` off) so they stay unquoted; `true` keeps source case (quoted). |
| `drop_existing` | bool | `true` | `DROP TABLE … CASCADE` before re-creating on `migrate` (ignored by `resume`). |

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
# driver = "native"    # experimental, Oracle-wire, Lite/Full only
```

| | `psycopg` | `native` |
|---|---|---|
| Wire protocol | PostgreSQL | Oracle TNS (`oracledb`) |
| Editions | Nano / Lite / Full | Lite / Full |
| Bulk load | COPY (fast path) or INSERT | array INSERT (`executemany`) — no COPY |
| Identifiers | lowercased (unless `preserve_case`) | source (upper) case, quoted |
| Dialect translation | the **tool** translates Oracle→PG | **HeliosDB** translates (tool sends near-passthrough Oracle SQL) |
| Status | **validated** | **experimental** — live parity test blocked on a HeliosDB Oracle-listener TNS-version handshake |

The `native` driver is the purest expression of the
[design principle](../../README.md#design-principle-a-thin-translation-layer-over-a-runtime-probe)
— it transforms almost nothing and lets HeliosDB's in-database Oracle
compatibility do the work. It is code-complete and unit-tested; use `psycopg` for
production until the TNS handshake fix ships. Selecting `native` with a non-Oracle
source raises a config error.

## Tuning

- **`parallelism`** — raise for wide PK ranges and a target that handles
  concurrent transactions (Full); the loader makes ~`parallelism × 2` chunks per
  table. On **Lite**, concurrent transactions are rejected today, so chunks may
  fail under contention and are then re-run by the automatic **serial-retry**
  pass — the load still converges. There is no benefit to a high `parallelism` on
  Lite; `1` avoids the wasted parallel attempt.
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
