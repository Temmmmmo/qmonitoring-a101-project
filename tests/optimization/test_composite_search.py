from dataclasses import replace
from types import SimpleNamespace

import pytest

from rebar.models import Axis
from rebar.optimization.algorithms.composite_pool import solve_composite_pool
from rebar.optimization.contracts.composite_coverage import COMPOSITE_COVERAGE_POLICY
from rebar.optimization.contracts.composite_search import CompositeSearchProblem
from rebar.optimization.contracts.problem import LayoutConstraints, LayoutProblem
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_windows import covering_composite_window
from rebar.optimization.services.finite_cover import solve_finite_cover_front

from test_composite_coverage import demand_sample, zone_sample
from test_composite_host import host_sample


def problem_sample(axis=Axis.X, required_level=2):
    demand = demand_sample(axis, required_level=required_level)
    placements = tuple((i, zone_sample(demand, level=i).placement) for i in (1, 2, 3))
    return CompositeSearchProblem(demand, placements, LayoutConstraints(), COMPOSITE_COVERAGE_POLICY)


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
@pytest.mark.parametrize("phase", [-325, -50, 0, 50, 200, 10000])
def test_covering_window_keeps_required_area_and_services_its_edges(axis, phase):
    problem = problem_sample(axis)
    zone = zone_sample(problem.demand, phase25=phase)
    box = covering_composite_window(problem.demand, problem.demand.bbox, 2, zone.placement, constraints=problem.constraints)
    rebuilt = build_composite_zone(problem.demand, box, 2, "expanded", zone.placement)
    check = evaluate_composite_coverage(problem.demand, [rebuilt], policy_id=problem.policy_id)
    assert check.status == "pass" and check.uncovered_cell_count == 0
    transverse = (box[3] - box[1]) if axis is Axis.X else (box[2] - box[0])
    assert transverse % 300 == pytest.approx(0)


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
@pytest.mark.parametrize("required", [1, 2, 3])
def test_search_preserves_recipes_and_revalidates_selected_points(axis, required):
    pytest.importorskip("scipy")
    problem = problem_sample(axis, required)
    result = solve_composite_pool(problem, maximum_zones=4, maximum_candidates=32, partition_depth=2)
    assert result.points and result.selected_index is not None and not result.placement_eligible
    for point in result.points:
        check = evaluate_composite_coverage(problem.demand, point.zones, policy_id=problem.policy_id)
        assert check == point.coverage and check.status == "pass"
        assert check.additional_mass_kg == pytest.approx(sum(c.mass_kg for z in point.zones for c in z.components))
        assert check.physical_bar_count == sum(c.bar_count for z in point.zones for c in z.components)
        assert all(len(z.recipe.additions) >= (1 if required == 1 else 2) for z in point.zones)
        assert len(point.zones) <= 4
    with pytest.raises(ValueError, match="несколько"):
        LayoutProblem(problem.demand)  # Do not weaken the old GA's safety boundary.


def test_background_only_is_an_empty_front_point_not_a_fake_mass_or_permission():
    result = solve_composite_pool(problem_sample(required_level=0))
    assert len(result.points) == 1 and result.selected_index == 0
    assert result.points[0].coverage.additional_mass_kg == 0 and result.points[0].zones == ()
    assert not result.placement_eligible


@pytest.mark.parametrize("option,value", [("maximum_zones", True), ("maximum_zones", 0),
                                         ("maximum_candidates", 1025), ("partition_depth", -1),
                                         ("solver_time_limit_s", float("nan")), ("solver_time_limit_s", True)])
def test_invalid_search_budget_fails(option, value):
    with pytest.raises(ValueError):
        solve_composite_pool(problem_sample(), **{option: value})


@pytest.mark.parametrize("broken", ["missing_phase", "duplicate", "unknown_origin", "no_overlap", "policy"])
def test_invalid_problem_cannot_be_silently_repaired(broken):
    problem = problem_sample()
    if broken == "missing_phase":
        problem = replace(problem, placements=problem.placements[:1])
    elif broken == "duplicate":
        problem = replace(problem, placements=(*problem.placements, problem.placements[0]))
    elif broken == "unknown_origin":
        level, placement = problem.placements[0]
        problem = replace(problem, placements=((level, replace(placement, additions=(
            replace(placement.additions[0], origin_mm=None),))), *problem.placements[1:]))
    elif broken == "no_overlap":
        problem = replace(problem, constraints=LayoutConstraints(allow_overlaps=False))
    else:
        problem = replace(problem, policy_id="unapproved")
    with pytest.raises(ValueError):
        solve_composite_pool(problem)


