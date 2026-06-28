# a2h demo casts

Recorded [asciinema](https://asciinema.org) sessions of real `a2h` migrations into
HeliosDB-Nano — schema+data and zero-downtime CDC, for both a PostgreSQL source
(Pagila) and an Oracle source (HR). Every command in every cast is a real command
against real databases; nothing is faked. Host/user are masked (`user01@host001`).

## The casts

| Cast | File | Source → target | Shows | Length |
|---|---|---|---|---|
| **A** | `pagila-nano-migrate.cast` | Pagila (PostgreSQL) → Nano | one-shot schema+data migrate, then `test-count` / `test-data` | ~30s |
| **B** | `pagila-nano-cdc.cast` | Pagila (PostgreSQL) → Nano | **zero-downtime CDC** — initial load while the source stays live, then INSERT/UPDATE/**DELETE** captured via PostgreSQL logical decoding and applied to Nano | ~59s |
| **C** | `oracle-nano-migrate.cast` | Oracle HR → Nano | one-shot migrate + `test-count` / `test-index`, Unicode/decimal/FK peek | ~33s |
| **D** | `oracle-nano-cdc.cast` | Oracle HR → Nano | **zero-downtime CDC** — SCN-watermark capture for INSERT/UPDATE, DELETE via reconcile | ~65s |

## Play

```bash
asciinema play docs/demos/pagila-nano-migrate.cast      # A
asciinema play docs/demos/pagila-nano-cdc.cast          # B
asciinema play docs/demos/oracle-nano-migrate.cast      # C
asciinema play docs/demos/oracle-nano-cdc.cast          # D

asciinema play -s 2 <file>                              # 2x speed
asciinema cat  <file> | sed 's/\x1b\[[0-9;]*m//g'       # plain-text transcript
```

Share/embed: `asciinema upload <file>` (asciinema.org), or render a GIF with
[`agg`](https://github.com/asciinema/agg): `agg <file> out.gif`.

## What the CDC casts demonstrate

- **B (PostgreSQL source)** — true **log-based** CDC via logical decoding
  (`test_decoding`): the trail carries real INSERT/UPDATE/**DELETE** records, so the
  delete replicates natively (no reconcile pass). Implemented in
  [`cdc/sources/postgres_logical.py`](../../src/any2heliosdb/cdc/sources/postgres_logical.py).
- **D (Oracle source)** — **SCN-watermark** CDC: inserts/updates are captured by
  `ORA_ROWSCN`; deletes (which a watermark scan can't see) are reconciled via a
  key-set diff on `replicat`. The cast shows both halves explicitly.

Both keep the source serving writes the whole time — the target is brought into
sync and you cut over with zero downtime.

## Reproducing

`scripts/pagila/` and `scripts/oracle/` hold the exact driver + recorder scripts and
configs used. They are **reference** material: they were recorded against this
project's local test harness and hard-code its endpoints — a disposable
HeliosDB-Nano (the `heliosdb-nano` binary), a Pagila/Oracle source in local
containers, and `/tmp` working dirs. Each `record-*.sh` resets to a clean state
(fresh empty Nano; the Pagila CDC one also recreates an isolated `pagila_cdc_demo`
source DB) and then drives `record` → `demo` under asciinema. The configs pass DB
passwords by env-var reference only (`password_env`); no secrets are committed.

To adapt them, point the configs at your own source + a HeliosDB-Nano ≥ 3.60.4, set
the password env vars, and run the relevant `demo-*.sh` directly (no recording) or a
`record-*.sh` to produce a fresh `.cast`.
