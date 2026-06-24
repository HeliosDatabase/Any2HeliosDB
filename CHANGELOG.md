# Changelog

All notable changes to Any2HeliosDB are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **MySQL → PostgreSQL view-body translation** (reported via Sakila `staff_list`
  / `customer_list`): a backtick-quoted alias containing a space (or a reserved
  word / mixed case) was emitted **unquoted** — ``AS `zip code` `` became the
  invalid `AS zip code` — because the MySQL adapter stripped *all* backticks. The
  adapter now preserves backtick quoting (dropping only the schema qualifier),
  and the view-body translator renders each identifier through the shared quoter
  (`AS "zip code"`; a plain `lower_snake` name stays bare). MySQL scalar
  `IF(c, a, b)` is translated to `CASE WHEN c THEN a ELSE b END` (balanced-paren,
  quote-aware argument split, nested-`IF` safe). And `a2h export` now applies the
  same target-dialect view translation that `migrate` does, so an exported view
  is valid target SQL instead of raw source SQL. Validated end-to-end: Sakila →
  stock PostgreSQL 16, `staff_list` / `customer_list` create and query cleanly.

## [0.9.1] — 2026-06-24

### Added
- **`a2h monitor` — live full-screen migration dashboard.** A new command that
  reads the run manifest READ-ONLY (sqlite WAL allows a concurrent reader) and
  renders a Rich full-screen view of an in-progress (or finished) run: per-table
  status, chunks loaded/total, rows loaded, a progress bar, and **volume left**,
  plus roll-up totals (tables done, overall %, aggregate volume left, elapsed,
  ETA). `a2h monitor -c <cfg>` attaches to the exact run a concurrent
  `a2h migrate -c <cfg>` is writing; `--once` renders a single frame for
  non-TTY/CI. The loader now records a source row estimate (one `count(*)` per
  table at plan time, guarded) so volume-left and ETA are meaningful.
  Demonstrated live on a 49,636-row PostgreSQL → PostgreSQL Pagila migration
  (done-counter climbing, volume-left counting down, in-flight tables shown).
- **Stock PostgreSQL as a first-class target.** The `psycopg` driver already
  speaks the PostgreSQL wire protocol, so pointing `[target]` at a real
  PostgreSQL server migrates Oracle/MySQL/SQL-Server → PostgreSQL (a2h as a
  straight Ora2Pg replacement, not only → HeliosDB). The capability probe now
  recognises a stock-PostgreSQL banner (`Edition.POSTGRES`) instead of
  `unknown`, and the wizard names PostgreSQL as a valid target. Validated
  **Oracle → PostgreSQL 16** end-to-end (migrate + test-count + test-data,
  including the Oracle sequence — which real PostgreSQL creates natively).
- **View translation on the PG-wire path** (#36). A source view's body is run
  through the dialect rewriter (NVL/DECODE/SYSDATE/ROWNUM/seq.NEXTVAL/`(+)`) so an
  Oracle/MySQL view doesn't reach the target as raw source SQL (native-Oracle and
  MySQL targets keep the source body).

### Fixed (hardening against stock PostgreSQL — HeliosDB's leniency had masked these)
- **BOOLEAN column defaults** translate to boolean literals: a MySQL
  `TINYINT(1)`→`BOOLEAN` column with `DEFAULT 1/0` was emitted as `BOOLEAN
  DEFAULT 1`, which strict PostgreSQL rejects → now `DEFAULT true/false`.
- **Boolean comparisons in views** (`WHERE <boolcol> = 1`) are normalized to
  `= true/false` for the schema's actual BOOLEAN columns, so a MySQL view migrates
  to strict PostgreSQL.
- **Schema-unique index names**: Oracle/MySQL allow the same index name on
  different tables; PostgreSQL-family targets require it unique, so the second
  index was skipped — now deduped (table-prefixed, then a counter).
- **Sequence idempotency**: `drop_existing` now drops sequences too, so a re-run
  re-creates them cleanly instead of warning "already exists".
- **Silent data loss on `drop_existing` re-migrate** (data correctness): a
  re-migration recreated the tables empty, but the resume manifest still marked
  the chunks `LOADED`, so the loader skipped them and reported stale success.
  `drop_existing` now resets the run's chunk state (while `resume` still
  continues from the manifest), so the recreated tables are always reloaded.
- **CDC Nano gate → 3.58.3** (the `E'…'` literal fix makes bytea `ON CONFLICT`
  upserts correct).