def test_finite_cover_finds_mass_vs_zone_tradeoff_with_no_weak_summing():
    pytest.importorskip("scipy")
    coverage = (frozenset({0}), frozenset({1}), frozenset({0, 1}))
    proposals, telemetry = solve_finite_cover_front(coverage, (1, 1, 3), 2,
                                                  maximum_zones=2, budgets=(1, 2), time_limit_s=5)
    assert set(proposals) == {(2,), (0, 1)}
    assert all(record["accepted"] for record in telemetry["solves"])
    proposals, telemetry = solve_finite_cover_front((frozenset({0}),), (1,), 2,
                                                  maximum_zones=2, budgets=(1, 2), time_limit_s=5)
    assert not proposals and telemetry["uncoverable_cells"] == [1]


@pytest.mark.parametrize("masses,count,budgets", [((float("nan"),), 1, (1,)), ((0,), 1, (1,)),
                                               ((1,), 0, (1,)), ((1,), 1, (True,)), ((1,), 1, (3,))])
def test_finite_cover_rejects_invalid_matrix(masses, count, budgets):
    with pytest.raises(ValueError):
        solve_finite_cover_front((frozenset({0}),), masses, count, maximum_zones=2, budgets=budgets, time_limit_s=5)


def test_search_budget_does_not_claim_global_infeasibility():
    problem = problem_sample(required_level=3)
    problem = replace(problem, demand=replace(problem.demand, cells=(
        replace(problem.demand.cells[0], level_index=1), problem.demand.cells[1])))
    result = solve_composite_pool(problem, maximum_candidates=1)
    assert not result.points and result.selected_index is None
    assert result.telemetry["pool_truncated"]
    assert not result.telemetry["engineering_optimality_proven"]


@pytest.mark.parametrize("values", [None, [0.5, 0.5], [0, 0], [float("nan"), 1], [-1, 2], [1, 1]])
def test_solver_failure_fractional_or_overbudget_proposal_is_not_accepted(monkeypatch, values):
    np = pytest.importorskip("numpy")
    scipy = pytest.importorskip("scipy.optimize")
    monkeypatch.setattr(scipy, "milp", lambda *a, **kw: SimpleNamespace(status=1, x=None if values is None else np.array(values)))
    proposals, telemetry = solve_finite_cover_front((frozenset({0}), frozenset({1})), (1, 1), 2,
                                                  maximum_zones=1, budgets=(1,), time_limit_s=1)
    assert not proposals and not telemetry["solves"][0]["accepted"]


def test_host_boundary_contradiction_blocks_before_solver_or_candidate_build(monkeypatch):
    problem = problem_sample()
    problem = replace(problem, host_envelope=host_sample(outer_mm=problem.demand.bbox))
    monkeypatch.setattr("rebar.optimization.algorithms.composite_pool.solve_finite_cover_front",
                        lambda *a, **kw: pytest.fail("solver must not hide an engineering contradiction"))
    result = solve_composite_pool(problem)
    assert not result.points and result.selected_index is None
    assert result.telemetry["host_demand_feasibility"]["requires_engineering_decision"]
    assert result.telemetry["candidate_count"] == 0 and not result.telemetry["solver_executed"]


def test_host_aware_search_is_still_research_only_with_unknown_depth():
    pytest.importorskip("scipy")
    problem = replace(problem_sample(), host_envelope=host_sample())
    result = solve_composite_pool(problem, maximum_candidates=16, maximum_zones=4)
    assert result.points and not result.placement_eligible
    assert result.telemetry["host_demand_feasibility"]["status"] == "necessary_condition_passed"


def test_opening_rejects_candidates_even_when_longitudinal_condition_passes():
    problem = replace(problem_sample(), host_envelope=host_sample(openings_mm=((0, -100, 3900, 1000),)))
    result = solve_composite_pool(problem, maximum_candidates=16)
    assert not result.points and result.telemetry["host_rejected_candidates"] > 0
    assert not result.placement_eligible
