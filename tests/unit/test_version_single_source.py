"""Version single-sourcing (release engineering).

``__version__`` in the package ``__init__`` is the ONE source of truth; pyproject
declares ``dynamic = ["version"]`` and points hatch's version hook at that file, so
the wheel metadata and the runtime-reported version can never disagree. The CI
publish job additionally asserts the built version == the release tag (that guard
lives in .github/workflows/publish.yml and runs on a release event, not here).

These hermetic checks pin the local invariants: __version__ resolves, and
pyproject is configured for single-sourcing (no static [project] version).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib as _toml
else:  # pragma: no cover - exercised only on <3.11 CI legs
    import tomli as _toml

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _pyproject() -> dict:
    with open(_PYPROJECT, "rb") as f:
        return _toml.load(f)


def test_runtime_version_resolves():
    import any2heliosdb

    assert isinstance(any2heliosdb.__version__, str)
    # a plausible semver-ish string (e.g. 1.4.0)
    assert re.match(r"^\d+\.\d+\.\d+", any2heliosdb.__version__)


def test_pyproject_declares_dynamic_version():
    data = _pyproject()
    project = data["project"]
    # version is dynamic, never a hardcoded [project] key (else the two could drift)
    assert "version" in project.get("dynamic", [])
    assert "version" not in project


def test_hatch_version_points_at_the_init():
    data = _pyproject()
    path = data["tool"]["hatch"]["version"]["path"]
    assert path == "src/any2heliosdb/__init__.py"
    # and that file actually defines __version__
    init = Path(__file__).resolve().parents[2] / path
    assert re.search(r'^__version__\s*=\s*["\']', init.read_text(), re.MULTILINE)
