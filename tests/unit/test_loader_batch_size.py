"""options.batch_size must tune the resumable/parallel load path, not just the
sequential fallback.

Regression for the interface-honesty defect: ``config.toml`` (and the MCP migrate
tool) document ``batch_size`` as the source-side fetch arraysize, but the
resumable loader — the path every non-native migrate takes — constructed
``ResumableLoader`` without it and streamed each chunk with no ``arraysize``, so
the hardcoded adapter default (1000) always won and the knob was a silent no-op.
These tests pin the fix: the configured batch_size reaches ``stream_rows`` as
``arraysize`` on the chunked path, and the orchestrator threads it into the
loader.

Hermetic — a fake source/target adapter (no DB server) is injected by
monkeypatching the config store's builders, exactly the seam the loader imports.
"""
import types

from any2heliosdb.core import manifest as M
from any2heliosdb.core.catalog_model import (Column, DataType, PrimaryKey, Schema,
                                             Table)
from any2heliosdb.core.loader import ResumableLoader


class RecordingSource:
    """Fake source adapter that serves both ``plan()`` and ``_load_chunk`` and
    records the ``arraysize`` every ``stream_rows`` call received."""

    def __init__(self, bounds_by_fqn, row_count=100):
        self.bounds_by_fqn = bounds_by_fqn
        self.row_count = row_count
        self.stream_arraysizes = []

    def connect(self):
        pass

    def close(self):
        pass

    def capture_snapshot(self):
        return None

    def use_snapshot(self, token):
        pass

    def exact_row_count(self, table):
        return self.row_count

    def numeric_pk_bounds(self, table, col):
        return self.bounds_by_fqn.get(table.fqn)

    def stream_rows(self, table, columns, where=None, arraysize=1000):
        self.stream_arraysizes.append(arraysize)
        return iter([])


class FakeTarget:
    def connect(self):
        pass

    def close(self):
        pass

    def load_range(self, target_table, columns, rows, where=None, use_copy=True):
        return sum(1 for _ in rows)


def _cfg(tmp_path, batch_size=1000):
    options = types.SimpleNamespace(
        preserve_case=False, manifest_backend="sqlite",
        output_dir=str(tmp_path), parallelism=2, batch_size=batch_size)
    source = types.SimpleNamespace(
        dialect="oracle", host="h", port=1521, database="db", schema="HR", user="hr")
    target = types.SimpleNamespace(
        driver="native", host="th", port=1521, dbname="tdb", user="tu")
    return types.SimpleNamespace(source=source, target=target, options=options)


def _table(name="EMP"):
    return Table(
        name=name, schema="HR",
        columns=[Column("ID", DataType.decimal(10, 0), nullable=False),
                 Column("NAME", DataType.varchar(50))],
        primary_key=PrimaryKey(columns=["ID"]))


def _inject(monkeypatch, source, target):
    from any2heliosdb.config import store
    monkeypatch.setattr(store, "build_source_adapter", lambda cfg: source)
    monkeypatch.setattr(store, "build_target_driver", lambda cfg: target)


def _load_one_chunk(loader):
    """Plan, then drive one chunk through the real _load_chunk seam. All chunks
    belong to the single HR.EMP table, so any one exercises stream_rows."""
    loader.plan()
    chunk = next(iter(loader._chunks.values()))
    loader._load_chunk("HR.EMP", chunk)


def test_load_chunk_passes_configured_batch_size_as_arraysize(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, batch_size=500)
    manifest_path = M.manifest_path_for(str(tmp_path))
    src = RecordingSource({"HR.EMP": (1, 100)})
    tgt = FakeTarget()
    _inject(monkeypatch, src, tgt)
    loader = ResumableLoader(cfg, Schema("HR", tables=[_table()]), manifest_path, "run1",
                             parallelism=2, batch_size=500)
    _load_one_chunk(loader)
    # every chunk read used the configured arraysize, never the hardcoded 1000
    assert src.stream_arraysizes and all(a == 500 for a in src.stream_arraysizes)


