# Any2HeliosDB

**Migrate Oracle, MySQL, PostgreSQL, and SQL Server into HeliosDB — Nano, Lite, or
Full — or into stock PostgreSQL.** A modern, Apache-2.0, clean-room Python
successor to [Ora2Pg](https://github.com/darold/ora2pg): an interactive setup
wizard, a parallel + resumable data load, a live full-screen migration monitor,
structural and row-level validation, a GoldenGate-style change-data-capture (CDC)
engine, and an MCP server — all driving
the target over the PostgreSQL wire protocol (with an experimental Oracle-wire
path).

The CLI is **`a2h`**. Sources: **Oracle, MySQL, PostgreSQL, and SQL Server** —
all validated end-to-end. Targets: **HeliosDB** (Nano/Lite/Full) **or stock
PostgreSQL** through the one `psycopg` PostgreSQL-wire driver — so a2h is also a
straight Oracle/MySQL/SQL-Server → **PostgreSQL** migrator (validated Oracle →
PostgreSQL 16 incl. sequences) — plus a **MySQL** target for heterogeneous /
migrate-back flows.

```bash
pip install any2heliosdb[oracle]   # add [mysql] / [mssql] / [mcp] / [all] as needed
a2h wizard            # connect, smoke-test, write config.toml
a2h migrate -c config.toml
a2h monitor -c config.toml          # (optional, in another terminal) watch it live
a2h test-count -c config.toml && a2h test-data -c config.toml
```

---

## Design principle: a thin translation layer over a runtime probe

> a2h keeps the translation layer thin by adapting to what the **target actually
> supports**, discovered at runtime, instead of hard-coding per-edition behavior.

This is operational, not aspirational. At connect time a **capability probe**
([`target/capability.py`](src/any2heliosdb/target/capability.py)) asks the live
server what it actually accepts — COPY, `ON CONFLICT`, `RETURNING`, `MERGE`,
PL/pgSQL, CHECK/FK enforcement — and the emitters translate *only* what this
target cannot take, degrading gracefully for the rest. Editions are never assumed
from a version string. See
[docs/heliosdb-compatibility.md](docs/heliosdb-compatibility.md) for the supported
editions, minimum versions, and how graceful degradation works.

---

## Install

```bash
pip install any2heliosdb            # core: psycopg (PG-wire) target + CLI
pip install any2heliosdb[oracle]    # + Oracle source (oracledb)
# extras: [mysql]  [mssql]  [mcp]  [all]   (combine, e.g. [oracle,mcp])
```

The `psycopg` PG-wire target is in core, so a source driver is the only extra you
usually need: `[oracle]` (oracledb), `[mysql]` (PyMySQL; `[mysql-cdc]` adds binlog
CDC), `[mssql]` (pyodbc + an ODBC driver), `[mcp]` (the MCP server SDK),
`[nano-manifest]` (run the resumable-load ledger on embedded HeliosDB-Nano — the
[`manifest_backend = "nano"`](docs/guides/configuration.md#manifest_backend--sqlite-vs-embedded-nano)
option), or `[all]`. Python 3.9+; core deps are light (`psycopg[binary]`, `typer`,
`rich`, `jinja2`, `tomli`/`tomli-w`). Verify your environment with `a2h doctor`.

> **PyPI caveat.** The `nano-manifest` extra pulls `heliosdb-nano-embedded`,
> which is **not yet published to PyPI**, so `pip install
> any2heliosdb[nano-manifest]` cannot resolve from PyPI today — install that
> wheel out-of-band (or from a local index) first, or stay on the default
> `manifest_backend = "sqlite"` (stdlib, zero extra deps). `[all]` deliberately
> **excludes** it for exactly that reason: `[all]` and every other extra install
> cleanly from PyPI. It rejoins `[all]` once the wheel is published.

> Developing on a checkout? Use an editable install instead:
> `pip install -e ".[all,dev]"`.

---

## 60-second quick start

```bash
# 1. Point a password at an env var (never store it in the config).
export ORACLE_PW='hr'

# 2. Interactive setup: connects both ends, detects the edition, probes
#    capabilities, round-trips a tiny COPY to prove NULL-vs-empty-string fidelity,
#    and writes config.toml.
a2h wizard

# 3. (optional) Inventory, type mapping and a cost estimate before you commit.
a2h assess -c config.toml

# 4. Schema + data, parallel and resumable.
a2h migrate -c config.toml
#   migrated 2 tables, 8 rows (load_mode=copy)

# 5. Validate. Non-zero exit on any mismatch, so this gates CI.
a2h test-count -c config.toml      # row-count parity
a2h test-data  -c config.toml      # PK-ordered, per-row checksum compare

# 6. If a load was interrupted, continue it with no duplicates.
a2h status -c config.toml
a2h resume -c config.toml
```

### Change data capture (v1)

```bash
a2h extract  cdc1 -c config.toml   # capture source changes into a durable trail
a2h replicat cdc1 -c config.toml   # apply the trail to the target (idempotent)
a2h extracts      -c config.toml   # list extracts + capture/apply positions
```

Oracle capture is **SCN-watermark** — a full snapshot on the first cycle, then
incremental (`ORA_ROWSCN`) after. **MySQL** capture is **log-based binlog** and
**PostgreSQL** capture is **log-based logical decoding** (`test_decoding`) — both
real I/U/D, including deletes. Apply is an idempotent upsert
(`INSERT … ON CONFLICT … DO UPDATE`), so re-running a trail slice never
duplicates. Oracle LogMiner and SQL Server CDC are on the
[roadmap](docs/cdc.md), built on the same Extract → trail → Replicat spine, and
heterogeneous "migrate-back" sinks (HeliosDB → MySQL) are already supported.

---

## Compatibility matrix

**Legend**

| Mark | Meaning |
|---|---|
| ✅ | Validated end-to-end (full integration suite passes against a live server). |
| ⚠️ | Experimental — code-complete and unit-tested, but its live parity test is blocked. |

### Source dialect × target edition (driver + status)

| Source | HeliosDB-Nano | HeliosDB-Lite | HeliosDB-Full | Stock PostgreSQL |
|---|---|---|---|---|
| **Oracle 21c** | `psycopg` ✅ | `psycopg` ✅ · `native` ⚠️ | `psycopg` ✅ · `native` ⚠️ | `psycopg` ✅ |
| **MySQL 8** | `psycopg` ✅ | `psycopg` ✅ | `psycopg` ✅ | `psycopg` ✅ |
| **PostgreSQL 14–16** | `psycopg` ✅ | `psycopg` ✅ | `psycopg` ✅ | `psycopg` ✅ |
| **SQL Server 2022** | `psycopg` ✅ | `psycopg` ✅ | `psycopg` ✅ | `psycopg` ✅ |

The right-most column is **stock PostgreSQL as a target** — the same `psycopg`
driver, so a2h doubles as an `Oracle/MySQL/SQL-Server → PostgreSQL` migrator (see
[migration/to-postgresql.md](docs/migration/to-postgresql.md); Oracle → PostgreSQL
16 validated end-to-end incl. sequences). **PostgreSQL** as a *source* is
validated against real PostgreSQL (Pagila — 65k rows, `timestamptz`, arrays,
composite PKs, declarative partitions).

SQL Server → HeliosDB is validated end-to-end (migrate + `test`/`test-count`/
`test-data`): `sys.*` introspection, `BIT`→BOOLEAN, `DATETIME2`→TIMESTAMP,
`NVARCHAR(MAX)`/`VARBINARY(MAX)` byte-perfect, FK referenced columns resolved.
(Battle-tested against SQL Server 2022 → HeliosDB-Full; the psycopg path is
edition-agnostic, as for the other sources.) Needs `pip install -e ".[mssql]"`
(pyodbc + an ODBC driver).

MySQL→HeliosDB is validated end-to-end (migrate + `test`/`test-count`/`test-data`)
on all three editions: `information_schema` introspection, `TINYINT(1)`→`BOOLEAN`,
`''` preserved (MySQL doesn't fold it to NULL), BLOB/unicode intact. CDC capture
covers **Oracle** (SCN-watermark), **MySQL binlog**, and **PostgreSQL logical
decoding** — all real I/U/D (incl. deletes) except the idempotent Oracle watermark
scan; Oracle LogMiner and SQL Server CDC remain [roadmap](docs/cdc.md).

Minimum HeliosDB build per edition (full details in
[docs/heliosdb-compatibility.md](docs/heliosdb-compatibility.md)):

- **Nano** — `heliosdb-nano` **3.58.5** or newer. The tool gates **CDC apply to
  Nano ≥ 3.58.5** and refuses older builds with a clear error.
- **Lite** — **2.0** or newer. Migrate, validate, **and CDC apply** are green on a
  current build; the loader's serial-retry pass handles Lite's lack of concurrent
  transactions.
- **Full** — a current `main` build. The full suite — migrate, validate, and CDC —
  is green. (a2h emits standard `CREATE SEQUENCE`/`nextval` DDL and degrades to a
  warning on a build that does not yet implement it; table data still migrates.)

a2h does not assume capabilities from a version string — the runtime probe
discovers them per connection and the tool degrades gracefully on an older build.

The `native` (Oracle-wire) driver is **⚠️ experimental** on Lite and Full: it is
code-complete and unit-tested, but its live parity test is blocked on a HeliosDB
Oracle-listener TNS-version handshake; use `psycopg` for production work. `native`
does not apply to Nano.

### Feature support per edition (via the `psycopg` driver)

| Capability | HeliosDB-Nano | HeliosDB-Lite | HeliosDB-Full |
|---|---|---|---|
| Schema DDL (tables, indexes, FKs, views, sequences³) | ✅ | ✅ | ✅ |
| Bulk load | ✅ INSERT (no COPY) | ✅ COPY | ✅ COPY |
| Parallel + resumable load | ✅ | ✅ (serial-retry converges¹) | ✅ |
| Validation (`test` / `test-count` / `test-data`) | ✅ | ✅ | ✅ |
| CDC apply (`replicat`) | ✅ (≥ 3.58.5)² | ✅ | ✅ |

¹ Lite rejects concurrent transactions today, so a chunk can fail under parallel
contention yet succeed on a serial retry; the loader runs a serial mop-up pass and
chunks are idempotent, so the load still converges with no duplicates.

² The tool gates CDC apply to **Nano ≥ 3.58.5** and refuses older builds with a
clear error.

³ Sequences are emitted as standard PostgreSQL DDL. Stock PostgreSQL and
sequence-supporting HeliosDB builds create them natively; on a build that does not
yet implement `CREATE SEQUENCE`/`nextval`, a2h emits the DDL and degrades to a
warning — table data still migrates.

### Heterogeneous targets & migrate-back (v2)

Beyond migrating *into* HeliosDB, the same engine runs **any-to-any**: a
PostgreSQL-wire **source** adapter reads a HeliosDB (or PostgreSQL) server *out*,
and a **MySQL target** driver writes to MySQL — so data can flow **back out of
HeliosDB** (the GoldenGate-reverse direction).

| Direction | Driver path | Status |
|---|---|---|
| Oracle → MySQL | `oracledb` → `mysql` | ✅ validated (migrate + TEST_COUNT + TEST_DATA) |
| **HeliosDB → MySQL** (migrate-back) | `postgres` source → `mysql` | ✅ validated (migrate + TEST_COUNT) |
| MySQL → HeliosDB | `pymysql` → `psycopg` | ✅ validated (all editions) |

When **HeliosDB is the source**, its PG-wire catalog exposes no PK/FK/index
metadata and no column precision/scale today, so chunking falls back to a single
chunk and `test-data` self-handles the PK-less case — **`test-count` (row parity)
is the gate** for migrate-back; the tool works around the missing catalog
metadata. (A SQL Server *sink* — migrate-back *into* SQL Server — is not
implemented; the heterogeneous target drivers are `psycopg` and `mysql`. SQL
Server as a *source* is validated, per the matrix above.)

---

## The target drivers

- **`psycopg`** (default) — PostgreSQL wire via psycopg v3. Portable across
  **Nano / Lite / Full**. The tool performs the Oracle→PG dialect translation;
  the capability probe decides per connection what must be rewritten vs. passed
  through. This is the validated, production path into HeliosDB.
- **`mysql`** — a MySQL **target** (PyMySQL) for heterogeneous / migrate-back
  flows (`INSERT` + `ON DUPLICATE KEY UPDATE`); validated as the sink for
  Oracle→MySQL and HeliosDB→MySQL.
- **`native`** *(experimental)* — connects through the **same wire protocol as the
  source** (Oracle TNS → Lite/Full via `oracledb`), so HeliosDB's in-database
  compatibility absorbs the dialect and the tool transforms almost nothing. Bulk
  load is array `INSERT` (no COPY). Code-complete and unit-tested; live
  validation pending a HeliosDB TNS-version fix.

Pick the driver in `[target].driver` (`"psycopg"`, `"mysql"`, or `"native"`); see
[configuration](docs/guides/configuration.md#driver-selection).

---

## Documentation

| Guide | What's inside |
|---|---|
| [docs/](docs/README.md) | The full documentation index. |
| [Getting started](docs/guides/getting-started.md) | Install, prerequisites, `doctor`, wizard, the end-to-end workflow with real transcripts. |
| [Configuration](docs/guides/configuration.md) | Every `config.toml` field, env-var passwords, driver selection, tuning. |
| [Oracle → Lite](docs/migration/oracle-to-heliosdb-lite.md) · [→ Full](docs/migration/oracle-to-heliosdb-full.md) · [→ Nano](docs/migration/oracle-to-heliosdb-nano.md) | Per-target migration guides. |
| [MySQL & SQL Server](docs/migration/mysql-and-mssql.md) | The MySQL and SQL Server source guides. |
| [→ PostgreSQL](docs/migration/to-postgresql.md) | a2h as an Oracle/MySQL/SQL-Server → stock PostgreSQL migrator. |
| [CDC](docs/cdc.md) | Extract → trail → Replicat, the verbs, v1 limits, v2 roadmap. |
| [MCP server](docs/mcp.md) | Expose the toolkit as MCP tools (Bearer auth + RBAC) for AI agents. |
| [CLI reference](docs/reference/cli.md) | Every `a2h` command and option. |
| [Type mapping](docs/reference/type-mapping.md) | The full Oracle→HeliosDB table + overrides. |
| [HeliosDB compatibility](docs/heliosdb-compatibility.md) | Supported editions, minimum versions, and the runtime capability probe. |
| [Troubleshooting](docs/troubleshooting.md) | Common issues + the per-edition minimum builds. |

---

## Development

```bash
pip install -e ".[all,dev]"
pytest -q                       # hermetic unit tests

# Integration tests need a live source + target:
A2H_TEST_TARGET_PORT=<port> A2H_TEST_ORACLE_DSN=host:1521/SVC \
A2H_TEST_ORACLE_USER=hr A2H_TEST_ORACLE_PW=hr pytest tests/integration -q
```

## License

Apache-2.0. Clean-room implementation — no Ora2Pg source is copied; only public
catalog-query knowledge and the Apache-licensed HeliosDB migration scaffold are
referenced.
