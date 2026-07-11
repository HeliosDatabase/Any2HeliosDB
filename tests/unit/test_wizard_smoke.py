"""config/wizard.smoke_test — the non-interactive, testable seam of the wizard.

Hermetic: the store builders are faked in the wizard namespace (exactly where
smoke_test looks them up), so no live source/target. Pins the report the wizard
records and the NULL-vs-empty-string fidelity verdict, plus the load-path choice
(COPY when the probe reports copy_from_stdin, else INSERT).
"""
from __future__ import annotations

from any2heliosdb.config import wizard
from any2heliosdb.constants import Edition
from any2heliosdb.target.base import CapabilityMatrix


class FakeSource:
    def __init__(self):
        self.closed = False

    def connect(self):
        pass

    def server_version(self):
        return "Oracle 19c"

    def default_schema(self):
        return "HR"

    def close(self):
        self.closed = True


class FakeTarget:
    def __init__(self, caps, fidelity_rows):
        self.capabilities = caps
        self._rows = fidelity_rows
        self.executed = []
        self.copied = False
        self.inserted = False
        self.closed = False

    def connect(self):
        pass

    def server_banner(self):
        return "17.0 (HeliosDB-Lite 2.0)"

    def probe_capabilities(self):
        return self.capabilities

    def execute(self, sql, params=None):
        self.executed.append(sql)

    def copy_rows(self, table, cols, rows):
        self.copied = True
        return len(list(rows))

    def insert_rows(self, table, cols, rows, on_conflict_do_nothing=False):
        self.inserted = True
        return len(list(rows))

    def query(self, sql, params=None):
        return self._rows

    def close(self):
        self.closed = True


def _caps(copy):
    return CapabilityMatrix(edition=Edition.LITE, copy_from_stdin=copy,
                            enforces_check=True, enforces_fk=True)


def _inject(monkeypatch, source, target):
    monkeypatch.setattr(wizard, "build_source_adapter", lambda cfg: source)
    monkeypatch.setattr(wizard, "build_target_driver", lambda cfg: target)


# n=1 -> s '' (not null: False); n=2 -> s NULL (True) == correct fidelity
_GOOD_ROWS = [(1, False, ""), (2, True, None)]
_BAD_ROWS = [(1, True, None), (2, True, None)]


def test_smoke_test_copy_path_reports_full_fidelity(monkeypatch):
    src = FakeSource()
    tgt = FakeTarget(_caps(copy=True), _GOOD_ROWS)
    _inject(monkeypatch, src, tgt)

    report = wizard.smoke_test(cfg=object())

    assert report["source_version"] == "Oracle 19c"
    assert report["source_schema"] == "HR"
    assert report["target_banner"] == "17.0 (HeliosDB-Lite 2.0)"
    assert report["target_edition"] == Edition.LITE.value
    assert report["copy_from_stdin"] is True
    assert report["enforces_check"] is True and report["enforces_fk"] is True
    assert report["null_empty_fidelity"] is True
    # COPY chosen (copy_from_stdin True); table dropped afterwards; ends closed.
    assert tgt.copied is True and tgt.inserted is False
    assert tgt.executed.count("DROP TABLE IF EXISTS _a2h_smoke") == 2
    assert src.closed and tgt.closed


def test_smoke_test_insert_path_when_copy_unsupported(monkeypatch):
    src = FakeSource()
    tgt = FakeTarget(_caps(copy=False), _GOOD_ROWS)
    _inject(monkeypatch, src, tgt)
    report = wizard.smoke_test(cfg=object())
    assert report["copy_from_stdin"] is False
    assert tgt.inserted is True and tgt.copied is False
    assert report["null_empty_fidelity"] is True


def test_smoke_test_flags_broken_null_empty_fidelity(monkeypatch):
    # A target that collapses '' -> NULL fails the round-trip verdict.
    src = FakeSource()
    tgt = FakeTarget(_caps(copy=True), _BAD_ROWS)
    _inject(monkeypatch, src, tgt)
    report = wizard.smoke_test(cfg=object())
    assert report["null_empty_fidelity"] is False


def test_smoke_test_lost_row_reports_fidelity_false_not_indexerror(monkeypatch):
    # A round-trip that LOSES a row (only n=1 comes back of the 2 loaded) is
    # itself a fidelity failure: the len-guard reports False instead of crashing
    # with IndexError on rows[1].
    src = FakeSource()
    tgt = FakeTarget(_caps(copy=True), [(1, False, "")])
    _inject(monkeypatch, src, tgt)
    report = wizard.smoke_test(cfg=object())
    assert report["null_empty_fidelity"] is False
    # no exception -> the wizard still finished its cleanup and closed both ends
    assert tgt.executed.count("DROP TABLE IF EXISTS _a2h_smoke") == 2
    assert src.closed and tgt.closed
