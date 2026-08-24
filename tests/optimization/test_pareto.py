"""Тесты общего CandidateSet, метрик сложности и общеплитного Парето-фронта."""

from __future__ import annotations

from dataclasses import replace

from rebar.optimization import (
    PLATE_DIRECTIONS,
    AlgorithmRequest,
    BspOptimizer,
    ComplexityAxis,
    LayoutConstraints,
    SolutionStatus,
    StrongestBBoxOptimizer,
    build_direction_pareto_front,
    build_layout_problem,
    build_plate_pareto_front,
    build_plate_problem,
    measure_constructability,
)


def _direction_solutions(mosaic, direction):
    problem = build_layout_problem(
        replace(mosaic, direction=direction),
        LayoutConstraints(min_width_cells=1, enforce_zone_gap=False),
    )
    request = AlgorithmRequest(max_details=2)
    return problem, (
        StrongestBBoxOptimizer().solve(problem, request),
        BspOptimizer().solve(problem, request),
    )


def test_constructability_keeps_components_separate(splittable_mosaic):
    problem, solutions = _direction_solutions(
        splittable_mosaic,
        splittable_mosaic.direction,
    )
    solution = solutions[1]

    metrics = measure_constructability(solution)

    assert metrics.zone_count == solution.metrics.detail_count
    assert metrics.physical_bar_count == solution.metrics.physical_bar_count
    assert metrics.unique_diameter_count == len(
        {zone.rebar.diameter for zone in solution.zones}
    )
    assert metrics.unique_step_count == len(
        {zone.rebar.step for zone in solution.zones}
    )
    assert metrics.unique_installed_length_count == len(
        {zone.installed_length_mm for zone in solution.zones}
    )
    assert metrics.unique_layout_signature_count == len(
        {
            (zone.rebar.diameter, zone.rebar.step, zone.installed_length_mm)
            for zone in solution.zones
        }
    )
    assert metrics.warning_count == sum(
        diagnostic.startswith("WARNING:") for diagnostic in solution.diagnostics
    )
    assert metrics.error_count == 0
    assert problem.demand.direction == splittable_mosaic.direction


def test_direction_front_revalidates_rejects_and_collapses_equal_points(
    splittable_mosaic,
):
    problem, solutions = _direction_solutions(
        splittable_mosaic,
        splittable_mosaic.direction,
    )
    bbox, bsp = solutions
    equivalent_bbox = replace(bbox, algorithm="bbox-copy")
    invalid = replace(
        bbox,
        algorithm="invalid-empty",
        zones=(),
        status=SolutionStatus.FEASIBLE,
    )

    front = build_direction_pareto_front(
        problem,
        (bbox, bsp, equivalent_bbox, invalid),
    )

    assert front.source_candidate_count == 4
    assert len(front.rejections) == 1
    assert front.rejections[0].algorithm == "invalid-empty"
    assert any("недоармированы КЭ" in item for item in front.rejections[0].diagnostics)
    assert front.equivalent_candidate_count == 1
    assert front.dominated_candidate_count == 0
    assert len(front.candidates) == 2
    assert [candidate.constructability.zone_count for candidate in front.candidates] == [
        1,
        2,
    ]
    assert front.candidates[0].equivalent_candidate_ids
    assert front.candidates[1].solution.metrics.total_mass_kg < (
        front.candidates[0].solution.metrics.total_mass_kg
    )


def test_direction_front_can_use_physical_bars_as_explicit_axis(splittable_mosaic):
    problem, solutions = _direction_solutions(
        splittable_mosaic,
        splittable_mosaic.direction,
    )

    front = build_direction_pareto_front(
        problem,
        solutions,
        complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT,
    )

    assert front.complexity_axis is ComplexityAxis.PHYSICAL_BAR_COUNT
    assert all(
        candidate.constructability.value(front.complexity_axis)
        == candidate.solution.metrics.physical_bar_count
        for candidate in front.candidates
    )


def test_plate_front_combines_different_algorithms_across_four_directions(
    splittable_mosaic,
):
    problem_solutions = tuple(
        _direction_solutions(splittable_mosaic, direction)
        for direction in PLATE_DIRECTIONS
    )
    plate_problem = build_plate_problem(
        problem for problem, _solutions in problem_solutions
    )

    front = build_plate_pareto_front(
        plate_problem,
        {
            problem.demand.direction: solutions
            for problem, solutions in problem_solutions
        },
    )

    assert front.combination_count == 16
    assert front.candidates
    assert all(candidate.solution.valid for candidate in front.candidates)
    assert all(
        candidate.constructability.zone_count == candidate.solution.metrics.zone_count
        for candidate in front.candidates
    )
    assert any(
        len(set(candidate.solution.meta["algorithm_by_direction"].values())) > 1
        for candidate in front.candidates
    )
    for candidate in front.candidates:
        for other in front.candidates:
            if other is candidate:
                continue
            assert not (
                other.solution.metrics.total_mass_kg
                < candidate.solution.metrics.total_mass_kg - 1e-6
                and other.constructability.zone_count
                <= candidate.constructability.zone_count
            )
