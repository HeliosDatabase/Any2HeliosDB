#!/usr/bin/env python3
"""Probe the Any2HeliosDB native Oracle-wire target path."""
from __future__ import annotations

import argparse
import os
import socket
import sys
import traceback
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _classify_failure(exc: BaseException) -> str:
    text = "{}: {}".format(exc.__class__.__name__, exc).lower()
    cause = getattr(exc, "__cause__", None)
    if cause is not None:
        text += " | cause={}: {}".format(cause.__class__.__name__, cause).lower()
    if "connection refused" in text or "timed out" in text or "name or service" in text:
        return "tcp-connect"
    if "server version" in text or "not supported" in text or "dpy-3010" in text:
        return "tns-accept-vsnnum"
    if "packet" in text or "checksum" in text or "invalid data" in text:
        return "tns-packet-framing"
    if "auth" in text or "password" in text or "ora-01017" in text:
        return "ttc-authentication"
    return "oracledb-thin-handshake"


def _print_stage(stage: str, status: str, detail: Optional[str] = None) -> None:
    line = "stage={} status={}".format(stage, status)
    if detail:
        line += " {}".format(detail)
    print(line, flush=True)


def _tcp_probe(host: str, port: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        return


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe oracledb thin -> Any2HeliosDB NativeOracleDriver connect path."
    )
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--service", required=True)
    parser.add_argument("--user", default=os.environ.get("A2H_NATIVE_TNS_USER", "postgres"))
    parser.add_argument("--password", default=os.environ.get("A2H_NATIVE_TNS_PASSWORD"))
    parser.add_argument("--timeout", default=10, type=int)
    parser.add_argument("--traceback", action="store_true")
    args = parser.parse_args(argv)

    try:
        import oracledb  # noqa: PLC0415

        _print_stage(
            "import-oracledb",
            "ok",
            "version={} thin={}".format(
                getattr(oracledb, "__version__", "unknown"), oracledb.is_thin_mode()
            ),
        )
    except Exception as exc:  # noqa: BLE001
        _print_stage("import-oracledb", "failed", repr(exc))
        if args.traceback:
            traceback.print_exc()
        return 2

    try:
        _tcp_probe(args.host, args.port, args.timeout)
        _print_stage("tcp-connect", "ok", "{}:{}".format(args.host, args.port))
    except Exception as exc:  # noqa: BLE001
        _print_stage("tcp-connect", "failed", repr(exc))
        if args.traceback:
            traceback.print_exc()
        return 2

    try:
        from any2heliosdb.target.base import TargetDsn  # noqa: PLC0415
        from any2heliosdb.target.native_driver import NativeOracleDriver  # noqa: PLC0415

        dsn = TargetDsn(
            host=args.host,
            port=args.port,
            dbname=args.service,
            user=args.user,
            password=args.password,
            connect_timeout=args.timeout,
        )
        driver = NativeOracleDriver(dsn)
        _print_stage("native-driver-init", "ok")
    except Exception as exc:  # noqa: BLE001
        _print_stage("native-driver-init", "failed", repr(exc))
        if args.traceback:
            traceback.print_exc()
        return 2

    try:
        driver.connect()
        _print_stage("native-driver-connect", "ok")
    except Exception as exc:  # noqa: BLE001
        failure_stage = _classify_failure(exc)
        _print_stage("native-driver-connect", "failed", repr(exc))
        _print_stage("failure-stage", failure_stage)
        if args.traceback:
            traceback.print_exc()
        return 1

    try:
        banner = driver.server_banner()
        _print_stage("server-banner", "ok", repr(banner))
    except Exception as exc:  # noqa: BLE001
        failure_stage = _classify_failure(exc)
        _print_stage("server-banner", "failed", repr(exc))
        _print_stage("failure-stage", failure_stage)
        if args.traceback:
            traceback.print_exc()
        return 1
    finally:
        driver.close()

    _print_stage("probe", "success")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