### Validated
- **The HeliosDB-Full target gaps surfaced in v0.9.0 are now fixed in Full
  `main` and confirmed end-to-end through a2h** (Full rebuilt from `main`,
  PG-wire): an Oracle sequence (`EMP_SEQ`) migrates as a real `CREATE SEQUENCE`
  with its value preserved — `nextval` returns 100, no degrade-to-warning
  (**#44**); `sum(<const>)` is correct on a multi-row table — `sum(1)` = row
  count, `sum(5)` = 5×rows (**#42**); and the psycopg3 extended-protocol path
  (PK-sampled parameterized `TEST_DATA`) runs with no SELECT desync (**#22**).
  Oracle HR → Full end-to-end: `TEST_COUNT` 3/3 matched, `TEST_DATA` 0
  mismatches across all tables.

## [0.9.0] — 2026-06-23

Migration-correctness milestone: the HeliosDB-Full composite-PK count bug is
fixed (so row-count validation is trustworthy), a full adversarial review
hardened the tool end-to-end, and AI-agent administration (MCP) shipped.

### Added
- **Log-based CDC — MySQL binlog**: a `mysql-replication` ROW-binlog capture
  source producing real `I`/`U`/`D` change records (**including deletes**) with
  the binlog coordinate as the cursor; new `[mysql-cdc]` extra. The CDC engine
  dispatches capture by source dialect (Oracle SCN-watermark vs MySQL binlog).
- **MCP server** (`a2h mcp serve`): JSON-RPC server exposing 16 engine tools for
  AI-agent administration, with Bearer-token auth + viewer/operator/admin RBAC
  over HTTP and stdio (stdlib JSON-RPC fallback on Python 3.9). New `[mcp]` extra.
- **CLI reference + worked-examples** docs (`docs/reference/cli.md`,
  `docs/guides/examples.md`).

### Fixed (correctness — from the v0.9.0 adversarial review)
- **Identifier quoting unified** across DDL / loader / validators via
  `core.identifiers`: reserved words and `preserve_case` render byte-identically,
  so a reserved/mixed-case table or column can no longer be created under one
  name and then loaded/validated under another.
- **TEST_DATA** no longer false-fails on `timestamptz` (compares the UTC instant
  regardless of tz-aware vs naive wire form) or `ARRAY` columns (serializes a
  source list to the exact PG array literal the target stores).
- **PostgreSQL source**: pins `search_path` to the source schema (unqualified
  reads can't hit a same-named table in another schema); reserved-aware reads;
  composite-FK columns paired by ordinal via `pg_catalog` conkey/confkey;
  `numeric_pk_bounds` floors so a negative fractional PK can't skip rows.
- **Declarative-partition children** excluded from the PostgreSQL source (no
  duplicate data; parent table carries all rows).
- **Chunking** range-DELETE predicate uses the shared quoter (reserved/mixed PK).
- **`a2h resume`** exits non-zero on remaining failed chunks, like `migrate`.
- **CDC apply** preserves trail order (a `DELETE`→`INSERT` leaves the row present).
- **CDC MySQL binlog** fails closed unless `binlog_format=ROW`,
  `binlog_row_metadata=FULL`, and `binlog_row_image=FULL`.
- **Type map**: `timestamp without time zone` no longer promoted to timestamptz;
  bare Oracle `NUMBER` → `NUMERIC`; PG `USER-DEFINED`/`ARRAY`/tsvector/… → `TEXT`.
- **Oracle source**: FK referenced columns read from the referenced constraint;
  sequence `START WITH` from `LAST_NUMBER`; floor()-based PK bounds.

### Validated
- End-to-end against **HeliosDB-Full rebuilt with the composite-PK count fix**
  (issue #37, merged to Full `main`): **Pagila (real PostgreSQL — 65k rows with
  `timestamptz`, arrays, composite PKs, declarative partitions), Oracle, and
  MySQL → Full** all green (migrate + `test-count` + `test-data`, 0 mismatches).
  SQL Server 2022 → Full validated previously. HeliosDB-Lite (PG-compat batch)
  and Nano 3.58.2 (`ON CONFLICT`) validated when those target fixes were
  committed; a fresh Lite/Nano re-run is deferred (a `heliosdb-lite start`
  headless-daemon defect, tracked target-side).

### Target gaps surfaced (fix-in-HeliosDB, not tool bugs)
- HeliosDB-Full: `CREATE SEQUENCE` / `nextval` unimplemented over PG-wire (a2h
  emits standard PG sequence DDL and degrades to a warning); `sum(<const>)`
  wrong on composite-PK tables (the count(*)/count(1) sibling of #37).

## [0.9.0-rc2] — 2026-06-23

Heterogeneous targets, migrate-back, and CDC deletes — the engine is now
any-to-any, not just any-to-HeliosDB.

### Added
- **MySQL target driver** (`[target] driver="mysql"`, PyMySQL; `INSERT` +
  `ON DUPLICATE KEY UPDATE`) and a **PostgreSQL-wire source adapter** (read a
  HeliosDB/PostgreSQL server *out*), plus an orchestrator **dialect dispatch**
  (postgres / oracle / mysql) for DDL + identifiers. Validated **Oracle→MySQL**
  and **HeliosDB→MySQL** (migrate-back, the GoldenGate-reverse direction).
- **CDC delete reconciliation** (`replicat --reconcile-deletes/--no-deletes`):
  a source/target key-set diff removes rows deleted at the source; battle-tested
  and CI-asserted.
- Validation normalization (numeric trailing-zero, boolean, bytea-hex) so row
  checksums agree across drivers, editions, and heterogeneous targets.

### Found (filed against HeliosDB, per the governing principle)
- **#35** — PG-wire catalog/introspection gaps (no PK/FK/index or precision
  metadata; catalog queries reject bind parameters) that limit HeliosDB as a
  *source*; the tool works around them (`test-count` gates migrate-back).

### Suites
- 76 unit + 7 integration tests green: Oracle/MySQL → Lite/Full/Nano,
  Oracle → MySQL, HeliosDB → MySQL, CDC snapshot + incremental + deletes.

## [0.9.0-rc1] — 2026-06-23

First public-shaped release candidate. **Oracle and MySQL → HeliosDB** (Lite,
Full, Nano) via the `psycopg` driver, validated end-to-end against live servers,
plus a full documentation set with a compatibility matrix.

### Added in 0.9.0-rc1
- **MySQL source adapter** — `information_schema` introspection to the canonical
  IR, `SSCursor` streaming, `TINYINT(1)`→`BOOLEAN`; validated migrate + validate
  on Lite, Full, and Nano (Oracle-parity).
- **Native (Oracle-wire) driver** — `NativeOracleDriver` + Oracle-dialect DDL +
  orchestrator integration; code-complete and unit-tested (live parity test
  gated on a HeliosDB TNS-version fix).
- **Documentation** — README compatibility matrix, getting-started + configuration
  guides, per-target migration guides, CDC guide, CLI + type-mapping reference,
  troubleshooting.
- Validation hardening: boolean and BYTEA-hex-string normalization so checksums
  agree across drivers/editions.

### Carried from the pre-v2 core
- **CLI `a2h`** (Typer + Rich): `doctor`, `wizard`, `assess`, `export`,
  `migrate`, `load`, `status`, `resume`, `test`/`test-count`/`test-data`,
  `report`, `extract`/`replicat`/`extracts`.
- **Oracle source adapter** — `ALL_*` introspection to a canonical IR; streamed
  extraction with tuned arraysize; LOB/NULL/empty-string fidelity.
- **Type map + DDL emitters** — data-driven, `DATA_TYPE`/`MODIFY_TYPE`
  overridable; FKs emitted after data; idempotent views.
- **Target drivers** — portable `psycopg` driver with an edition/version
  capability probe (Nano/Lite/Full); `native` (Oracle TNS) on the roadmap.
- **Data engine** — PK-range chunking, parallel `ThreadPoolExecutor` load, and a
  durable SQLite-WAL manifest. Chunks are idempotent (DELETE-range + load in one
  transaction); `resume` continues an interrupted load with no duplicates. A
  serial-retry pass lets parallel loads converge even when a target can't run
  concurrent transactions.
- **Validation** — TEST (structure), TEST_COUNT (row counts), TEST_DATA
  (PK-sampled value + checksum); non-zero exit for CI gating.
- **Wizard + smoke test** — connect both ends, detect edition, probe
  capabilities, and round-trip a tiny COPY to prove NULL-vs-empty-string fidelity.
- **CDC spine (v1)** — Oracle SCN-watermark capture → durable append-only trail
  → idempotent replicat; independent capture watermark and apply cursor.
  Battle-tested end-to-end against HeliosDB-Full (snapshot + incremental capture,
  UPDATE/INSERT propagation, idempotent re-apply).
- **PL/SQL rewrite + gap report** and **assessment** (SHOW_*/SHOW_REPORT) with
  cost scoring.

### Fixed (HeliosDB, via the target-gap workflow)
- Lite: `version()`/`current_setting()`, COPY-FROM-STDIN permission + quoted
  identifiers.
- Full: trust auth without SASL, COPY-FROM-STDIN parsing, binary-param null-byte
  check.

### Known gaps (resolved in current builds)
- Lite (rc1-era): NUMERIC/DECIMAL columns created as type `unknown` (breaks
  numeric comparison), parameterized WHERE matched nothing, ON CONFLICT ignored
  with bind params, concurrent transactions rejected. The tool worked around
  these (literal SQL, serial-retry) and CDC apply is proven on Full; **all are
  resolved in the current Lite build** — see
  [HeliosDB compatibility](docs/heliosdb-compatibility.md).

### Notes
- Python 3.9+. Apache-2.0. Clean-room implementation.
