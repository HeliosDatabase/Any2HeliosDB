# HeliosDB compatibility

Any2HeliosDB targets HeliosDB over the **PostgreSQL wire protocol** through the
one `psycopg` driver, and the same driver also targets **stock PostgreSQL**. This
page states the supported editions and minimum versions, and explains how a2h
adapts to whatever the target actually supports.

## Supported targets and minimum versions

| Target | Minimum version | Bulk load | Notes |
|---|---|---|---|
| **HeliosDB-Nano** | 3.58.5 | INSERT (no COPY) | The Apache edition; PG-wire. CDC apply is gated to **Nano ≥ 3.58.5**. |
| **HeliosDB-Lite** | 2.0 | COPY | PG-wire. Concurrent transactions are not yet supported, so the loader uses a serial-retry mop-up pass (the load still converges with no duplicates). |
| **HeliosDB-Full** | current `main` build | COPY | PG-wire. The most complete edition: migrate, validate, and CDC. |
| **Stock PostgreSQL** | 14+ | COPY | The reference PG-wire target; creates sequences natively and runs full PL/pgSQL. |

All four are reached with **`pip install any2heliosdb`** and the default
`psycopg` driver — no edition flag. The capability probe (below) classifies the
edition at connect time from what the server reports.

> Keep your HeliosDB build current. a2h's validated cells assume a recent build
> of each edition; older builds may have target-side gaps that current releases
> have already closed (see "Runtime capability probing").

## Runtime capability probing

a2h does **not** assume a target's abilities from a version string. At connect
time it runs a **capability probe**
([`target/capability.py`](../src/any2heliosdb/target/capability.py)) that asks the
live server what it actually accepts — COPY, `ON CONFLICT`, `RETURNING`, `MERGE`,
PL/pgSQL, CHECK / FK enforcement, and empty-string-vs-NULL fidelity — and the
emitters translate **only** what this particular target cannot take.

The practical consequences:

- **One `migrate` works across Nano / Lite / Full and stock PostgreSQL** without
  edition-specific flags. The same command adapts per connection.
- **Graceful degradation.** Where a target lacks COPY (Nano), the loader falls
  back to batched INSERT automatically. Where it lacks concurrent transactions
  (Lite), the loader adds a serial-retry pass. Where it lacks native
  `CREATE SEQUENCE`, a2h emits the standard DDL and degrades to a warning while
  table data still migrates.
- **Validation catches target divergence.** `test` / `test-count` / `test-data`
  compare source and target structurally and row-by-row (binary, numeric, and
  boolean values are canonicalized so they compare equal across drivers), so a
  content mismatch surfaces immediately rather than passing silently.

## Earlier target-side gaps are resolved in current builds

Earlier HeliosDB builds had a handful of PG-wire compatibility gaps that affected
specific migration paths (for example bytea round-tripping, numeric wire typing,
`ON CONFLICT … DO UPDATE`, and quoted-identifier handling). **These are resolved
in the current HeliosDB releases listed above.** a2h pins the minimum version per
edition (and gates CDC apply to Nano ≥ 3.58.5) so that, on a supported build, the
full workflow — migrate, validate, and (on Lite/Full) CDC — runs cleanly. On an
older build, a2h still degrades gracefully via the capability probe and reports
any work-around it had to apply.

## Validated compatibility

a2h's `migrate` + `test-count` / `test-data` + CDC suites exercise the following
PG-wire behaviours against current HeliosDB builds on every run, so a regression
in any of them is caught immediately:

- **Sequences** — `CREATE SEQUENCE` and `nextval` / `currval` /
  `setval(seq, value, is_called)`; an Oracle or MySQL sequence migrates with its
  current value preserved.
- **Aggregates over composite-primary-key tables** — `count(*)`, `count(1)`, and
  `sum(<const>)` return exact results, so row-count validation is trustworthy.
- **Numeric typing** — `NUMERIC` / `DECIMAL` columns report the numeric type on
  the wire, so decimal values compare and canonicalize correctly.
- **Fixed-length text** — `CHAR(n)` / `CHARACTER(n)` columns are created and
  loaded (blank-padded to the declared length); `CHARACTER VARYING(n)` →
  `VARCHAR`, and the CLOB forms → `TEXT`.
- **Binary** — `BYTEA` round-trips byte-for-byte, including embedded `NUL` bytes
  and the escaped (`E'…'`) literal form psycopg uses for parameterized binary
  upserts.
- **Extended query protocol** — psycopg3 prepared / parameterized statements run
  without protocol desync.
- **Upserts for CDC apply** — `INSERT … ON CONFLICT … DO UPDATE` resolving
  `EXCLUDED.<col>`.

If a target build is missing one of these, a2h degrades gracefully (see above)
and reports the work-around it applied rather than failing silently.

## Feature support per edition (via the `psycopg` driver)

| Capability | HeliosDB-Nano | HeliosDB-Lite | HeliosDB-Full | Stock PostgreSQL |
|---|---|---|---|---|
| Schema DDL (tables, indexes, FKs, views, sequences¹) | ✅ | ✅ | ✅ | ✅ |
| Bulk load | ✅ INSERT (no COPY) | ✅ COPY | ✅ COPY | ✅ COPY |
| Parallel + resumable load | ✅ | ✅ (serial-retry converges) | ✅ | ✅ |
| Validation (`test` / `test-count` / `test-data`) | ✅ | ✅ | ✅ | ✅ |
| CDC apply (`replicat`) | ✅ (≥ 3.58.5) | ✅ | ✅ | ✅ |

¹ Sequences are emitted as standard PostgreSQL DDL. Stock PostgreSQL and
sequence-supporting HeliosDB builds create them natively; on a build that does
not yet implement `CREATE SEQUENCE`/`nextval`, a2h emits the DDL and degrades to
a warning — table data still migrates.

## See also

- [Troubleshooting](troubleshooting.md) — common errors and the per-edition
  minimum builds.
- [Compatibility matrix](../README.md#compatibility-matrix) — source dialect ×
  target edition validation status.
- Per-target guides:
  [Oracle → Lite](migration/oracle-to-heliosdb-lite.md) ·
  [→ Full](migration/oracle-to-heliosdb-full.md) ·
  [→ Nano](migration/oracle-to-heliosdb-nano.md) ·
  [→ PostgreSQL](migration/to-postgresql.md).
