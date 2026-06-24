# Oracle → HeliosDB-Nano

**Status: ✅ validated** for migrate + validation + resume via the `psycopg`
driver, against Oracle 21c XE. Load is **INSERT-based** (Nano has no COPY). CDC
apply requires **Nano ≥ 3.58.3**.

Nano is the Apache edition. The a2h integration suite passes against a current
Nano build: migrate + validate, the wizard smoke test, and resumable migrate +
resume.

## Prerequisites

- **HeliosDB-Nano 3.58.3 or newer.** See
  [HeliosDB compatibility](../heliosdb-compatibility.md) for the capability probe
  and minimum builds. Keep Nano current — older builds may have target-side gaps
  that current releases have closed.
- An Oracle source you can introspect and read.
- `pip install -e ".[oracle]"`; verify with `a2h doctor`.

Nano's server banner looks like `16.0 (HeliosDB Nano 3.58.3)`; the probe
classifies it as `nano`.

## Driver choice

Use **`psycopg`** — it is the only driver for Nano. The `native` (Oracle-wire)
driver does **not** apply to Nano (no Oracle listener; it is Lite/Full-only).

```toml
[target]
driver = "psycopg"
host = "127.0.0.1"
port = 5432            # your Nano PG-wire port
dbname = "postgres"
user = "postgres"
password_env = "HELIOS_PW"    # omit for a trust Nano
```

## Load mode: INSERT, not COPY

Nano has **no COPY FROM STDIN**, so the capability probe reports
`copy_from_stdin = false` and the loader automatically uses the **batched INSERT**
path (per-row synchronous execute). You'll see `load_mode=insert`:

```
$ a2h migrate -c config.toml
migrated 2 tables, 8 rows (load_mode=insert)
  DEPARTMENTS -> 3
  EMPLOYEES -> 5
```

`prefer_copy = true` is harmless — the probe overrides it where COPY is absent.
Parallel + resumable load still works; chunks are loaded via INSERT and remain
idempotent (range-delete + load in one transaction).

## Step by step

```bash
export ORACLE_PW='hr'
export HELIOS_PW='heliosdb'

# 1. Configure + smoke-test.
a2h wizard
#   target_edition  nano
#   copy_from_stdin False          <- expected on Nano
#   null_empty_fidelity True OK

# 2. Inventory + type mapping (read-only).
a2h assess -c config.toml

# 3. Schema + data (INSERT-based).
a2h migrate -c config.toml
#   migrated 2 tables, 8 rows (load_mode=insert)

# 4. Validate.
a2h test       -c config.toml
a2h test-count -c config.toml
a2h test-data  -c config.toml

# 5. Resume if interrupted.
a2h status -c config.toml
a2h resume -c config.toml
```

## Type mapping

Standard Oracle → HeliosDB mapping (full table in the
[type-mapping reference](../reference/type-mapping.md)):

| Oracle | HeliosDB-Nano |
|---|---|
| `NUMBER(p,s)` | `DECIMAL(p,s)` |
| `VARCHAR2(n)` | `VARCHAR(n)` |
| `CHAR(n)` | `CHAR(n)` |
| `DATE` | `TIMESTAMP` |
| `TIMESTAMP[TZ]` | `TIMESTAMP` / `TIMESTAMP WITH TIME ZONE` |
| `CLOB` | `TEXT` |
| `BLOB` / `RAW` | `BYTEA` |

All columns round-trip on a current Nano build (`NUMBER → numeric`, `VARCHAR2`,
`DATE`, `CLOB`, NULLs, BLOB, and `'' ≠ NULL` fidelity all verified).

## Edition-specific behaviors

A couple of Nano behaviors are worth knowing. On a current build the full
migrate/validate workflow is green; see
[HeliosDB compatibility](../heliosdb-compatibility.md).

| Behavior | How a2h handles it |
|---|---|
| No COPY (Apache edition) | Load uses the batched INSERT path automatically. |
| `bytea` returned as a hex string (`'\x…'`) | Nano returns `bytea` as the PG hex *string* rather than decoded bytes; a2h's `test-data` normalizes it to the same hash, so validation passes. |
| Sequences | `CREATE SEQUENCE … INCREMENT BY` may warn and skip on some builds; the table data still migrates. |

## CDC support

**Validated on Nano ≥ 3.58.3.** The standard `extract`/`replicat`/`extracts` cycle
applies. Against an **older Nano**, a2h refuses CDC apply at runtime with a clear
error (rather than a cryptic mid-apply failure) — upgrade Nano, or keep the target
current with an idempotent re-run of `a2h migrate` (or `a2h resume`). See
[docs/cdc.md](../cdc.md).

## See also

- [HeliosDB compatibility](../heliosdb-compatibility.md) — editions, minimum
  builds, and the capability probe.
- [CLI reference](../reference/cli.md) · [Type mapping](../reference/type-mapping.md).
- [Troubleshooting](../troubleshooting.md).
