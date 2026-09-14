"""Finite fixed-axis solver tests; full original FE revalidation is a separate gate."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from types import SimpleNamespace

import pytest
from shapely.geometry import box
from shapely.ops import unary_union

from rebar.models import Axis, Direction, Layer
from rebar.optimization.algorithms import fe_host_repair as solver
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection

X = Direction(Layer.BOTTOM, Axis.X)
Y = Direction(Layer.BOTTOM, Axis.Y)


def bar(name="a", interval=(-100.0, 1900.0), q=1000.0, *, direction=X, diameter=10, steel="A500"):
    return PhysicalBar(name, direction, steel, diameter, q, interval, ("zone/0/"+name,))


def host(shape=None):
    shape = box(0, 0, 6000, 4000) if shape is None else shape
    return OrthogonalSolidHost((SolidHostSection(0, 200, shape),), 25, 25, 25, shape.area*200, 6)


def obligations(bars, required=None):
    required = [(800.0, 1100.0)]*len(bars) if required is None else required
    return {(b.direction, b.id): {"required_interval_mm": interval,
        "served_cell_ids": (index,) if interval is not None else ()}
        for index, (b, interval) in enumerate(zip(bars, required, strict=True))}


def stock(bars):
    return Counter((b.steel_class, b.diameter_mm, round(b.installed_length_mm, 6)) for b in bars)


def require_scipy():
    pytest.importorskip("scipy.optimize")


def test_resolves_host_without_shortening_or_changing_any_owner_axis():
    require_scipy()
    before = (bar(),)
    after, info = solver.solve_fe_host_repair(before, obligations(before), host())
    assert info["status"] == "checked_finite_pool_solution", info
    assert info["host_blocked_before"] == 1 and info["host_blocked_after"] == 0
    assert after[0].installed_interval_mm == pytest.approx((25, 2025))
    assert replace(after[0], installed_interval_mm=before[0].installed_interval_mm) == before[0]
    assert stock(after) == stock(before)
    assert info["all_lexicographic_stages_optimal_in_pool"]
    assert not info["placement_eligible"] and not info["engineering_approval"] and not info["global_optimality_claimed"]


def test_joint_contacts_move_neighbour_instead_of_creating_same_axis_collision():
    require_scipy()
    before = (bar(), bar("b", (1800.0, 3800.0)))
    demands = obligations(before, [(800.0, 1100.0), (2600.0, 2900.0)])
    after, info = solver.solve_fe_host_repair(before, demands, host())
    assert info["host_failures_resolved"] == 1, info
    assert info["changed_bar_count"] == 2
    assert not solver._collides(*after)
    assert after[0].installed_interval_mm[1] == pytest.approx(after[1].installed_interval_mm[0])
    assert stock(after) == stock(before)


def test_frozen_neighbour_blocks_new_collision_and_old_old_conflict_is_retained():
    require_scipy()
    before = (bar(), bar("b", (1800.0, 3800.0)))
    after, info = solver.solve_fe_host_repair(before, obligations(before, [(800.0, 1100.0), None]), host())
    assert after == before
    assert info["host_blocked_after"] == 1
    assert solver._collides(*after)  # Original conflict is explicit fallback, not silently forbidden/deleted.
    assert info["changed_bar_count"] == 0


def test_transverse_body_collision_checks_different_but_nearby_fixed_axes():
    require_scipy()
    before = (bar(), bar("b", (1800.0, 3800.0), 1005))
    after, info = solver.solve_fe_host_repair(before, obligations(before, [(800.0, 1100.0), None]), host())
    assert after == before and info["host_blocked_after"] == 1


def test_same_planar_projection_different_direction_does_not_add_fake_same_direction_constraint():
    require_scipy()
    before = (bar(), bar("b", (1800.0, 3800.0), 1000, direction=Y))
    after, info = solver.solve_fe_host_repair(before, obligations(before, [(800.0, 1100.0), None]), host())
    assert info["host_failures_resolved"] == 1
    assert after[1] == before[1]
    assert any("3D" in value for value in info["not_checked"])


def swap_case():
    shape = unary_union([box(0, 0, 3000, 1500), box(0, 1500, 6000, 4000)])
    before = (bar("a", (-500.0, 3500.0)), bar("b", (100.0, 2100.0), 2500))
    return before, obligations(before, [(1200.0, 1600.0), (800.0, 1200.0)]), host(shape)


def test_stock_preserving_length_swap_requires_opt_in_and_complete_inventory():
    require_scipy()
    before, demands, actual_host = swap_case()
    frozen, no = solver.solve_fe_host_repair(before, demands, actual_host)
    assert frozen == before and no["host_blocked_after"] == 1
    after, info = solver.solve_fe_host_repair(before, demands, actual_host, reassign_lengths=True)
    assert info["host_blocked_after"] == 0, info
    assert [b.installed_length_mm for b in after] == pytest.approx([2000, 4000])
    assert stock(after) == stock(before)
    for old, new in zip(before, after, strict=True):
        assert replace(new, installed_interval_mm=old.installed_interval_mm) == old


@pytest.mark.parametrize("constraint", ["steel", "diameter", "frozen_donor"])
def test_cannot_borrow_stock_from_other_material_or_frozen_bar(constraint):
    require_scipy()
    before, _, actual_host = swap_case()
    a, b = before
    if constraint == "steel":
        b = replace(b, steel_class="A400")
    elif constraint == "diameter":
        b = replace(b, diameter_mm=12)
    before = (a, b)
    demands = obligations(before, [(1200.0, 1600.0), None if constraint == "frozen_donor" else (800.0, 1200.0)])
    after, info = solver.solve_fe_host_repair(before, demands, actual_host, reassign_lengths=True)
    assert after == before and info["host_blocked_after"] == 1


def test_explicit_none_freezes_outside_original_without_removing_it():
    before = (bar(),)
    after, info = solver.solve_fe_host_repair(before, obligations(before, [None]), host(), reassign_lengths=True)
    assert after is before
    assert info["status"] == "original_incumbent_no_alternatives"
    assert not info["solver_executed"]
    assert info["physical_bar_count"] == 1 and info["host_blocked_after"] == 1


def test_independent_real_hole_containment_not_host_bbox_only():
    require_scipy()
    before = (bar(),)
    actual_host = host(box(0, 0, 6000, 4000).difference(box(1800, 900, 2000, 1100)))
    after, info = solver.solve_fe_host_repair(before, obligations(before), actual_host)
    assert after == before and info["host_blocked_after"] == 1


@pytest.mark.parametrize("which", ["missing", "extra", "unknown_field", "interval", "nan", "bool", "long",
    "direction", "duplicate_bar", "lost_owner", "original_40d", "empty_cells", "bars_list"])
def test_malformed_or_unknown_frozen_input_rejected_before_search(which):
    before = (bar(),)
    demands = obligations(before)
    if which == "missing":
        demands.clear()
    elif which == "extra":
        demands[(X, "unknown")] = demands[(X, "a")]
    elif which == "unknown_field":
        demands[(X, "a")]["allow_no_anchorage"] = True
    elif which == "interval":
        demands[(X, "a")]["required_interval_mm"] = [800, 1100]
    elif which == "nan":
        before = (replace(before[0], transverse_axis_mm=float("nan")),)
    elif which == "bool":
        before = (replace(before[0], diameter_mm=True),)
    elif which == "long":
        before = (replace(before[0], installed_interval_mm=(-100, 12000)),)
    elif which == "direction":
        before = (replace(before[0], direction="bottom-X"),)
    elif which == "duplicate_bar":
        before *= 2
    elif which == "lost_owner":
        before = (replace(before[0], source_bar_ids=()),)
    elif which == "original_40d":
        demands[(X, "a")]["required_interval_mm"] = (0, 1800)
    elif which == "empty_cells":
        demands[(X, "a")]["served_cell_ids"] = ()
    else:
        before = list(before)
    with pytest.raises(ValueError):
        solver.solve_fe_host_repair(before, demands, host())


@pytest.mark.parametrize("kwargs", [{"reassign_lengths": 1}, {"time_limit_s": 0}, {"time_limit_s": float("inf")},
    {"maximum_candidates_per_bar": True}, {"maximum_total_candidates": 0}, {"maximum_pair_checks": -1}])
def test_invalid_search_limits_rejected(kwargs):
    before = (bar(),)
    with pytest.raises(ValueError):
        solver.solve_fe_host_repair(before, obligations(before), host(), **kwargs)


def test_candidate_bounds_do_not_truncate_original_party():
    before = (bar(), bar("b", q=2000))
    after, info = solver.solve_fe_host_repair(before, obligations(before), host(), maximum_total_candidates=1)
    assert after is before and info["status"] == "original_incumbent_budget"
    after, info = solver.solve_fe_host_repair(before, obligations(before), host(), maximum_candidates_per_bar=1)
    assert after is before and info["candidate_pool_truncated"]
    assert info["discarded_by_candidate_bounds"] > 0
    assert info["candidate_count"] == 2


def test_pair_budget_exhaustion_returns_whole_original():
    before = (bar(), bar("b", (1800.0, 3800.0)))
    after, info = solver.solve_fe_host_repair(before, obligations(before, [(800.0, 1100.0), (2600.0, 2900.0)]),
        host(), maximum_pair_checks=1)
    assert after is before and info["reason"] == "pair_check_limit"
    assert not info["solver_executed"]


def test_wall_time_budget_retains_original(monkeypatch):
    clock = iter([0.0, 100.0, 100.0])
    monkeypatch.setattr(solver, "perf_counter", lambda: next(clock))
    before = (bar(),)
    after, info = solver.solve_fe_host_repair(before, obligations(before), host(), time_limit_s=1)
    assert after is before and info["reason"] == "time_limit"


@pytest.mark.parametrize("mode", ["fractional", "nonfinite", "zero", "timeout"])
def test_solver_values_never_trusted_without_complete_integer_constraints(monkeypatch, mode):
    require_scipy()
    import numpy as np
    import scipy.optimize
    def fake(cost, **kwargs):
        value = 0.5 if mode == "fractional" else float("nan") if mode == "nonfinite" else 0
        return SimpleNamespace(status=1 if mode == "timeout" else 0, x=np.full(len(cost), value), message=mode)
    monkeypatch.setattr(scipy.optimize, "milp", fake)
    before = (bar(),)
    after, info = solver.solve_fe_host_repair(before, obligations(before), host())
    assert after is before and info["host_blocked_after"] == 1
    assert not info["stages"][0]["integer_incumbent_checked"]


def test_later_tiebreak_timeout_retains_checked_improvement(monkeypatch):
    require_scipy()
    import scipy.optimize
    real = scipy.optimize.milp
    calls = []
    def first_only(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            return SimpleNamespace(status=1, x=None, message="time limit")
        return real(*args, **kwargs)
    monkeypatch.setattr(scipy.optimize, "milp", first_only)
    before = (bar(),)
    after, info = solver.solve_fe_host_repair(before, obligations(before), host())
    assert after != before and info["host_failures_resolved"] == 1
    assert info["status"] == "checked_incumbent_solver_stopped"
    assert not info["all_lexicographic_stages_optimal_in_pool"]


def test_earlier_stage_can_have_worse_unoptimized_tiebreak_without_aborting(monkeypatch):
    require_scipy()
    import numpy as np
    import scipy.optimize
    real = scipy.optimize.milp
    calls = []
    def opposite_tiebreak(cost, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            # Both feasible changed choices optimize changed-count equally.
            # Deliberately prefer the LAST (farther) one; stage3 must still run.
            result = real(-np.arange(len(cost), dtype=float), **kwargs)
            return result
        return real(cost, **kwargs)
    monkeypatch.setattr(scipy.optimize, "milp", opposite_tiebreak)
    before = (bar(),)
    after, info = solver.solve_fe_host_repair(before, obligations(before), host())
    assert len(calls) == 3 and info["status"] == "checked_finite_pool_solution", info
    assert after[0].installed_interval_mm[0] == pytest.approx(25)
