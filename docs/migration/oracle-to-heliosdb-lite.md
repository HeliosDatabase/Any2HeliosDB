# Oracle → HeliosDB-Lite

**Status: ✅ validated** (migrate + validation + resume, **and CDC apply** on a
current build) via the `psycopg` driver, against Oracle 21c XE.

Lite is a co-equal, first-class target alongside Full. The full a2h integration
suite — migrate with `TEST`/`TEST_COUNT`/`TEST_DATA`, the wizard smoke test, and
resumable migrate + resume — passes on a current Lite build.

## Prerequisites

- A running **HeliosDB-Lite** (2.0 or newer) reachable over the PG-wire protocol.
  See [HeliosDB compatibility](../heliosdb-compatibility.md) for the capability
  probe and minimum builds.
- An Oracle source you can introspect (`ALL_*` views) and read.
- `pip install -e ".[oracle]"`; verify with `a2h doctor`.

Lite's server banner looks like `PostgreSQL 17.0 (HeliosDB-Lite 2.0) on …`; the
edition probe classifies it as `lite`.

## Driver choice

Use **`psycopg`** (the default) — it is the validated path. The **`native`**
(Oracle-wire) driver is available for Lite but **experimental**: code-complete and
unit-tested, with its live parity test blocked on a HeliosDB Oracle-listener
TNS-version handshake. Don't use `native` for production Lite migrations yet.

```toml
[target]
driver = "psycopg"
host = "127.0.0.1"
port = 5432            # your Lite PG-wire port
dbname = "postgres"
user = "postgres"
password_env = "HELIOS_PW"
```

## Step by step

```bash
export ORACLE_PW='hr'
export HELIOS_PW='heliosdb'      # omit password_env for a trust Lite

# 1. Configure + smoke-test.
a2h wizard
#   target_edition  lite
#   copy_from_stdin True OK
#   null_empty_fidelity True OK

# 2. Inventory + type mapping (read-only).
a2h assess -c config.toml

# 3. Schema + data. Lite supports COPY, so load_mode=copy.
a2h migrate -c config.toml
#   migrated 2 tables, 8 rows (load_mode=copy)

# 4. Validate (each exits non-zero on mismatch).
a2h test       -c config.toml
a2h test-count -c config.toml
a2h test-data  -c config.toml

# 5. Resume if a load was interrupted.
a2h status -c config.toml
a2h resume -c config.toml
```

### Parallelism on Lite

Lite **rejects concurrent transactions today**, so a chunk can fail under parallel
contention. The loader handles this automatically: after the parallel pass it runs a **serial
mop-up pass**, and because every chunk is idempotent (range-delete + load in one
transaction), the load **converges with no duplicates**. You will see warnings for
chunks that needed the retry. Setting `parallelism = 1` avoids the wasted parallel
attempt entirely on Lite.

## Type mapping

Standard Oracle → HeliosDB mapping (full table in the
[type-mapping reference](../reference/type-mapping.md)). The headline rules:

| Oracle | HeliosDB-Lite |
|---|---|
| `NUMBER(p,s)` | `DECIMAL(p,s)` |
| `VARCHAR2(n)` | `VARCHAR(n)` |
| `CHAR(n)` | `CHAR(n)` |
| `DATE` | `TIMESTAMP` |
| `TIMESTAMP[TZ]` | `TIMESTAMP` / `TIMESTAMP WITH TIME ZONE` |
| `CLOB` | `TEXT` |
| `BLOB` / `RAW` | `BYTEA` |

**Lite stores BLOBs intact**, including embedded null bytes.

## Edition-specific behaviors

A couple of Lite behaviors shape how a2h drives it. On a current build, migrate,
validate, and CDC apply are all green; see
[HeliosDB compatibility](../heliosdb-compatibility.md).

| Behavior | How a2h handles it |
|---|---|
| Concurrent transactions not yet supported | A chunk can fail under parallel load, so the loader runs an automatic **serial-retry** mop-up pass (per-row execute, not `executemany`/pipeline). Chunks are idempotent, so the load converges with no duplicates. `parallelism = 1` avoids the wasted parallel attempt. |
| Parameter encoding | The driver disables client-side prepare (`prepare_threshold = None`) so a few binary-prone parameter types travel as **text** for portability; COPY is the bulk path regardless. |
| Sequences | `CREATE SEQUENCE … INCREMENT BY` may warn and skip on some builds; the table data still migrates. |

## CDC support

**Validated on a current Lite build.** The standard `extract`/`replicat`/`extracts`
cycle applies, with the keyed upsert running natively. For idempotent refreshes you
can also re-run `a2h migrate` (or `a2h resume`). See [docs/cdc.md](../cdc.md).

## See also

- [HeliosDB compatibility](../heliosdb-compatibility.md) — editions, minimum
  builds, and the capability probe.
- [Troubleshooting](../troubleshooting.md) — common errors.
- [CLI reference](../reference/cli.md) · [Type mapping](../reference/type-mapping.md).
