# Any2HeliosDB documentation

`a2h` migrates **Oracle, MySQL, PostgreSQL, and SQL Server** into HeliosDB —
**Nano**, **Lite**, or **Full** — or into **stock PostgreSQL**, over the
PostgreSQL wire protocol, with a setup wizard, a parallel + resumable load,
validation, a GoldenGate-style CDC engine, and an MCP server.

Start with the [project README](../README.md) for the tagline, install, and the
[compatibility matrix](../README.md#compatibility-matrix).

## Guides

- **[Getting started](guides/getting-started.md)** — install, prerequisites,
  `a2h doctor`, the wizard + smoke test, and the end-to-end workflow
  (`assess → migrate → status/resume → test → CDC`) with real transcripts.
- **[Configuration](guides/configuration.md)** — the complete `config.toml`
  reference, env-var password handling, driver selection (`psycopg` vs `native`),
  and tuning (`parallelism`, `prefer_copy`, `preserve_case`, `output_dir`).
- **[Worked examples](guides/examples.md)** — copy-pasteable end-to-end scenarios:
  Oracle/MySQL/SQL Server → HeliosDB, HeliosDB → MySQL migrate-back, CDC
  (SCN-watermark + MySQL binlog), interrupted-load resume, and type overrides.

## Migration guides (per validated target)

- **[Oracle → HeliosDB-Lite](migration/oracle-to-heliosdb-lite.md)**
- **[Oracle → HeliosDB-Full](migration/oracle-to-heliosdb-full.md)**
- **[Oracle → HeliosDB-Nano](migration/oracle-to-heliosdb-nano.md)**
- **[MySQL & SQL Server](migration/mysql-and-mssql.md)** — both validated
  end-to-end (MySQL on all editions; SQL Server 2022 → Full).
- **[→ PostgreSQL](migration/to-postgresql.md)** — a2h as an Oracle/MySQL/
  SQL-Server → **stock PostgreSQL** migrator (same `psycopg` driver).

## Reference

- **[CLI reference](reference/cli.md)** — every `a2h` command + options, defaults,
  exit codes, the global config/password/`output_dir` model, and the supported
  source-dialect × target-driver matrix.
- **[Type mapping](reference/type-mapping.md)** — the full Oracle→HeliosDB type
  table with provenance and overrides.
- **[CDC](cdc.md)** — the Extract → trail → Replicat model, the `extract` /
  `replicat` / `extracts` verbs, watermark/cursor semantics, idempotency, v1
  limits, and the v2 roadmap.
- **[MCP server](mcp.md)** — expose the toolkit as MCP tools (Bearer-token auth +
  RBAC) so an AI agent can drive a migration remotely.
- **[Troubleshooting](troubleshooting.md)** — common issues and the per-edition
  HeliosDB minimum builds.
- **[HeliosDB compatibility](heliosdb-compatibility.md)** — supported editions and
  minimum versions, the runtime capability probe, and graceful degradation.
- **[Oracle object support](reference/oracle-object-support.md)** — what migrates
  automatically vs. is surfaced for review (routines, triggers, materialized
  views, partitioning), and the v2.0.0 auto-translation roadmap.

## Project

- **[Development history](development-history.md)** — the engineering journey:
  the phases (Foundations → v1.1.0), milestones, and the design decisions that paid
  off, distilled from ~120 commits.
- **[Demo casts](demos/)** — recorded asciinema migrations (Pagila & Oracle →
  HeliosDB-Nano, one-shot migrate + zero-downtime CDC).

## Roadmap

- **[Roadmap](roadmap/README.md)** — what's next, including
  **[v2.0.0 — AI-native procedural migration](roadmap/v2.0.0.md)**: connect an LLM
  (Ollama / RESTful) that analyzes each object + its dependencies, adapts it to the
  target dialect, tests it in an ephemeral metadata-only sandbox, and migrates
  working code (with a refactoring-effort estimate as the fallback). A deterministic
  PL/SQL→PL/pgSQL transpiler is treated as obsolete in the AI era.
