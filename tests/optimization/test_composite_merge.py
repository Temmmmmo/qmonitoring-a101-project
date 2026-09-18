"""Small complete-demand cases for the independent bottom-up zone search."""
from copy import deepcopy
from dataclasses import replace

import pytest

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.algorithms.composite_merge import solve_composite_merge
from rebar.optimization.contracts.composite_coverage import MONOTONE_COMPONENT_STO_COVERAGE_POLICY
from rebar.optimization.contracts.composite_search import CompositeSearchProblem
from rebar.optimization.contracts.host import RectangularHostEnvelope
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutConstraints
from rebar.optimization.services.axis_patterns import a101_sto_279_slab_recipe_placement
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_windows import covering_composite_window
from rebar.optimization.services.zone_tradeoff import combine_zone_candidates, recommend_zone_knee


def _problem(axis=Axis.X, *, host=None, constraints=None):
    background = Rebar(300, 10)
    recipes = (ReinforcementRecipe(background),
               ReinforcementRecipe(background, (Rebar(300, 10),)),
               ReinforcementRecipe(background, (Rebar(150, 12),)))
    levels = tuple(DemandLevel(i, i, None, None, str(i),
                  recipe.additions[0] if len(recipe.additions) == 1 else None,
                  bool(recipe.additions), recipe) for i, recipe in enumerate(recipes))
    cells = []
    strong = {(0, 0), (2, 2)}
    weak = {(1, 0), (1, 1), (0, 2)}
    for y in range(3):
        for x in range(3):
            level = 2 if (x, y) in strong else 1 if (x, y) in weak else 0
            points = ((x * 600, y * 600), ((x + 1) * 600, y * 600),
                      ((x + 1) * 600, (y + 1) * 600), (x * 600, (y + 1) * 600))
            centroid = ((x + 0.5) * 600, (y + 0.5) * 600)
            if axis is Axis.Y:
                points = tuple((b, a) for a, b in points)
                centroid = centroid[::-1]
            cells.append(DemandCell(1 + y * 3 + x, points, centroid, level, level))
    demand = DemandMap(Direction(Layer.TOP, axis), levels, tuple(cells), (0, 0, 1800, 1800))
    placements = []
    for index, recipe in enumerate(recipes[1:], 1):
        placement = a101_sto_279_slab_recipe_placement(recipe, background_origin_mm=0, contact_side="left")
        placement = replace(placement, additions=tuple(
            replace(axes, origin_mm=100) if spec.step == 300 else axes
            for spec, axes in zip(recipe.additions, placement.additions)))
        placements.append((index, placement))
    return CompositeSearchProblem(demand, tuple(placements), constraints or LayoutConstraints(min_width_cells=1),
                                  MONOTONE_COMPONENT_STO_COVERAGE_POLICY, host)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_independent_merge_search_retains_multiple_valid_zone_mass_points(axis):
    problem = _problem(axis)
    original = deepcopy(problem)
    result = solve_composite_merge(problem, maximum_zones=9, time_limit_s=5)
    assert problem == original
    assert result.telemetry["source_demand_preserved"]
    assert result.telemetry["initial_cell_count"] == 5
    assert len(result.points) >= 3
    counts = [len(point.zones) for point in result.points]
    masses = [point.coverage.additional_mass_kg for point in result.points]
    assert counts == sorted(set(counts))
    assert all(a > b for a, b in zip(masses, masses[1:]))
    assert any(count > 1 for count in counts)
    for point in result.points:
        assert len({zone.id for zone in point.zones}) == len(point.zones)
        checked = evaluate_composite_coverage(problem.demand, point.zones,
            policy_id=problem.policy_id, constraints=problem.constraints)
        assert checked == point.coverage
        assert checked.status == "pass" and checked.covered_cell_count == 5
        assert checked.uncovered_area_mm2 == 0
    assert not result.placement_eligible


def test_no_partial_answer_when_tree_time_budget_expires(monkeypatch):
    problem = _problem()
    ticks = iter((0.0, 100.0, 200.0, 300.0))
    monkeypatch.setattr("rebar.optimization.algorithms.composite_merge.perf_counter", lambda: next(ticks, 400.0))
    result = solve_composite_merge(problem, maximum_zones=9, time_limit_s=0.01)
    assert result.telemetry["timed_out"]
    assert len(result.points) == 1
    assert len(result.points[0].zones) == 1
    assert result.points[0].coverage.status == "pass"


def test_stock_length_limit_does_not_publish_invalid_point():
    problem = _problem()
    result = solve_composite_merge(problem, maximum_bar_length_mm=1000, time_limit_s=5)
    assert result.points == () and result.selected_index is None


def test_incompatible_host_returns_no_false_coverage():
    host = RectangularHostEnvelope((0, 0, 1800, 1800), (), 0, 300, 25, 25, 25, "synthetic")
    result = solve_composite_merge(_problem(host=host), time_limit_s=5)
    assert result.points == () and result.selected_index is None
    assert result.telemetry["host_demand_feasibility"]["requires_engineering_decision"]


def test_requires_explicit_phase_for_every_extra_level():
    problem = _problem()
    broken = replace(problem, placements=problem.placements[:-1])
    with pytest.raises(ValueError, match="фазы всех дополнительных"):
        solve_composite_merge(broken)