def test_load_chunk_defaults_to_1000_when_unset(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)  # batch_size defaults to 1000
    manifest_path = M.manifest_path_for(str(tmp_path))
    src = RecordingSource({"HR.EMP": (1, 100)})
    tgt = FakeTarget()
    _inject(monkeypatch, src, tgt)
    # constructor default (unset) is 1000 — matches config/model Options.batch_size
    loader = ResumableLoader(cfg, Schema("HR", tables=[_table()]), manifest_path, "run1",
                             parallelism=2)
    assert loader.batch_size == 1000
    _load_one_chunk(loader)
    assert src.stream_arraysizes and all(a == 1000 for a in src.stream_arraysizes)


def test_batch_size_excluded_from_config_hash(tmp_path):
    """batch_size is not plan-affecting, so changing it must NOT change the
    drift-guard hash (else a resume with a tweaked batch_size would reset the
    ledger and re-load already-LOADED chunks)."""
    cfg = _cfg(tmp_path, batch_size=1000)
    manifest_path = M.manifest_path_for(str(tmp_path))
    schema = Schema("HR", tables=[_table()])
    h1 = ResumableLoader(cfg, schema, manifest_path, "run1", parallelism=2,
                         batch_size=1000)._config_hash()
    h2 = ResumableLoader(_cfg(tmp_path, batch_size=8000), schema, manifest_path, "run1",
                         parallelism=2, batch_size=8000)._config_hash()
    assert h1 == h2


def test_orchestrator_threads_batch_size_into_loader(tmp_path, monkeypatch):
    """migrate() must construct the ResumableLoader with the batch_size it was
    given, so the CLI/MCP config value actually reaches the chunked read."""
    from any2heliosdb.constants import Edition
    from any2heliosdb.core import loader as loader_mod
    from any2heliosdb.core.orchestrator import migrate as run_migrate

    captured = {}

    class _FakeLoader:
        def __init__(self, cfg, schema, manifest_path, run_id, **kw):
            captured["batch_size"] = kw.get("batch_size")

        def run(self):
            return types.SimpleNamespace(rows={}, warnings=[], chunks_total=0, chunks_loaded=0)

    class _EmptySchema:
        tables = []
        sequences = []
        views = []

    class _Caps:
        edition = Edition.UNKNOWN
        copy_from_stdin = True
        concurrent_writes = True

    class _FakeSource:
        def introspect_schema(self, schema):
            return _EmptySchema()

        def stream_rows(self, *a, **k):
            return iter([])

    class _FakeTarget:
        dialect = "postgres"
        capabilities = _Caps()

        def probe_capabilities(self):
            pass

        def execute(self, *a, **k):
            pass

    monkeypatch.setattr(loader_mod, "ResumableLoader", _FakeLoader)
    run_migrate(_FakeSource(), _FakeTarget(), cfg=object(),
                manifest_path=str(tmp_path / "m.db"), run_id="r", batch_size=777)
    assert captured["batch_size"] == 777


# --- chunks_per_worker: plan-affecting, so it JOINS the config hash ----------
def test_chunks_per_worker_changes_config_hash(tmp_path):
    """chunks_per_worker sets the per-table chunk COUNT, so (unlike batch_size) it
    MUST change the drift-guard hash — else a resume with a new value would replay
    the OLD recorded chunk plan while the operator believes the new chunking is in
    effect (silently mixing two plans)."""
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    schema = Schema("HR", tables=[_table()])
    h2 = ResumableLoader(cfg, schema, manifest_path, "run1", parallelism=2,
                         chunks_per_worker=2)._config_hash()
    h3 = ResumableLoader(cfg, schema, manifest_path, "run1", parallelism=2,
                         chunks_per_worker=3)._config_hash()
    assert h2 != h3


