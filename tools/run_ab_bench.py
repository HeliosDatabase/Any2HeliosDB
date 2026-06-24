#!/usr/bin/env python3
"""A/B benchmark runner: start each HeliosDB binary in-memory, benchmark it, stop
it, then compare baseline vs fixed and flag any regression.

    python tools/run_ab_bench.py <baseline_binary> <fixed_binary> [--start "start --memory"]

Defaults assume a Lite binary (`<bin> start --memory -p ...`). For Full, pass
--start "" since Full takes flags directly (`<bin> --postgres-port ...`).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                                  # bench_oltp
sys.path.insert(0, os.path.join(_HERE, "..", "src"))       # any2heliosdb (unused here)
import bench_oltp  # noqa: E402

PORT = 26442
HTTP = 26092
ROWS, SELECTS, UPDATES, REPS = 20, 6000, 0, 3
os.environ.setdefault("HELIOSDB_JWT_SECRET", "a2h-bench-secret")


def wait_up(port: int, timeout: int = 45) -> bool:
    import psycopg

    for _ in range(timeout):
        try:
            psycopg.connect(host="127.0.0.1", port=port, user="postgres",
                            dbname="postgres", connect_timeout=1).close()
            return True
        except Exception:
            time.sleep(1)
    return False


def server_cmd(binpath: str, start_mode: str) -> list:
    if start_mode.strip():
        # Lite-style: `<bin> start --memory -p PORT --http-port HTTP --auth trust`
        return ([binpath] + start_mode.split() +
                ["-p", str(PORT), "--http-port", str(HTTP), "--auth", "trust"])
    # Full-style: `<bin> --postgres-port PORT --http-port HTTP --auth-mode trust`
    return [binpath, "--postgres-port", str(PORT), "--http-port", str(HTTP), "--auth-mode", "trust"]


def bench_binary(binpath: str, label: str, outjson: str, start_mode: str) -> dict:
    log = open("/tmp/bench-{}.log".format(label), "w")
    proc = subprocess.Popen(server_cmd(binpath, start_mode), stdout=log, stderr=subprocess.STDOUT)
    try:
        if not wait_up(PORT):
            raise RuntimeError("server '{}' did not come up (see /tmp/bench-{}.log)".format(label, label))
        args = SimpleNamespace(host="127.0.0.1", port=PORT, user="postgres", password="",
                               dbname="postgres", rows=ROWS, selects=SELECTS, updates=UPDATES,
                               reps=REPS, label=label, json=outjson)
        res = bench_oltp.benchmark(args)
        bench_oltp.print_report(res)
        with open(outjson, "w") as f:
            json.dump(res, f, indent=2)
        return res
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()
        time.sleep(2)  # let the port free before the next server


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("baseline")
    ap.add_argument("fixed")
    ap.add_argument("--start", default="start --memory")
    ap.add_argument("--outdir", default=os.path.join(_HERE, "..", "docs", "benchmarks"))
    ap.add_argument("--threshold", type=float, default=0.97)
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)
    base_json = os.path.join(args.outdir, "baseline.json")
    fixed_json = os.path.join(args.outdir, "fixed.json")

    print("=== BASELINE ===")
    bench_binary(args.baseline, "baseline", base_json, args.start)
    print("\n=== FIXED ===")
    bench_binary(args.fixed, "fixed", fixed_json, args.start)
    print()
    return bench_oltp.compare(base_json, fixed_json, args.threshold)


if __name__ == "__main__":
    sys.exit(main())
