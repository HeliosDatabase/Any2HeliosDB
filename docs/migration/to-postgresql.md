# Migrating to PostgreSQL (a2h as an Ora2Pg replacement)

Any2HeliosDB targets **any PostgreSQL-wire server** through its `psycopg` driver.
HeliosDB (Nano/Lite/Full) is one such target; **stock PostgreSQL is another** —
so a2h migrates `Oracle / MySQL / SQL Server → PostgreSQL` with no extra code and
the same pipeline you use for HeliosDB: parallel + resumable load, `test` /
`test-count` / `test-data` validation, and CDC.

Because PostgreSQL is the reference implementation of the wire and SQL surface,
a target running stock PostgreSQL has **fewer gaps than HeliosDB** — for example
it creates sequences natively (`CREATE SEQUENCE` / `nextval`), runs full
PL/pgSQL, and supports every built-in type — so migrations to PostgreSQL are
often cleaner than to a HeliosDB edition still filling in compatibility.

## Configuration

Only the `[target]` block changes — point it at your PostgreSQL host. The
`driver` stays `psycopg` (the same PG-wire driver HeliosDB uses).

```toml
[source]
dialect = "oracle"            # or mysql / postgresql / mssql
host = "oracle.internal"
port = 1521
service_name = "XEPDB1"
schema = "HR"
user = "hr"
password_env = "ORA_PW"       # password read from $ORA_PW

[target]
driver = "psycopg"            # PostgreSQL wire — HeliosDB or stock PostgreSQL
host = "postgres.internal"
port = 5432
dbname = "appdb"
user = "postgres"
password_env = "PG_PW"        # blank/omitted for a trust-auth dev server

[options]
parallelism = 4
drop_existing = false         # set true to recreate target tables each run
```

```bash
export ORA_PW='…'  PG_PW='…'
a2h migrate     -c config.toml      # schema + parallel data load
a2h test-count  -c config.toml      # row-count parity
a2h test-data   -c config.toml      # per-row SHA256 content parity
```

The wizard names PostgreSQL as a valid target and runs the same smoke test
(connect both ends, detect the server, probe capabilities, prove a `\N`-vs-`''`
COPY round-trip) before writing the config:

```bash
a2h wizard
```

## What you get

| Stage | Behavior on a PostgreSQL target |
|---|---|
| Schema | Tables, PK/FK, indexes, **sequences (native)**, views emitted from the IR. |
| Types | Oracle/MySQL/MSSQL types mapped to PostgreSQL types; `NUMBER`→`NUMERIC`, `VARCHAR2`→`VARCHAR`, `CLOB`→`TEXT`, `BLOB`/`RAW`→`BYTEA`, `DATE`/`TIMESTAMP`→`TIMESTAMP[TZ]`, etc. |
| Bulk load | `COPY FROM STDIN` fast path, parallel + chunked, idempotent per chunk. |
| Resume | `a2h resume` continues from the SQLite manifest after an interruption (and exits non-zero if any chunk is still failed). |
| Validation | `test` (object inventory), `test-count` (row counts), `test-data` (ordered SHA256 sample; tz-instant + array aware). |
| CDC | `extract` → trail → `replicat` idempotent apply works against PostgreSQL like any PG-wire target. |

## Validated

**Oracle → PostgreSQL 16** end-to-end: `migrate` + `test-count` + `test-data`
all green (0 mismatches), and the Oracle sequence migrated and `nextval`
advanced correctly on the PostgreSQL side. The same path applies to MySQL and
SQL Server sources.

## Notes & differences from a HeliosDB target

- **Capability probe** reports `edition = postgres` for a stock PostgreSQL server
  (HeliosDB reports `nano`/`lite`/`full`). Capabilities are always discovered by
  the probe at connect time, never assumed from this label.
- **Parameter encoding.** The driver text-encodes a few binary-prone parameter
  types (`datetime`, `Decimal`) for portability across HeliosDB editions. This is
  harmless on PostgreSQL, which casts the text form natively.
- **Fewer workarounds fire.** The dialect-rewrite passes that translate Oracle
  constructs only run when the target's probe rejects them; on PostgreSQL most
  pass through, and the emitted DDL is close to what `pg_dump` would accept.
- **Schema target.** Data lands in the target's default schema (or set
  `search_path`/create the schema beforehand); see `docs/guides/configuration.md`.

## See also

- `docs/guides/getting-started.md` — first migration walkthrough.
- `docs/guides/configuration.md` — full `config.toml` reference.
- `docs/reference/type-mapping.md` — per-dialect type rules.
- `docs/migration/oracle-to-heliosdb-full.md` — the HeliosDB-target variant.
