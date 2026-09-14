"""Независимый полный перебор и инженерские контрпримеры для exact-oracle."""

import itertools
from dataclasses import replace

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar
from rebar.application.genetic_oracle import small_oracle_problems
from rebar.optimization import AlgorithmRequest, ComplexityAxis, LayoutConstraints, build_layout_problem
from rebar.optimization.algorithms.genetic.exact_oracle import solve_exact_candidate_front
from rebar.optimization.algorithms.genetic_pareto import (
    _build_search_space,
    _materialize_genome,
    _PoolCandidate,
)
from rebar.optimization.services import build_zone_from_bbox, evaluate_layout, prepare_detailing


def _native_space(problem, request):
    return _build_search_space(
        problem, request, candidate_window=6, candidate_trajectories=3,
        layer_bridge_span=6, maximum_merge_reduction=12, maximum_pool_merges=5000,
        baseline_seed_algorithms=("agglomerative", "bsp", "greedy-priority"),
        random_seed=7, deadline=None,
    )


def _single_demand_problem(axis=Axis.X):
    background = Rebar(300, 12)
    bands = [
        Band(0, 181, "base", 3.77, background, None),
        Band(1, 254, "extra", 15.08, background, Rebar(100, 12)),
        Band(2, 2, "strong", 35.19, background, Rebar(100, 20)),
    ]
    cells = [
        Cell([(0, 0), (1000, 0), (1000, 1000), (0, 1000)], (500, 500), 254, bands[1]),
        Cell([(1000, 0), (2000, 0), (2000, 1000), (1000, 1000)], (1500, 500), 181, bands[0]),
    ]
    return build_layout_problem(
        Mosaic(Direction(Layer.BOTTOM, axis), cells, bands, (0, 0, 2000, 1000)),
        LayoutConstraints(min_width_cells=1),
    )


def _space_with_zones(problem, zones):
    space = _native_space(problem, AlgorithmRequest())
    template = space.candidates[0].rectangle
    return replace(
        space,
        candidates=tuple(
            _PoolCandidate(
                rectangle=replace(template, zone=zone, level_index=zone.level_index),
                # Deliberately false leaf metadata: oracle must not use GA's coverage shortcut.
                leaf_ids=frozenset(), source_cell_ids=(), origins=frozenset(("test",)),
            )
            for zone in zones
        ),
        baseline_seed_genomes=(), seed_genomes=(),
    )


@pytest.mark.parametrize("axis", list(ComplexityAxis))
@pytest.mark.parametrize("case_index", [0, 1, 2, 6])
def test_oracle_matches_unpruned_brute_force(case_index, axis):
    problem = small_oracle_problems()[case_index]
    request = AlgorithmRequest(max_details=len(problem.demand.cells))
    space = _native_space(problem, request)
    context = prepare_detailing(problem)
    points = set()

    def independent_complexity(zones):
        if axis is ComplexityAxis.ZONE_COUNT:
            return len(zones)
        if axis is ComplexityAxis.PHYSICAL_BAR_COUNT:
            return sum(zone.bar_count for zone in zones)
        # Independent grouping: do not call the optimizer's objective helper.
        return len({(zone.rebar.diameter, round(zone.installed_length_mm, 6)) for zone in zones})

    # No coverage masks, objective pruning, GA repair or oracle dominance routine here.
    for count in range(request.max_details + 1):
        for indexes in itertools.combinations(range(len(space.candidates)), count):
            zones, _diagnostic = _materialize_genome(problem, space, frozenset(indexes), context)
            evaluation = evaluate_layout(problem, zones, request)
            if evaluation.valid:
                complexity = independent_complexity(zones)
                points.add((complexity, evaluation.metrics.total_mass_kg))
    expected = sorted(
        point for point in points
        if not any(
            other[0] <= point[0] and other[1] <= point[1]
            and (other[0] < point[0] or other[1] < point[1])
            for other in points
        )
    )

    result = solve_exact_candidate_front(problem, space, request, complexity_axis=axis)

    actual = [
        (
            independent_complexity(solution.zones),
            solution.metrics.total_mass_kg,
        )
        for solution in result.solutions
    ]
    assert len(actual) == len(expected)
    for point, reference in zip(actual, expected):
        assert point == pytest.approx(reference, rel=0, abs=1e-6)
    assert result.complete
    assert result.scope == "finite_candidate_set_with_deterministic_detailing"
    assert result.subset_count == (
        result.evaluated_count + result.coverage_pruned_count + result.objective_pruned_count
    )
    assert result.coverage_pruned_count > 0
    assert result.objective_pruned_count > 0
    assert all(evaluate_layout(problem, solution.zones, request).valid for solution in result.solutions)


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
@pytest.mark.parametrize("overlap", [False, True])
@pytest.mark.parametrize("cuts", [(), (1600.0, 2200.0)])
def test_oracle_validates_union_of_partial_fe_coverage(axis, overlap, cuts):
    problem = _single_demand_problem(axis)
    problem = replace(problem, constraints=replace(problem.constraints, allowed_cut_lengths_mm=cuts))
    boxes = [(0, 0, 600 if overlap else 500, 1000), (400 if overlap else 500, 0, 1000, 1000)]
    zones = tuple(
        build_zone_from_bbox(problem, box, 1, f"part-{index}")
        for index, box in enumerate(boxes)
    )
    space = _space_with_zones(problem, zones)
    result = solve_exact_candidate_front(problem, space, AlgorithmRequest())

    assert len(result.solutions) == 1
    assert result.solutions[0].metrics.detail_count == 2
    assert result.solutions[0].metrics.under_reinforced_cell_count == 0
    assert result.solutions[0].metrics.total_mass_kg == pytest.approx(sum(z.mass_kg for z in zones))
    strict = replace(problem, constraints=replace(problem.constraints, allow_overlaps=False))
    strict_result = solve_exact_candidate_front(strict, space, AlgorithmRequest())
    assert bool(strict_result.solutions) is not overlap
    limited = solve_exact_candidate_front(problem, space, AlgorithmRequest(max_details=1))
    assert limited.complete and limited.solutions == ()


