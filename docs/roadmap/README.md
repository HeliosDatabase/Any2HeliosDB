# Roadmap

Forward-looking plans for Any2HeliosDB. Shipped work lives in the top-level
[CHANGELOG.md](../../CHANGELOG.md); this directory holds what's *next*.

- **[v2.0.0 — AI-native procedural migration](v2.0.0.md)** — connect an LLM
  (**Ollama** or any **RESTful / OpenAI-compatible** endpoint) that analyzes each
  object + its dependencies, adapts it to the target dialect, tests it in an
  **ephemeral metadata-only sandbox**, and migrates working code; objects the model
  can't get working fall back to a pinpointed refactoring-effort estimate. (A
  hand-written deterministic PL/SQL→PL/pgSQL transpiler is treated as **obsolete in
  the AI era**.)

> Current release: **v1.0.0** — Oracle / PostgreSQL → HeliosDB-Nano + stock
> PostgreSQL, data tier fully automated; procedural/advanced objects surfaced for
> review (see [reference/oracle-object-support.md](../reference/oracle-object-support.md)).
