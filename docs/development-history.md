# Any2HeliosDB — development history

How `a2h` went from an empty directory to a battle-tested, PyPI-published migration
tool. This is a curated narrative of the engineering journey — the phases, the
milestones, and the design decisions that paid off — distilled from ~120 commits of
development. It complements the [CHANGELOG](../CHANGELOG.md) (what shipped) with the
*how and why* (how it was built).

> Timeline: first commit **2026‑06‑22**; first stable **v1.0.0 on 2026‑06‑26**;
> **v1.1.0 on 2026‑06‑28**. A deliberately milestone-driven build, validated against
> real databases at every step.

---

## The arc at a glance

| Phase | Theme | Outcome |
|---|---|---|
| **0 — Foundations** | Architecture before code | Canonical IR, pluggable target drivers, runtime capability probe |
| **1 — Oracle, end to end** | One source, fully working | Oracle → HeliosDB migrate + validate, resumable |
| **2 — Data engine** | Move data at scale, safely | Chunked, parallel, crash-resumable load with a durable manifest |
| **3 — Wizard + native path** | Make it usable + thin | Interactive wizard, smoke test, native (dialect-matched) driver |
| **4 — CDC spine** | Zero-downtime story | Extract → trail → idempotent replicat (incl. deletes) |
| **5 — Heterogeneous** | Beyond one source/target | Oracle, MySQL, SQL Server, PostgreSQL — all green |
| **6 — Hardening + ops** | Make it trustworthy | Adversarial review, MCP server, live monitor dashboard |
| **7 — Release line** | Ship + iterate | v0.9.0 → v0.9.5 on PyPI |
| **8 — v1.0.0** | First stable | Procedural-object visibility, 3-round adversarial pass, scale-tested |
| **9 — v1.1.0** | Log-based CDC + concurrency | PostgreSQL logical-decoding CDC, parallel-on-Nano |

---

## Phase 0 — Foundations (day 1)

The project started with **architecture, not features**. The first commits laid down
the pieces everything else would hang off:

- a **canonical intermediate representation (IR)** for schemas — so every source
  dialect and every target speak through one model;
- a **`TargetDriver` abstraction** with two implementations from the outset
  (portable PG-wire and dialect-matched native);
- a **runtime capability probe** — the single most important design decision: rather
  than assume what a target supports from its version string, a2h *asks the live
  server* and adapts. This is what later let one `migrate` command work across every
  HeliosDB edition and stock PostgreSQL without edition flags;
- a data-driven **type-mapping registry** and a **COPY codec** with correct
  `\N`-vs-empty-string semantics — with unit tests on day one.

## Phase 1 — Oracle, end to end (day 1)

Depth before breadth: one source taken **all the way** before adding others. The
Oracle source adapter, DDL emitters, orchestrator, and a resume manifest landed
together, producing a **working end-to-end Oracle → HeliosDB migration over the COPY
fast path** on the first day.

## Phase 2 — The data engine (day 2)

The core differentiator over a naive port: a **chunked, parallel, crash-resumable
loader** backed by a durable SQLite-WAL manifest. Each chunk is idempotent
(range-delete + load in one transaction), so a kill mid-load resumes with **no
duplicates and no re-loading of completed work**. Alongside it came the validation
suite — **TEST / TEST_COUNT / TEST_DATA** (object, row-count, and checksum parity) —
and the assessment/PL-SQL cost modules, so a migration could be *planned, executed,
and proven* end to end.

## Phase 3 — Wizard + the native path (day 2)

Two usability investments:

- an **interactive wizard with a smoke test** — replacing hand-edited config with a
  guided setup that connects both ends, probes capabilities, and proves a tiny COPY
  round-trip before you commit to a real run;
- the **native driver** — connecting to HeliosDB over the *same wire protocol as the
  source*, so the database itself absorbs the dialect and the tool transforms almost
  nothing. This is the purest expression of the project's governing principle (below).

## Phase 4 — The CDC spine (day 2)

A symmetric **Extract → trail → Replicat** engine, designed so capture and apply
advance on independent durable cursors. The apply path is idempotent (upsert via
`ON CONFLICT`, FK-safe) and order-preserving, and delete handling arrived quickly —
first via key-set **reconciliation**, then via real log-based capture. This spine is
what later made zero-downtime migrations possible.

