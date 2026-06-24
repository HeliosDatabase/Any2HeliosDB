# Oracle → HeliosDB-Full

**Status: ✅ validated end-to-end**, including CDC, via the `psycopg` driver
against Oracle 21c XE on a current HeliosDB-Full build.

This is the most complete target: migrate with `TEST`/`TEST_COUNT`/`TEST_DATA`,
the wizard smoke test, resumable migrate + resume, **and CDC snapshot + incremental
+ apply** all pass. Full is a co-equal, first-class target alongside Lite.

## Prerequisites

- A running **HeliosDB-Full** (current `main` build) reachable over the PG-wire
  protocol. See [HeliosDB compatibility](../heliosdb-compatibility.md) for the
  capability probe and minimum builds.
- An Oracle source you can introspect and read.
- `pip install -e ".[oracle]"`; verify with `a2h doctor`.

Full's server banner is bare `HeliosDB` with no edition qualifier; the probe
classifies it as `full`.

## Driver choice

Use **`psycopg`** (the default, validated path). The **`native`** (Oracle-wire)
driver is available for Full but **experimental** — its live parity test is blocked
on a HeliosDB Oracle-listener TNS-version handshake (`oracledb` thin mode rejects
the handshake with DPY-3010). Use `psycopg` for production.

```toml
[target]
driver = "psycopg"
host = "127.0.0.1"
port = 5432            # your Full PG-wire port
dbname = "postgres"
user = "postgres"
# If a trust-mode connect is ever rejected, supply any password via password_env.
password_env = "HELIOS_PW"
```

## Step by step

```bash
export ORACLE_PW='hr'
export HELIOS_PW='heliosdb'

# 1. Configure + smoke-test.
a2h wizard
#   target_edition  full
#   copy_from_stdin True OK
#   null_empty_fidelity True OK

# 2. Inventory + type mapping (read-only).
a2h assess -c config.toml

# 3. Schema + data. Full supports COPY, so load_mode=copy.
a2h migrate -c config.toml
#   migrated 2 tables, 8 rows (load_mode=copy)

# 4. Validate.
a2h test       -c config.toml
a2h test-count -c config.toml
a2h test-data  -c config.toml      # per-row checksum; catches any target divergence

# 5. Resume if interrupted.
a2h status -c config.toml
a2h resume -c config.toml

# 6. CDC (validated on Full).
a2h extract  cdc1 -c config.toml
a2h replicat cdc1 -c config.toml
a2h extracts      -c config.toml
```

Full handles concurrent transactions, so raise `parallelism` for large tables to
exploit the parallel chunk loader.

## Type mapping

Standard Oracle → HeliosDB mapping (full table in the
[type-mapping reference](../reference/type-mapping.md)):

| Oracle | HeliosDB-Full |
|---|---|
| `NUMBER(p,s)` | `DECIMAL(p,s)` |
| `VARCHAR2(n)` | `VARCHAR(n)` |
| `CHAR(n)` | `CHAR(n)` |
| `DATE` | `TIMESTAMP` |
| `TIMESTAMP[TZ]` | `TIMESTAMP` / `TIMESTAMP WITH TIME ZONE` |
| `CLOB` | `TEXT` |
| `BLOB` / `RAW` | `BYTEA` |

## Edition-specific behaviors

A few Full behaviors are worth knowing for an Oracle migration. On a current
build, the full suite (migrate, validate, CDC) is green; see
[HeliosDB compatibility](../heliosdb-compatibility.md).

| Behavior | Effect |
|---|---|
| `'' → NULL` on COPY load | Full folds empty string to NULL (Oracle-compatible). Differs from stock PG and from Lite/Nano. Benign for Oracle migrations, since Oracle already folds `'' → NULL`. |
| Sequences | `CREATE SEQUENCE`/`nextval` may not be implemented on every build; a2h emits the standard DDL and degrades to a warning, and table data still migrates. Migrate to stock PostgreSQL if you need sequences created natively. |

`test-data` does a per-row checksum compare (binary normalized to hex), so any
target divergence surfaces immediately — confirm your build meets the
[minimum](../heliosdb-compatibility.md) if it flags one.

## CDC support

**Fully supported and validated on Full** — full snapshot, incremental capture,
UPDATE/INSERT propagation, and idempotent re-apply. Run the standard cycle:

```bash
a2h extract  cdc1 -c config.toml   # full snapshot first, then incremental
a2h replicat cdc1 -c config.toml   # idempotent INSERT ... ON CONFLICT DO UPDATE
a2h extracts      -c config.toml
```

See [docs/cdc.md](../cdc.md) for watermark/cursor semantics and v1 limits (no
deletes; SCN-watermark capture).

## See also

- [HeliosDB compatibility](../heliosdb-compatibility.md) — editions, minimum
  builds, and the capability probe.
- [CDC](../cdc.md) · [Troubleshooting](../troubleshooting.md).
