"""Finite proposal recombination retains all V3 budgets and hard checks."""
from dataclasses import replace
from time import perf_counter
from types import SimpleNamespace

import pytest

from rebar.models import Axis
from rebar.optimization.algorithms.composite_merge import solve_composite_merge
from rebar.optimization.algorithms.composite_recombine import (
    _BoundedProposalPool, _whole_fe_coverage, solve_composite_recombine,
)
from rebar.models import Rebar, ReinforcementRecipe
from rebar.optimization.contracts.problem import DemandLevel
from rebar.optimization.services.composite_coverage import monotone_component_recipe_covers
from rebar.optimization.contracts.host import RectangularHostEnvelope
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from test_composite_merge import _problem


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_recombine_keeps_old_budget_envelope_and_full_checker(axis):
    problem = _problem(axis)
    options = dict(maximum_zones=9, maximum_points=3, time_limit_s=2,
                   neighbor_polish=True, gap_hierarchy=True, polish_time_limit_s=1)
    baseline = solve_composite_merge(problem, **options)
    progress = []
    result = solve_composite_recombine(problem, **options, recombine_time_limit_s=2,
                                       progress_callback=lambda done, total: progress.append((done, total)))
    assert result.telemetry["recombine"]["status"] == "completed"
    assert result.telemetry["recombine"]["baseline_points"] == len(baseline.points)
    assert len(result.points) <= 5
    assert progress == sorted(progress) and progress[-1] == (5, 5)
    assert all(len(p.zones) <= 9 for p in result.points)
    for old in baseline.points:
        assert any(len(new.zones) <= len(old.zones)
                   and new.coverage.additional_mass_kg <= old.coverage.additional_mass_kg + 1e-6
                   for new in result.points)
    for point in result.points:
        assert len({zone.id for zone in point.zones}) == len(point.zones)
        checked = evaluate_composite_coverage(problem.demand, point.zones,
            policy_id=problem.policy_id, constraints=problem.constraints)
        assert checked == point.coverage and checked.status == "pass"


def test_pool_uses_stronger_recipe_for_whole_weak_fe_but_never_sums_weak_zones():
    problem = _problem()
    captured = []
    solve_composite_merge(problem, maximum_zones=9, _proposal_observer=lambda z, check: captured.append((z, check)))
    assert captured
    weak_cell = next(cell for cell in problem.demand.cells if cell.level_index == 1)
    weak_problem = replace(problem, demand=replace(problem.demand, cells=(weak_cell,)))
    strong = [(z, check) for z, check in captured if z.level_index == 2]
    active, _, reason = _whole_fe_coverage(weak_problem, strong, perf_counter() + 2, 10000)
    assert reason is None and active
    assert any(0 in coverage for _, _, coverage in active)

    strong_cell = next(cell for cell in problem.demand.cells if cell.level_index == 2)
    strong_problem = replace(problem, demand=replace(problem.demand, cells=(strong_cell,)))
    weak = [(z, check) for z, check in captured if z.level_index == 1]
    active, _, reason = _whole_fe_coverage(strong_problem, weak, perf_counter() + 2, 10000)
    assert not active and reason == "uncoverable_whole_fe"


@pytest.mark.parametrize("limit,reason", (("maximum_pool_zones", "pinned_baseline_exceeds_pool_limit"),
                                          ("maximum_incidence_nnz", "incidence_limit"),
                                          ("maximum_prep_s", "prep_timeout")))
def test_bounded_failures_return_exact_baseline(limit, reason):
    problem = _problem()
    options = dict(maximum_zones=9, time_limit_s=2, neighbor_polish=False, gap_hierarchy=True)
    baseline = solve_composite_merge(problem, **options)
    result = solve_composite_recombine(problem, **options, **{limit: 1e-9 if limit == "maximum_prep_s" else 1})
    assert result.points == baseline.points
    assert result.telemetry["recombine"]["status"] == "fallback"
    assert result.telemetry["recombine"]["fallback_reason"] == reason


def test_solver_error_is_bounded_fallback_not_partial_result(monkeypatch):
    def failed(*args, **kwargs):
        raise RuntimeError("synthetic solver failure")

    monkeypatch.setattr("rebar.optimization.algorithms.composite_recombine.solve_finite_cover_front", failed)
    problem = _problem()
    baseline = solve_composite_merge(problem, maximum_zones=9, gap_hierarchy=True)
    result = solve_composite_recombine(problem, maximum_zones=9, gap_hierarchy=True,
                                       neighbor_polish=False)
    assert result.points == baseline.points
    assert result.telemetry["recombine"]["fallback_reason"] == "solver_error"
    assert result.telemetry["recombine"]["solver_error_type"] == "RuntimeError"


def test_empty_demand_and_length_limit_never_publish_false_solution():
    problem = _problem()
    empty = replace(problem, demand=replace(problem.demand,
        cells=tuple(replace(cell, level_index=0, aci=0) for cell in problem.demand.cells)))
    result = solve_composite_recombine(empty, maximum_zones=9)
    assert len(result.points) == 1 and result.points[0].zones == ()
    assert result.telemetry["recombine"]["fallback_reason"] == "empty_demand"

    limited = solve_composite_recombine(problem, maximum_zones=9, maximum_bar_length_mm=1000)
    assert limited.points == ()
    assert limited.telemetry["recombine"]["fallback_reason"] == "no_baseline_front"

    host = RectangularHostEnvelope((0, 0, 1800, 1800), (), 0, 300, 25, 25, 25, "synthetic")
    impossible_host = solve_composite_recombine(_problem(host=host), maximum_zones=9)
    assert impossible_host.points == ()
    assert impossible_host.telemetry["recombine"]["fallback_reason"] == "no_baseline_front"


