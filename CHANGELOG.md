# Changelog

All notable changes to Any2HeliosDB are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Oracle procedural & advanced objects are now visible (v1.0.0 "Option A").**
  The Oracle source adapter introspects stored **routines** (PROCEDURE / FUNCTION
  / PACKAGE), **triggers**, **materialized views**, and detects **partitioned
  tables** — previously none of these were seen, so `a2h assess` under-reported a
  schema (e.g. "0 routines" for a package-heavy DB). They are now:
  - **counted** in `a2h assess` / SHOW_REPORT and folded into the person-day cost,
  - **gap-reported** (one `DEGRADED` gap each — the data migration still succeeds),
  - written verbatim to a **`<schema>.review.sql`** companion by `a2h export` for
    manual porting.
  These objects are **not auto-translated** (the thin-tool bar); PL/SQL →
  PL/pgSQL auto-translation is the **v2.0.0** roadmap (see
  `docs/reference/oracle-object-support.md`). A materialized view's container
  table is excluded from the migrated table set (it's handled as an mview).
  Validated on a rich Oracle HR schema (procedure, function, package, trigger,
  materialized view, range-partitioned table) → PostgreSQL: data tier migrates
  (incl. the partitioned table as a flat table) with TEST_COUNT/TEST_DATA 4/4,
  while the six procedural/advanced objects are surfaced for review.

## [0.9.5] — 2026-06-26

### Added
- **PostgreSQL-source sequence migration.** The PG source adapter now
  introspects sequences (`pg_sequences`, falling back to
  `information_schema.sequences`) and preserves each SERIAL/IDENTITY column's
  `DEFAULT nextval('seq')` (normalized to a bare, schema-unqualified reference).
  Each target sequence's `START` is set to the *next* value the source would
  produce (read from live `last_value`/`is_called`) so post-migration inserts
  resume past the loaded rows instead of colliding. Sequences are now created
  **before** tables so a `DEFAULT nextval` resolves at `CREATE TABLE` time, and
  the emitter carries the source `CACHE` size. The Oracle/MySQL emitters skip a
  PG `nextval()` default (those dialects spell auto-increment differently), so
  migrate-back is unaffected. Validated Pagila → HeliosDB-Nano 3.60.0: all 13
  sequences created with correct resume points and a working `DEFAULT nextval`
  (insert without the PK auto-increments), TEST_COUNT/TEST_DATA/TEST_INDEX 15/15.

### Fixed
- **Native (Oracle-wire) TIMESTAMP fractional seconds dropped.**
  python-oracledb defaults a `datetime` bind to `DB_TYPE_DATE` (7-byte, no
  fractional seconds), truncating sub-second precision client-side before it
  reaches HeliosDB. The native driver now binds datetime positions as
  `DB_TYPE_TIMESTAMP` (`setinputsizes`), so `TIMESTAMP(6)` values round-trip
  exactly. Completes the native Oracle → HeliosDB-Full path (TEST_DATA 0
  mismatches incl. BLOB/RAW/fractional-TIMESTAMP).

## [0.9.4] — 2026-06-25

### Fixed
- **Native (Oracle-wire) target: a successful migration could hang on close.**
  After a native `migrate` finished and committed, the driver's `close()` blocked
  in oracledb's `Protocol._reset` because HeliosDB-Full does not answer the
  graceful logoff/close handshake — the data was already persisted, but the run
  never returned. `close()` is now best-effort (caps the close wait at 5s and
  swallows close-handshake errors), and `connect()` sets a generous per-call
  timeout (300s) so a stalled bulk array-INSERT round-trip fails fast rather than
  blocking forever. A native Oracle → HeliosDB-Full migrate now completes.
- **Validators read zero rows from a native (Oracle-wire) target.** The native
  path keeps source-case identifiers (e.g. `"DEPARTMENTS"`), but `test-count` /
  `test-data` / `test-index` folded names to lowercase and queried a wrong-cased
  relation, reporting an empty target that in fact held the data. They now derive
  the effective identifier case the same way the migration does
  (`keep_source_case = preserve_case OR oracle-dialect`), rendering names exactly
  as the migration created them. TEST_COUNT and TEST_INDEX now pass on a native
  Oracle → HeliosDB-Full migrate. (TEST_DATA on DATE/TIMESTAMP/RAW columns is
  gated on a HeliosDB-Full TTC read-path type-tagging fix, tracked separately —
  the data is stored correctly; only the read-back column metadata is wrong.)
- **`a2h test-index` false positive on HeliosDB-Nano** (and any target that can't
  evaluate a nested scalar subquery with a cast). The check compared an
  index-eligible count against an index-defeated
  `col::text = (SELECT min(col))::text` count, but Nano can't materialise that
  nested cast-subquery and returned NULL — which the check misread as a mismatch
  (it reported a stale FK index where none existed). Rewritten to a portable,
  fail-safe form: ground-truth per-value counts come from a `GROUP BY` (no cast,
  no nested subquery, no bind params), and the most-common value's true count is
  compared against an index-eligible equality lookup; a column whose probe errors
  or whose sample value can't be rendered is skipped (DEGRADED), never failed.
  Validated: Oracle → HeliosDB-Nano 3.58.5 and Oracle → stock PostgreSQL both clean.

## [0.9.3] — 2026-06-24

### Added
- **`a2h test-index` — target-side FK-index sanity check** (`TEST_INDEX`). For
  each foreign-key column it compares an index-eligible equality count
  (`WHERE col = (SELECT min(col) …)`) against an index-defeated full-scan count
  (`WHERE col::text = (…)::text`); a mismatch means the target answered the
  lookup from a stale/empty index — e.g. an FK index auto-created by
  `ADD FOREIGN KEY` but not backfilled from rows loaded before the FK was added
  (the class of bug fixed in HeliosDB-Nano 3.58.5). Standard `test-data` can't
  catch it because it never filters or joins on an FK column. Target-only,
  type-agnostic (`::text` defeat), exits non-zero on a mismatch so it gates CI.
  Validated against Sakila → stock PostgreSQL: real FK columns probed
  (e.g. `film_category.category_id` 64 = 64 over 1,000 rows), no false alarms.

## [0.9.2] — 2026-06-24

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
- **MySQL `GROUP_CONCAT` → PostgreSQL `string_agg`** (Sakila `film_list`,
  `nicer_but_slower_film_list`, `actor_info`): `GROUP_CONCAT([DISTINCT] expr
  [ORDER BY ...] [SEPARATOR s])` now becomes `string_agg((expr)::text, s [ORDER
  BY ...])` — MySQL's default `,` separator is supplied, the value is cast to
  text so non-text columns aggregate, multi-arg forms wrap in `concat(...)`, and a
  nested `GROUP_CONCAT` in a subquery (with its own ORDER BY / SEPARATOR) is
  translated too. Where MySQL combines `DISTINCT` with an `ORDER BY` over a
  different column (which PostgreSQL rejects), a2h orders by the aggregated value
  instead (valid + deterministic). Validated: Sakila → stock PostgreSQL 16 — all
  three views create and query (997 / 997 / 200 rows).
- **Loose MySQL `GROUP BY` → strict PostgreSQL `GROUP BY`** (Sakila
  `sales_by_store`): MySQL (with `ONLY_FULL_GROUP_BY` off) allows selecting
  columns that are neither aggregated nor grouped; PostgreSQL rejects them. a2h
  now appends each qualified, non-aggregate SELECT column reference to the
  `GROUP BY` (aggregated columns skipped, already-grouped columns not duplicated,
  only the outermost query touched). With this, **all 8 Sakila views migrate to
  stock PostgreSQL**.

### Changed
- **Minimum HeliosDB-Nano version pinned to 3.58.5** (was 3.58.3). Nano 3.58.5
  fixes a silent data-correctness bug where the index auto-created by `ALTER
  TABLE … ADD FOREIGN KEY` was not backfilled from existing rows — so an
  index-driven lookup or join on an FK column (after the load-then-add-FK
  migration order) could return too few rows while a full scan returned the
  correct data. The capability gate and compatibility doc now require ≥ 3.58.5.

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
