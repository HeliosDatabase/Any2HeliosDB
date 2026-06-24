"""Live native Oracle/TNS handshake probe.

Skipped by default because it requires a running HeliosDB Oracle listener.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Tuple

import pytest


def _parse_listener(value: str) -> Tuple[str, str, str]:
    target = value.strip()
    if target.startswith("//"):
        target = target[2:]
    host_port, sep, service = target.partition("/")
    if not sep or not service:
        raise ValueError("expected A2H_NATIVE_TNS as host:port/service")
    host, sep, port = host_port.rpartition(":")
    if not sep or not host or not port:
        raise ValueError("expected A2H_NATIVE_TNS as host:port/service")
    return host, port, service


@pytest.mark.skipif(
    not os.environ.get("A2H_NATIVE_TNS"),
    reason="set A2H_NATIVE_TNS=host:port/service to run live native TNS probe",
)
def test_native_oracle_tns_handshake_probe():
    host, port, service = _parse_listener(os.environ["A2H_NATIVE_TNS"])
    root = Path(__file__).resolve().parents[2]
    cmd = [
        sys.executable,
        str(root / "tools" / "native_connect_probe.py"),
        "--host",
        host,
        "--port",
        port,
        "--service",
        service,
    ]
    user = os.environ.get("A2H_NATIVE_TNS_USER")
    password = os.environ.get("A2H_NATIVE_TNS_PASSWORD")
    if user:
        cmd.extend(["--user", user])
    if password:
        cmd.extend(["--password", password])

    result = subprocess.run(
        cmd,
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    assert result.returncode == 0, result.stdout