def test_invalid_recombine_budget_is_input_error():
    with pytest.raises(ValueError, match="maximum_pool_zones"):
        solve_composite_recombine(_problem(), maximum_pool_zones=0)


def test_telemetry_runtime_includes_baseline_and_recombine_fallback(monkeypatch):
    ticks = iter((10.0, 12.0, 13.0))
    monkeypatch.setattr("rebar.optimization.algorithms.composite_recombine.perf_counter", lambda: next(ticks))
    result = solve_composite_recombine(_problem(), maximum_zones=9, maximum_pool_zones=1,
                                       neighbor_polish=False)
    assert result.telemetry["runtime_s"] == 3.0
    assert result.telemetry["recombine"]["elapsed_s"] == 1.0
    assert "finite_whole_fe_proposal_recombination" in result.telemetry["scope"]


def test_stable_bottom_k_is_order_independent_and_pins_exact_baseline_pair():
    pairs = []
    for x in range(100):
        bbox = (float(x), 0.0, float(x + 1), 1.0)
        pairs.append((SimpleNamespace(demand_bbox=bbox, level_index=1),
                      SimpleNamespace(component_service_bboxes_mm=(bbox,), additional_mass_kg=10.0)))
    first, reversed_stream = _BoundedProposalPool(7), _BoundedProposalPool(7)
    for zone, check in pairs:
        first.offer(zone, check)
    for zone, check in reversed(pairs):
        reversed_stream.offer(zone, check)
    assert len(first.items) == len(reversed_stream.items) == 7
    assert set(first.items) == set(reversed_stream.items)
    assert first.limited and first.replacements > 0
    assert first.observed == 100 and len(first.heap) == 7

    # A baseline zone with the same bbox/level but a different service box is
    # not interchangeable with a sampled cheaper candidate.
    original_zone, original_check = pairs[-1]
    baseline_check = SimpleNamespace(component_service_bboxes_mm=((99.0, 0.0, 99.5, 1.0),),
                                     additional_mass_kg=11.0)
    point = SimpleNamespace(zones=(original_zone,), coverage=SimpleNamespace(zones=(baseline_check,)))
    assert first.pin((point,))
    key = first._key(original_zone, baseline_check)
    assert first.items[key] == (original_zone, baseline_check)
    assert first.pinned_count == 1 and len(first.items) == 7
    assert len(first.heap) == 6


def test_vector_whole_fe_incidence_equals_independent_scalar_for_components_and_tolerance():
    base = _problem()
    background = Rebar(300, 10)
    composite = ReinforcementRecipe(background, (Rebar(300, 10), Rebar(150, 12)))
    level = DemandLevel(3, 3, None, None, 'two sets', None, True, composite)
    cells = list(base.demand.cells)
    cells[0] = replace(cells[0], level_index=3, aci=3)
    demand = replace(base.demand, levels=(*base.demand.levels, level), cells=tuple(cells))
    problem = replace(base, demand=demand)

    def proposal(level_index, *service_boxes):
        zone = SimpleNamespace(level_index=level_index, recipe=demand.level(level_index).recipe)
        check = SimpleNamespace(component_service_bboxes_mm=service_boxes, additional_mass_kg=1.0)
        return zone, check

    full = (0.0, 0.0, 1800.0, 1800.0)
    near = (0.0, 0.0, 600.0 - 0.5e-6, 600.0)
    partial = (0.0, 0.0, 600.0 - 2e-6, 600.0)
    proposals = (proposal(1, full), proposal(2, full), proposal(3, full, full),
                 proposal(3, near, near), proposal(3, partial, partial))

    def scalar(zone, check):
        served = []
        for i, cell in enumerate(c for c in demand.cells if demand.level(c.level_index).requires_extra):
            required = demand.level(cell.level_index).recipe
            if not monotone_component_recipe_covers(required, zone.recipe):
                continue
            boxes = check.component_service_bboxes_mm[:len(required.additions)]
            if len(boxes) != len(required.additions):
                continue
            service = (max(b[0] for b in boxes), max(b[1] for b in boxes),
                       min(b[2] for b in boxes), min(b[3] for b in boxes))
            bbox = (min(x for x, _ in cell.poly), min(y for _, y in cell.poly),
                    max(x for x, _ in cell.poly), max(y for _, y in cell.poly))
            if (service[0] <= bbox[0] + 1e-6 and service[1] <= bbox[1] + 1e-6
                    and service[2] >= bbox[2] - 1e-6 and service[3] >= bbox[3] - 1e-6):
                served.append(i)
        return frozenset(served)

    expected = [scalar(*pair) for pair in proposals]
    active, _, reason = _whole_fe_coverage(problem, proposals, perf_counter() + 2, 1000)
    assert reason is None
    assert [entry[2] for entry in active] == [mask for mask in expected if mask]
    assert 0 in expected[-2] and not expected[-1]
    assert 0 not in expected[0] and 0 not in expected[1]  # no weak-set summation
