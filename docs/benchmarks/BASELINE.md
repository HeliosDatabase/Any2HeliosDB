# OLTP benchmark baselines (2026-07-09)

Session-start baselines for the CLAUDE.md quality-gate 3 regression comparison
(`tools/bench_oltp.py --compare docs/benchmarks/baseline-<edition>.json current.json
--threshold 0.97`). Always compare against THESE files, never against a previous
run, so the ≤3% degradation bound stays cumulative across a work session.

## Method

- Harness: `tools/bench_oltp.py` defaults — `rows=80`, `selects=6000`, `updates=0`,
  `reps=3` (median), literal SQL over psycopg v3, autocommit point-SELECTs after a
  1000-select warmup; populate is one multi-row-INSERT transaction (setup, not gate).
- One throwaway server per edition, started sequentially on otherwise-idle CPU,
  throwaway data dir on local xfs scratch, default server settings, trust auth,
  loopback TCP. All protocol/HTTP ports moved off their defaults (264xx range) so
  no live service is touched. Each run executed under the fleet build lock
  (`flock …/coordination/build.lock`) inside a bounded scope
  (`systemd-run --user --scope -p MemoryMax=24G -p MemorySwapMax=0`).
- Server start commands (reproduction): Nano/Lite `heliosdb-{nano,lite} start
  --data-dir <scratch> --port 264xx --http-port 264xx` (Lite additionally requires
  `HELIOSDB_JWT_SECRET` set — it refuses to start without it); Full
  `heliosdb-full --data-dir <scratch> --auth-mode trust --postgres-port 264xx
  --mysql-port … --http-port … --oracle-port … --sqlserver-port … --native-port …`
  (Full defaults to SCRAM and rejects passwordless connects without
  `--auth-mode trust`; override every protocol port off its default).
- The gate metric is **select ops/sec** (the parse→plan→execute hot path).
  `insert_bulk` is recorded for reference only: it is a single 80-row transaction,
  so its "ops/sec" is dominated by commit latency and is too noisy to gate on.

| Edition | Server build | select ops/s | p50 ms | p95 ms | insert_bulk rows/s | JSON |
|---|---|---|---|---|---|---|
| Nano | heliosdb-nano 4.0.0 (`~/HDB/Nano` release build, 2026-07-06) | 10,678.2 | 0.088 | 0.110 | 80,892 | `baseline-nano.json` |
| Lite | HeliosDB-Lite v3.6.0 (`~/HDB/Lite` release build, 2026-06-24) | 5,959.7 | 0.163 | 0.203 | 259.1 | `baseline-lite.json` |
| Full | heliosdb-full 8.3.0 (`~/HDB/Full` release build, 2026-07-06) | 6,171.9 | 0.158 | 0.184 | 19.0 | `baseline-full.json` |

## Historical context

`full-2026-06-23.md` / `lite-2026-06-23.md` (salvaged from since-pruned agent
worktrees) record the 2026-06-23 per-fix A/B gates; their absolute numbers
(e.g. Full ≈ 9.0–9.6k sel/s via psycopg2 on different binaries/dataset sizes) are
NOT comparable to this table — different driver, protocol, and machine load.
Use only the 2026-07-09 JSON files for gate comparisons.
