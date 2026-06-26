# Oracle object support — what a2h migrates vs. surfaces for review

a2h follows a **thin-tool** principle: the **data tier is fully automated**, and
**procedural / advanced objects are surfaced** (counted in `a2h assess`,
flagged as gaps, and written to a `.review.sql` companion by `a2h export`) for
**manual porting** rather than silently dropped or risk-translated.

## v1.0.0 support matrix (Oracle → PostgreSQL / HeliosDB-Nano)

| Object | v1.0.0 behaviour |
|---|---|
| Tables, columns, data types | **Auto-migrated** |
| Primary keys, foreign keys, CHECK constraints | **Auto-migrated** |
| Indexes | **Auto-migrated** |
| Sequences (incl. resume point) | **Auto-migrated** |
| Views (with NVL/DECODE/SYSDATE rewrites) | **Auto-migrated** (unsupported expressions → gap) |
| Row **data** (chunked, parallel, resumable) | **Auto-migrated** |
| **Partitioned tables** | Data migrates into a **single (flat) table**; the partitioning scheme is **gap-reported** to recreate manually |
| **Stored procedures / functions / packages** | **Surfaced**: counted + gap-reported + emitted to `<schema>.review.sql` for manual PL/pgSQL porting |
| **Triggers** | **Surfaced** (as above) |
| **Materialized views** | **Surfaced**: defining query emitted to `.review.sql`; recreate with `CREATE MATERIALIZED VIEW` + a refresh strategy |

`a2h assess` reports the counts, a per-object **gap list** (each `DEGRADED` — the
data migration still succeeds), and a person-day cost estimate. `a2h export`
writes the target DDL to `schema.sql` **and** the procedural source verbatim to
`schema.review.sql`.

> A `DEGRADED` gap means: the migration proceeds and the data lands correctly,
> but that object needs a human to port it. It never blocks the run.

## Roadmap

Procedural-object **auto-translation** (PL/SQL → PL/pgSQL) and **AI-assisted
live-rewrite** of objects that can't be translated mechanically are the v2.0.0
goals — see **[docs/roadmap/v2.0.0.md](../roadmap/v2.0.0.md)**. v1.0.0's
introspection already captures every procedural object's verbatim source, so
v2.0.0 builds on that foundation rather than re-reading it.
