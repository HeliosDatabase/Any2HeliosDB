"""Unit tests for topological view ordering (a2h export/emit #3).

A view that selects from another view must be emitted after it — PostgreSQL and
HeliosDB both require the referent to exist at CREATE time. `_order_views` sorts
`schema.views` accordingly."""
from any2heliosdb.core.catalog_model import View
from any2heliosdb.core.orchestrator import _order_views


def _v(name, definition):
    return View(name=name, definition=definition)


def _names(views):
    return [v.name for v in views]


def test_referent_before_referrer():
    # v_child selects from v_parent -> parent must come first even if listed last.
    views = [
        _v("v_child", "SELECT * FROM v_parent WHERE x > 0"),
        _v("v_parent", "SELECT id, x FROM base_table"),
    ]
    assert _names(_order_views(views)) == ["v_parent", "v_child"]


def test_transitive_chain():
    # c -> b -> a  (declared c, b, a) must emit a, b, c.
    views = [
        _v("c", "SELECT * FROM b"),
        _v("b", "SELECT * FROM a"),
        _v("a", "SELECT 1 AS n FROM t"),
    ]
    assert _names(_order_views(views)) == ["a", "b", "c"]


def test_independent_views_keep_source_order():
    # No inter-view deps -> stable (original order preserved).
    views = [_v("m", "SELECT * FROM t1"),
             _v("k", "SELECT * FROM t2"),
             _v("z", "SELECT * FROM t3")]
    assert _names(_order_views(views)) == ["m", "k", "z"]


def test_recursive_self_reference_is_not_a_dependency():
    # A recursive view references its own name; that must not deadlock ordering.
    views = [
        _v("v_hier", "WITH cte AS (SELECT * FROM v_hier) SELECT * FROM cte"),
        _v("v_leaf", "SELECT * FROM t"),
    ]
    out = _names(_order_views(views))
    assert set(out) == {"v_hier", "v_leaf"} and len(out) == 2


def test_cycle_falls_back_without_losing_views():
    # Mutually-referencing views (no engine accepts this) must not crash or drop.
    views = [_v("a", "SELECT * FROM b"), _v("b", "SELECT * FROM a")]
    out = _order_views(views)
    assert _names(out) == ["a", "b"]  # original relative order, both present


def test_word_boundary_avoids_substring_false_dependency():
    # "emp" must NOT be considered referenced by a view whose body says "employee".
    views = [
        _v("employee_v", "SELECT * FROM employees"),
        _v("emp", "SELECT * FROM emp_base"),
    ]
    # No real dependency between them -> order unchanged (stable).
    assert _names(_order_views(views)) == ["employee_v", "emp"]


def test_schema_qualified_reference_is_detected():
    views = [
        _v("v_top", "SELECT * FROM hr.v_bottom JOIN t USING (id)"),
        _v("v_bottom", "SELECT id FROM hr.base"),
    ]
    assert _names(_order_views(views)) == ["v_bottom", "v_top"]


def test_single_and_empty_are_noops():
    assert _order_views([]) == []
    one = [_v("solo", "SELECT 1")]
    assert _names(_order_views(one)) == ["solo"]