## Phase 5 — Heterogeneous sources and targets (day 2)

Breadth, on the interfaces built in phases 0–4: **MySQL** and **SQL Server** source
adapters (Oracle-parity), a **PostgreSQL-wire source**, and **MySQL / PostgreSQL
target dialects** for migrate-back and heterogeneous routing. The orchestrator
generalized to dialect dispatch, and the compatibility matrix flipped to **all four
sources green** — Oracle, MySQL, SQL Server, PostgreSQL.

## Phase 6 — Hardening + operability (days 2–3)

What turns a working tool into a trustworthy one:

- **adversarial review passes** that found and fixed real edge cases (identifier
  quoting unified across DDL/load/validate, partition-child handling, sequence start
  values, timestamp/tz fidelity, chunk-bound math);
- a **silent-data-loss fix** in the resumable loader (manifest chunk-state reset on
  `drop_existing`) — exactly the class of bug rigorous battle-testing exists to catch;
- an **MCP server** (Bearer auth + RBAC) so AI agents can drive a2h remotely;
- a **live full-screen monitor** showing per-table progress, volume-left, and ETA.

## Phase 7 — The public release line (days 3–5)

`v0.9.0` through `v0.9.5` on PyPI, each a focused increment: **stock PostgreSQL as a
first-class target**, source-dialect **view translation** on the PG-wire path,
**`a2h test-index`** (FK-index sanity), native-driver resilience, and
**PostgreSQL-source sequences**. CI moved to **Trusted Publishing (OIDC, no tokens)**.

## Phase 8 — v1.0.0, the first stable release (day 5)

The bar for "stable" was set high: **Oracle procedural-object visibility** (routines,
triggers, materialized views, partitions surfaced for assessment + review),
**scale validation** (a 1M-row chunked parallel load with a mid-load kill and clean
resume — no duplicates), and a **three-round adversarial review at maximum rigor**
whose findings were all fixed and re-validated before tagging. Result:
`pip install any2heliosdb` with clean tag = main = wheel provenance.

## Phase 9 — v1.1.0 (day 7)

The latest release added **PostgreSQL logical-decoding CDC** (true log-based capture
with native deletes → zero-downtime PostgreSQL → HeliosDB), **parallel load on
HeliosDB-Nano** once the engine supported concurrent writes, loader and delete
robustness fixes, and a set of **asciinema demo casts** (`docs/demos/`). Re-validated
end to end against the current HeliosDB-Nano build.

---

## Engineering themes that paid off

- **Probe, don't assume.** The capability matrix — established in phase 0 — meant a
  single code path adapted to each target at runtime (COPY vs INSERT, concurrent
  writes vs serial, sequence support, etc.) instead of branching on version strings.
- **"Fix at the database, keep the tool thin."** The governing principle throughout:
  when a migration hit an incompatibility, the preference was to fix or extend
  HeliosDB and keep a2h's translation layer minimal — and to emit a structured
  target-gap report so fixes converged at the source. The toolchain and the database
  **co-evolved and hardened together**.
- **Battle-testing as a first-class activity.** Every integration ran against real
  databases, and the suite was written to catch silent failures (parity checksums,
  fail-loud-on-partial-load, crash-resume assertions). Repeatedly, this surfaced
  subtle correctness issues *before* a release rather than after.
- **Idempotency everywhere.** Chunk loads, CDC apply, and re-runs were all designed to
  be safely repeatable — the foundation of "resume after a crash" and "re-apply a
  trail" working correctly.
- **Depth, then breadth.** Oracle was taken fully end-to-end before MySQL/MSSQL/PG
  were added on the same interfaces — so breadth cost little once the seams were right.

## By the numbers

- ~**120 commits** over 7 days (2026‑06‑22 → 2026‑06‑28).
- **4 source dialects** (Oracle, MySQL, SQL Server, PostgreSQL) and **4 targets**
  (HeliosDB Nano/Lite/Full + stock PostgreSQL).
- **2 target drivers** (portable PG-wire + dialect-matched native).
- A full validation suite and a CDC engine, from scaffold to **v1.1.0 on PyPI**.

---

*The raw pre-public commit history (the 86-commit `dev-history` branch) is preserved
locally in the project bundle; this document is the curated, product-facing account of
that journey.*
