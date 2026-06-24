"""Unit tests for PK-range chunking (deterministic, gap/overlap-free)."""
from any2heliosdb.chunking.pk_range import compute_chunks
from any2heliosdb.core.catalog_model import Column, DataType, PrimaryKey, Table


class FakeSrc:
    def __init__(self, bounds):
        self._b = bounds

    def numeric_pk_bounds(self, table, col):
        return self._b


def _t():
    return Table(
        name="EMP", schema="HR",
        columns=[Column("ID", DataType.decimal(10, 0), nullable=False)],
        primary_key=PrimaryKey(columns=["ID"]),
    )


def test_chunks_cover_range_without_gaps_or_overlap():
    chunks = compute_chunks(FakeSrc((1, 10)), _t(), 4)
    assert chunks[0].lo == 1
    # half-open and contiguous: each chunk's hi is the next chunk's lo
    for i in range(len(chunks) - 1):
        assert chunks[i].hi == chunks[i + 1].lo
    # exclusive upper bound on the last chunk includes the max (10)
    assert chunks[-1].hi == 11
    # deterministic ids
    assert [c.chunk_id for c in chunks] == ["EMP:{}".format(i) for i in range(len(chunks))]
    # per-side predicates (source quoted/uppercase, target lowercased)
    assert chunks[0].source_where() == '"ID" >= 1 AND "ID" < {}'.format(chunks[0].hi)
    assert chunks[0].target_where() == "id >= 1 AND id < {}".format(chunks[0].hi)
    # preserve_case keeps "ID" mixed/upper, which MUST be quoted so the target
    # doesn't fold it to "id" (a bare ID would address the wrong/no column).
    assert chunks[0].target_where(preserve_case=True) == \
        '"ID" >= 1 AND "ID" < {}'.format(chunks[0].hi)


def test_single_chunk_when_no_numeric_pk():
    chunks = compute_chunks(FakeSrc(None), _t(), 4)
    assert len(chunks) == 1
    assert chunks[0].pk_col is None
    assert chunks[0].source_where() is None
    assert chunks[0].target_where() is None


def test_single_chunk_when_target_chunks_is_one():
    chunks = compute_chunks(FakeSrc((1, 1000)), _t(), 1)
    assert len(chunks) == 1


def test_chunk_count_capped_by_span():
    # span of 3 values but asking for 8 chunks -> at most 3 chunks
    chunks = compute_chunks(FakeSrc((5, 7)), _t(), 8)
    assert 1 <= len(chunks) <= 3
    assert chunks[-1].hi == 8


def test_target_where_quotes_reserved_pk_column():
    from any2heliosdb.chunking.pk_range import Chunk
    t = Table(name="orders", schema="s", columns=[])
    # A reserved-word PK must be quoted in the idempotent range DELETE predicate,
    # or it is a syntax error / addresses the wrong column on chunk retry/resume.
    assert Chunk(t, "orders:0", "order", 0, 10).target_where() == \
        '"order" >= 0 AND "order" < 10'
    assert Chunk(t, "orders:0", "id", 0, 10).target_where() == "id >= 0 AND id < 10"