def test_chunks_per_worker_threads_into_chunk_count(tmp_path, monkeypatch):
    """A fresh plan splits each table into ~parallelism*chunks_per_worker chunks."""
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    src = RecordingSource({"HR.EMP": (1, 1000)})
    _inject(monkeypatch, src, FakeTarget())
    loader = ResumableLoader(cfg, Schema("HR", tables=[_table()]), manifest_path, "run1",
                             parallelism=2, chunks_per_worker=3)
    loader.plan()
    # parallelism 2 * chunks_per_worker 3 = 6 chunks over the [1,1000] PK range
    assert len(loader._chunks) == 6


def test_chunks_per_worker_change_resets_prior_plan(tmp_path, monkeypatch):
    """Reset regression: changing chunks_per_worker across a re-plan of the same
    run_id must RESET (clear prior LOADED chunks + re-plan) rather than replay the
    stale plan — mirrors the parallelism drift-guard behaviour."""
    cfg = _cfg(tmp_path)
    manifest_path = M.manifest_path_for(str(tmp_path))
    rid = "run1"

    _inject(monkeypatch, RecordingSource({"HR.EMP": (1, 100)}), FakeTarget())
    loader1 = ResumableLoader(cfg, Schema("HR", tables=[_table()]), manifest_path, rid,
                              parallelism=2, chunks_per_worker=2)
    loader1.plan()
    assert len(loader1._chunks) == 4  # 2*2

    # Mark one chunk LOADED, then re-plan with a DIFFERENT chunks_per_worker.
    from any2heliosdb.core.manifest import Manifest
    man = Manifest(manifest_path)
    man.set_chunk_state(rid, "HR.EMP", "EMP:0", M.LOADED, rows_loaded=25)
    man.close()

    _inject(monkeypatch, RecordingSource({"HR.EMP": (1, 100)}), FakeTarget())
    loader2 = ResumableLoader(cfg, Schema("HR", tables=[_table()]), manifest_path, rid,
                              parallelism=2, chunks_per_worker=1)
    loader2.plan()
    # re-planned fresh at the new count (2*1) — not the replayed 4-chunk plan
    assert len(loader2._chunks) == 2
    # and the prior LOADED chunk state was cleared by the reset
    man = Manifest(manifest_path)
    try:
        counts = man.summary(rid)["chunk_states"]
    finally:
        man.close()
    assert counts.get(M.LOADED, 0) == 0


def test_orchestrator_threads_chunks_per_worker_into_loader(tmp_path, monkeypatch):
    """migrate() must construct the ResumableLoader with the chunks_per_worker it
    was given, so the CLI/MCP config value reaches the chunk planner."""
    from any2heliosdb.constants import Edition
    from any2heliosdb.core import loader as loader_mod
    from any2heliosdb.core.orchestrator import migrate as run_migrate

    captured = {}

    class _FakeLoader:
        def __init__(self, cfg, schema, manifest_path, run_id, **kw):
            captured["chunks_per_worker"] = kw.get("chunks_per_worker")

        def run(self):
            return types.SimpleNamespace(rows={}, warnings=[], chunks_total=0, chunks_loaded=0)

    class _EmptySchema:
        tables = []
        sequences = []
        views = []

    class _Caps:
        edition = Edition.UNKNOWN
        copy_from_stdin = True
        concurrent_writes = True

    class _FakeSource:
        def introspect_schema(self, schema):
            return _EmptySchema()

    class _FakeTarget:
        dialect = "postgres"
        capabilities = _Caps()

        def probe_capabilities(self):
            pass

        def execute(self, *a, **k):
            pass

    monkeypatch.setattr(loader_mod, "ResumableLoader", _FakeLoader)
    run_migrate(_FakeSource(), _FakeTarget(), cfg=object(),
                manifest_path=str(tmp_path / "m.db"), run_id="r", chunks_per_worker=5)
    assert captured["chunks_per_worker"] == 5
