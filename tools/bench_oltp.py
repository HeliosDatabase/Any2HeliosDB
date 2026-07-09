#!/usr/bin/env python3
"""OLTP micro-benchmark + baseline comparison (regression gate).

Designed to be a fair regression gate for the HeliosDB planner / per-statement
privilege-check fixes, and to run on *any* edition including a pre-fix Full
(which rejects parameterized binary args). Therefore it:

* uses **literal SQL** (no bind parameters) — universal and still exercises the
  full parse → plan → privilege → execute path the fixes touch;
* is **SELECT-dominant** — point lookups are the metric most sensitive to the
  planner and privilege hot paths, and are fast (single-row write commit is a
  separate, slow storage path on these builds and is not what the fixes touch);
* wraps the populate and UPDATE phases in **one transaction** each (so the slow
  per-commit write path doesn't dominate);
* reports throughput (ops/sec) and p50/p95 latency, median over N reps.

Run:
    python tools/bench_oltp.py --port 26442 --label fixed --json out.json
Compare (exit non-zero on regression):
    python tools/bench_oltp.py --compare baseline.json fixed.json [--threshold 0.97]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from typing import Dict, List

BENCH_TABLE = "bench_t"
POPULATE_CHUNK = 100


def _pct(durations: List[float], p: float) -> float:
    if not durations:
        return 0.0
    s = sorted(durations)
    return s[min(len(s) - 1, int(p / 100.0 * len(s)))] * 1000.0  # ms


def _phase(durations: List[float]) -> Dict[str, float]:
    total = sum(durations)
    n = len(durations)
    return {
        "ops_per_sec": round(n / total, 1) if total > 0 else 0.0,
        "p50_ms": round(_pct(durations, 50), 3),
        "p95_ms": round(_pct(durations, 95), 3),
    }


def _select_phase(cur, selects: int, rows: int) -> List[float]:
    sel: List[float] = []
    for k in range(selects):
        i = (k * 2654435761) % rows
        t = time.perf_counter()
        cur.execute("SELECT v FROM {} WHERE id = {}".format(BENCH_TABLE, i))
        cur.fetchall()
        sel.append(time.perf_counter() - t)
    return sel


def benchmark(args) -> Dict:
    import psycopg

    conn = psycopg.connect(host=args.host, port=args.port, user=args.user,
                           password=args.password or None, dbname=args.dbname,
                           autocommit=True, connect_timeout=10)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS {}".format(BENCH_TABLE))
    cur.execute("CREATE TABLE {} (id int PRIMARY KEY, v int, s text)".format(BENCH_TABLE))

    # One-time populate via multi-row INSERTs (the write path is ~300ms/row on
    # these dev builds, so we keep `rows` small; this is setup, not the gate).
    conn.autocommit = False
    t0 = time.perf_counter()
    for start in range(0, args.rows, POPULATE_CHUNK):
        vals = ",".join("({0},{1},'r{0}')".format(i, i * 2)
                        for i in range(start, min(start + POPULATE_CHUNK, args.rows)))
        cur.execute("INSERT INTO {} (id, v, s) VALUES {}".format(BENCH_TABLE, vals))
    conn.commit()
    insert_secs = time.perf_counter() - t0
    conn.autocommit = True

    _select_phase(cur, min(1000, args.selects), args.rows)  # warmup
    sel_rates: List[Dict[str, float]] = [_phase(_select_phase(cur, args.selects, args.rows))
                                         for _ in range(args.reps)]
    cur.execute("DROP TABLE IF EXISTS {}".format(BENCH_TABLE))
    cur.close()
    conn.close()

    select_agg = {
        "ops_per_sec": round(statistics.median(r["ops_per_sec"] for r in sel_rates), 1),
        "p50_ms": round(statistics.median(r["p50_ms"] for r in sel_rates), 3),
        "p95_ms": round(statistics.median(r["p95_ms"] for r in sel_rates), 3),
    }
    return {"label": args.label, "reps": args.reps, "rows": args.rows, "selects": args.selects,
            "updates": args.updates,
            "phases": {
                "insert_bulk": {"ops_per_sec": round(args.rows / insert_secs, 1) if insert_secs else 0.0,
                                "p50_ms": 0.0, "p95_ms": 0.0},
                "select": select_agg,
            }}


def print_report(res: Dict) -> None:
    print("Benchmark: {} (reps={}, rows={}, selects={}, updates={})".format(
        res.get("label", "?"), res["reps"], res["rows"], res["selects"], res.get("updates", 0)))
    print("  {:12} {:>14} {:>10} {:>10}".format("phase", "ops/sec", "p50 ms", "p95 ms"))
    for phase, m in res["phases"].items():
        print("  {:12} {:>14} {:>10} {:>10}".format(phase, m["ops_per_sec"], m["p50_ms"], m["p95_ms"]))


def compare(baseline_path: str, current_path: str, threshold: float) -> int:
    with open(baseline_path) as f:
        base = json.load(f)
    with open(current_path) as f:
        cur = json.load(f)
    print("Comparison: baseline='{}' vs current='{}'  (regression threshold {:.0%})".format(
        base.get("label", baseline_path), cur.get("label", current_path), threshold))
    print("  {:12} {:>12} {:>12} {:>9}  {}".format("phase", "base ops/s", "cur ops/s", "ratio", "verdict"))
    regressed = False
    for phase in base["phases"]:
        if phase not in cur["phases"]:
            continue
        b = base["phases"][phase]["ops_per_sec"]
        c = cur["phases"][phase]["ops_per_sec"]
        ratio = (c / b) if b else 1.0
        ok = ratio >= threshold
        regressed = regressed or (not ok)
        print("  {:12} {:>12} {:>12} {:>8.2f}x  {}".format(phase, b, c, ratio, "OK" if ok else "REGRESSION"))
    print("RESULT:", "REGRESSION DETECTED" if regressed else "no regression")
    return 1 if regressed else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=26442)
    ap.add_argument("--user", default="postgres")
    ap.add_argument("--password", default="")
    ap.add_argument("--dbname", default="postgres")
    ap.add_argument("--rows", type=int, default=80)
    ap.add_argument("--selects", type=int, default=6000)
    ap.add_argument("--updates", type=int, default=0)  # write path too slow on dev builds; gate is SELECT
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--label", default="run")
    ap.add_argument("--json", default="")
    ap.add_argument("--compare", nargs=2, metavar=("BASELINE", "CURRENT"))
    ap.add_argument("--threshold", type=float, default=0.97)
    args = ap.parse_args()

    if args.compare:
        return compare(args.compare[0], args.compare[1], args.threshold)
    res = benchmark(args)
    print_report(res)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(res, f, indent=2)
        print("wrote", args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
