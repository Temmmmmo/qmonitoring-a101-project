from itertools import combinations
from types import SimpleNamespace

import pytest

from rebar.optimization.services.finite_cover import solve_finite_cover_front


def test_position_limit_uses_union_of_types_not_zone_or_component_counts():
    coverage = (frozenset({0}), frozenset({1}), frozenset({0, 1}), frozenset({1}))
    masses = (1, 1, 4, 2)
    keys = (frozenset({("a",)}), frozenset({("b",)}), frozenset({("a",), ("b",)}), frozenset({("a",)}))
    result, telemetry = solve_finite_cover_front(coverage, masses, 2, maximum_zones=3, budgets=(3,),
        position_keys=keys, position_budgets=(1, 2), time_limit_s=5)
    assert (0, 1) in result and (0, 3) in result
    assert all(r["position_count"] <= r["maximum_positions"] for r in telemetry["solves"]
               if r["accepted"] and r["maximum_positions"] is not None)
    for cap in (1, 2):
        answer, _ = solve_finite_cover_front(coverage, masses, 2, maximum_zones=3, budgets=(3,),
                                           position_keys=keys, maximum_positions=cap, time_limit_s=5)
        oracle = min(sum(masses[i] for i in indexes) for n in range(1, 4)
                     for indexes in combinations(range(4), n)
                     if set().union(*(coverage[i] for i in indexes)) == {0, 1}
                     and len(set().union(*(keys[i] for i in indexes))) <= cap)
        assert min(sum(masses[i] for i in indexes) for indexes in answer) == oracle


@pytest.mark.parametrize("vector", [[1, 0], [1, 0.5], [1, 2], [0, 1], [1], [1, float("nan")]])
def test_activation_independently_checked_after_milp(monkeypatch, vector):
    import numpy as np
    import scipy.optimize

    monkeypatch.setattr(scipy.optimize, "milp", lambda *a, **kw: SimpleNamespace(status=1, x=np.array(vector)))
    proposals, _ = solve_finite_cover_front((frozenset({0}),), (1,), 1, maximum_zones=1, budgets=(1,),
        position_keys=(frozenset({("a",)}),), maximum_positions=1, time_limit_s=1)
    assert not proposals


@pytest.mark.parametrize("keys,cap", [(None, 1), ((frozenset(),), 1), ((frozenset({("a",)}),), True),
                                   ((frozenset({("a",)}),), 0), ((), 1)])
def test_invalid_position_model_rejected(keys, cap):
    with pytest.raises(ValueError):
        solve_finite_cover_front((frozenset({0}),), (1,), 1, maximum_zones=1, budgets=(1,),
                                position_keys=keys, maximum_positions=cap, time_limit_s=1)