def test_oracle_rejects_incomplete_geometric_union_despite_touching_every_fe():
    problem = _single_demand_problem()
    zone = build_zone_from_bbox(problem, (0, 0, 500, 1000), 1, "half")
    space = _space_with_zones(problem, (zone, replace(zone, id="same-half")))

    result = solve_exact_candidate_front(problem, space, AlgorithmRequest())

    assert result.complete and result.solutions == ()
    assert result.rejected_count == 3


def test_oracle_does_not_sum_two_weak_zones():
    problem = _single_demand_problem()
    problem = replace(problem, demand=replace(problem.demand, cells=(
        replace(problem.demand.cells[0], level_index=2), problem.demand.cells[1],
    )))
    weak = build_zone_from_bbox(problem, (0, 0, 1000, 1000), 1, "weak")
    space = _space_with_zones(problem, (weak, replace(weak, id="another-weak")))

    result = solve_exact_candidate_front(problem, space, AlgorithmRequest())

    assert result.complete and result.solutions == ()
    assert result.coverage_pruned_count == result.subset_count


def test_oracle_applies_a101_hard_prohibitions():
    problem = _single_demand_problem()
    levels = list(problem.demand.levels)
    prohibited = Rebar(100, 32)
    levels[1] = replace(levels[1], additional=prohibited,
                        recipe=replace(levels[1].recipe, additions=(prohibited,)))
    problem = replace(problem, demand=replace(
        problem.demand, levels=tuple(levels),
        meta={"rebar_mapping": {"a101_profile_id": "a101-2.4.2-foundation-parking-t450-550-v1"}},
    ))
    zone = build_zone_from_bbox(problem, (0, 0, 1000, 1000), 1, "prohibited")
    space = _space_with_zones(problem, (zone,))

    result = solve_exact_candidate_front(problem, space, AlgorithmRequest())

    assert result.complete and result.solutions == ()
    assert result.rejected_count == 1


def test_oracle_handles_empty_demand_and_empty_pool():
    problem = _single_demand_problem()
    problem = replace(problem, demand=replace(problem.demand, cells=(problem.demand.cells[1],)))
    space = _native_space(problem, AlgorithmRequest())

    result = solve_exact_candidate_front(problem, space, AlgorithmRequest())

    assert result.complete and result.subset_count == 1
    assert len(result.solutions) == 1
    assert result.solutions[0].zones == ()
    assert result.solutions[0].metrics.total_mass_kg == 0


@pytest.mark.parametrize("limits", [{"max_candidates": 1}, {"max_subsets": 1}])
def test_oracle_refuses_oversized_search_before_validation(limits, monkeypatch):
    problem = small_oracle_problems()[0]
    request = AlgorithmRequest()
    space = _native_space(problem, request)

    def forbidden(*args, **kwargs):
        pytest.fail("oracle must refuse before evaluating any subset")

    monkeypatch.setattr(
        "rebar.optimization.algorithms.genetic.exact_oracle.evaluate_layout", forbidden,
    )
    with pytest.raises(ValueError, match="max_candidates|max_subsets"):
        solve_exact_candidate_front(problem, space, request, **limits)


def test_oracle_rejects_time_limits_and_untrusted_candidate_cost():
    problem = small_oracle_problems()[0]
    space = _native_space(problem, AlgorithmRequest())
    with pytest.raises(ValueError, match="time_limit_s"):
        solve_exact_candidate_front(problem, space, AlgorithmRequest(time_limit_s=1))
    with pytest.raises(ValueError, match="остановлен по времени"):
        solve_exact_candidate_front(
            problem, replace(space, stopped_by_time_limit=True), AlgorithmRequest(),
        )
    candidate = space.candidates[0]
    altered = replace(candidate, rectangle=replace(
        candidate.rectangle, zone=replace(candidate.rectangle.zone, mass_kg=1e10),
    ))
    with pytest.raises(ValueError, match="стоимость CandidateSet"):
        solve_exact_candidate_front(
            problem, replace(space, candidates=(altered, *space.candidates[1:])), AlgorithmRequest(),
        )
