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

## Roadmap — v2.0.0: PL/SQL → PL/pgSQL auto-translation (Option B)

v2.0.0 will **auto-translate** the objects that v1.0.0 surfaces for review —
turning PL/SQL procedures/functions/packages and trigger bodies into PL/pgSQL,
and (where the target supports it) emitting `CREATE MATERIALIZED VIEW` and native
partitioning directly. The design intent:

- **Procedures / functions** → PL/pgSQL function bodies (control flow, `%TYPE`,
  cursors, exceptions, `RETURN`), with anything genuinely non-portable still
  routed to the review file + gap report.
- **Packages** → a schema (namespace) of PL/pgSQL functions, package state →
  session-local GUCs / temp structures.
- **Triggers** → a PL/pgSQL trigger function + `CREATE TRIGGER`.
- **Materialized views / partitioning** → native target DDL when the edition
  supports it; gap + review otherwise.

This is deliberately **out of scope for v1.0.0**: auto-translating arbitrary
PL/SQL is a large, correctness-sensitive surface, and the thin-tool bar (migrate
the data tier perfectly, surface the rest for review) is the safe, shippable
1.0. The v1.0.0 introspection already captures every procedural object's
verbatim source, so v2.0.0 builds on that foundation rather than re-reading it.