def test_geometric_knee_is_a_candidate_only_for_clear_bend():
    curve = ((1, 100.0), (2, 60.0), (3, 50.0), (4, 45.0), (5, 42.0))
    knee = recommend_zone_knee(curve)
    assert knee["status"] == "candidate" and knee["index"] == 1
    assert knee["coarser"]["kg_per_extra_zone"] == 40
    assert knee["finer"]["kg_per_extra_zone"] == 10
    assert not knee["engineering_optimality_proven"]
    assert recommend_zone_knee(((1, 100.0), (2, 90.0)))["status"] == "insufficient_tradeoff"
    assert recommend_zone_knee(((1, 100.0), (2, 90.0), (3, 80.0)))["status"] == "no_distinct_knee"


def test_plate_tradeoff_dp_agrees_with_tiny_exhaustive_oracle():
    groups = (((1, 40.0), (2, 28.0), (3, 25.0)),
              ((1, 35.0), (2, 24.0), (3, 20.0)),
              ((1, 20.0), (2, 18.0)))
    import itertools

    exhaustive = {}
    for combination in itertools.product(*groups):
        count = sum(item[0] for item in combination)
        mass = sum(item[1] for item in combination)
        exhaustive[count] = min(exhaustive.get(count, float("inf")), mass)
    oracle = []
    best = float("inf")
    for count, mass in sorted(exhaustive.items()):
        if mass < best:
            oracle.append((count, mass))
            best = mass
    result = combine_zone_candidates(groups, count_of=lambda item: item[0], mass_of=lambda item: item[1])
    assert [(sum(item[0] for item in solution), sum(item[1] for item in solution))
            for solution in result] == oracle


def test_two_cell_solver_matches_independently_enumerated_partition_oracle():
    full = _problem()
    cells = (full.demand.cells[0], full.demand.cells[-1])
    demand = replace(full.demand, cells=cells)
    problem = replace(full, demand=demand)
    placement = dict(problem.placements)[2]

    def zone_for(selected_cells, identifier):
        points = [point for cell in selected_cells for point in cell.poly]
        bounds = (min(p[0] for p in points), min(p[1] for p in points),
                  max(p[0] for p in points), max(p[1] for p in points))
        window = covering_composite_window(demand, bounds, 2, placement, constraints=problem.constraints)
        return build_composite_zone(demand, window, 2, identifier, placement, constraints=problem.constraints)

    oracle = []
    for zones in ((zone_for(cells, "all"),),
                  (zone_for(cells[:1], "first"), zone_for(cells[1:], "second"))):
        checked = evaluate_composite_coverage(demand, zones, policy_id=problem.policy_id,
                                              constraints=problem.constraints)
        assert checked.status == "pass"
        oracle.append((len(zones), checked.additional_mass_kg))
    solution = solve_composite_merge(problem, maximum_zones=2)
    assert [(len(point.zones), point.coverage.additional_mass_kg) for point in solution.points] == oracle


def test_opt_in_neighbor_polishing_improves_both_zone_count_and_mass():
    base = _problem()
    levels = (2, 0, 1, 1, 1, 2, 2, 1, 1)
    demand = replace(base.demand, cells=tuple(replace(cell, level_index=level, aci=level)
        for cell, level in zip(base.demand.cells, levels)))
    problem = replace(base, demand=demand)
    baseline = solve_composite_merge(problem, maximum_zones=9, maximum_points=32)
    polished = solve_composite_merge(problem, maximum_zones=9, maximum_points=32,
                                      neighbor_polish=True, polish_time_limit_s=1)
    assert [(len(point.zones), point.coverage.additional_mass_kg) for point in baseline.points] == [
        (1, pytest.approx(29.4026112)), (7, pytest.approx(28.0857672))]
    assert [(len(point.zones), point.coverage.additional_mass_kg) for point in polished.points] == [
        (1, pytest.approx(29.4026112)), (6, pytest.approx(27.8391672))]
    stats = polished.telemetry["neighbor_polish"]
    assert stats["accepted_merges"] >= 1 and stats["validated_trajectory_count"] >= 1
    assert stats["baseline_front"] != stats["polished_front"]
    assert "neighbor_unions" in polished.telemetry["scope"]
    for point in polished.points:
        checked = evaluate_composite_coverage(demand, point.zones, policy_id=problem.policy_id,
                                              constraints=problem.constraints)
        assert checked == point.coverage and checked.status == "pass"


def test_polishing_timeout_keeps_original_front():
    problem = _problem()
    baseline = solve_composite_merge(problem, maximum_zones=9)
    timed = solve_composite_merge(problem, maximum_zones=9, neighbor_polish=True,
                                  polish_time_limit_s=1e-9)
    assert timed.points == baseline.points
    assert timed.telemetry["neighbor_polish"]["validated_trajectory_count"] == 0


def test_polishing_reuses_pair_after_unique_evaluation_budget_is_exhausted():
    base = _problem()
    levels = (0, 2, 1, 1, 1, 1, 1, 1, 2)
    cells = tuple(replace(cell, level_index=level, aci=level)
                  for cell, level in zip(base.demand.cells, levels))
    problem = replace(base, demand=replace(base.demand, cells=cells))
    result = solve_composite_merge(problem, maximum_zones=9, maximum_points=32,
        neighbor_polish=True, polish_max_evaluations=10)
    stats = result.telemetry["neighbor_polish"]
    assert stats["evaluated_pairs"] == 10 and stats["cache_hits"] > 0
    assert len(stats["candidate_trajectory"]) >= 2
    assert all(row["evaluated_pairs_at_accept"] == 10 for row in stats["candidate_trajectory"][:2])
    assert stats["validated_trajectory_count"] >= 2
    assert all(point.coverage.status == "pass" for point in result.points)
