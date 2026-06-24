#!/usr/bin/env python3
"""Benchmark one HeliosDB binary's SELECT throughput (the fix regression gate).

Handles both editions' start flags and prints (and optionally json-dumps) the
median point-SELECT rate. Run it per binary (baseline + each fix snapshot) and
compare the rates.

    python tools/bench_server.py --binary /tmp/full-baseline --edition full \
        --port 26461 --label baseline --password postgres --json /tmp/full-baseline.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time


def start_cmd(binary: str, edition: str, port: int) -> list:
    http = port + 1000
    if edition == "lite":
        return [binary, "start", "--memory", "-p", str(port), "--http-port", str(http), "--auth", "trust"]
    # full: needs a data dir; trust via --auth-mode
    dd = tempfile.mkdtemp(prefix="full-bench-")
    return [binary, "--data-dir", dd, "--postgres-port", str(port),
            "--http-port", str(http), "--auth-mode", "trust"]


def wait_up(port: int, password, timeout: int = 60) -> bool:
    import psycopg

    for _ in range(timeout):
        try:
            psycopg.connect(host="127.0.0.1", port=port, user="postgres",
                            password=password or None, dbname="postgres", connect_timeout=1).close()
            return True
        except Exception:
            time.sleep(1)
    return False


def bench_select(port: int, password, rows: int, selects: int, reps: int) -> float:
    import psycopg

    c = psycopg.connect(host="127.0.0.1", port=port, user="postgres",
                        password=password or None, dbname="postgres", autocommit=True)
    cur = c.cursor()
    cur.execute("DROP TABLE IF EXISTS b")
    cur.execute("CREATE TABLE b (id int PRIMARY KEY, v int, s text)")
    cur.execute("INSERT INTO b (id,v,s) VALUES " +
                ",".join("(%d,%d,'r%d')" % (i, i * 2, i) for i in range(rows)))
    rates = []
    for _ in range(reps):
        t = time.perf_counter()
        for k in range(selects):
            cur.execute("SELECT v FROM b WHERE id = %d" % (k % rows))
            cur.fetchall()
        rates.append(selects / (time.perf_counter() - t))
    cur.execute("DROP TABLE IF EXISTS b")
    c.close()
    rates.sort()
    return rates[len(rates) // 2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True)
    ap.add_argument("--edition", choices=["lite", "full"], required=True)
    ap.add_argument("--port", type=int, default=26461)
    ap.add_argument("--label", default="run")
    ap.add_argument("--password", default="")
    ap.add_argument("--rows", type=int, default=10)
    ap.add_argument("--selects", type=int, default=5000)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    os.environ.setdefault("HELIOSDB_JWT_SECRET", "a2h-bench-secret")
    proc = subprocess.Popen(start_cmd(args.binary, args.edition, args.port),
                            stdout=open("/tmp/benchsrv-%s.log" % args.label, "w"),
                            stderr=subprocess.STDOUT)
    try:
        if not wait_up(args.port, args.password):
            print("ERROR: %s server did not come up (see /tmp/benchsrv-%s.log)" % (args.label, args.label))
            return 2
        rate = bench_select(args.port, args.password, args.rows, args.selects, args.reps)
        print("%-14s SELECT: %.0f sel/s" % (args.label, rate))
        if args.json:
            with open(args.json, "w") as f:
                json.dump({"label": args.label, "select_per_sec": round(rate, 1)}, f)
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
