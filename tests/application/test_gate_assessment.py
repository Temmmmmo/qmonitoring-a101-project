"""Проверки измеримых гейтов результата и честных отклонений от эталона."""

from __future__ import annotations

import math
from dataclasses import replace

from rebar import Rebar
from rebar.application import assess_layout_gates, assess_plate_gates
from rebar.application.revit_export import build_plate_solution_revit_export
from rebar.golden import PLATE_ZERO_K09
from rebar.optimization import (
    PLATE_DIRECTIONS,
    AlgorithmRequest,
    LayoutConstraints,
    PlateDirectionSolution,
    build_layout_problem,
    build_plate_problem,
    build_plate_solution,
    built_in_optimizer_registry,
)
from rebar.standards import (
    A101_242_FOUNDATION_PARKING_T450_550,
    A101_244_ATS3_ZERO_T240,
)


def _direction_solution(mosaic, direction):
    problem = build_layout_problem(
        replace(mosaic, direction=direction),
        LayoutConstraints(min_width_cells=1, enforce_zone_gap=False),
    )
    solution = built_in_optimizer_registry().create("bbox").solve(
        problem,
        AlgorithmRequest(max_details=1),
    )
    return problem, solution


def _with_profile(direction_mosaic, *, additional, profile_id):
    weak, original_strong = direction_mosaic.legend
    strong = replace(
        original_strong,
        label=f"s300d18+s{additional.step}d{additional.diameter}",
        additional=additional,
    )
    return replace(
        direction_mosaic,
        cells=[
            replace(direction_mosaic.cells[0], band=weak),
            replace(direction_mosaic.cells[1], band=strong),
        ],
        legend=[weak, strong],
        meta={**direction_mosaic.meta, "a101_profile_id": profile_id},
    )


def test_layout_gate_assessment_separates_checked_and_missing_catalog(direction_mosaic):
    problem, solution = _direction_solution(
        direction_mosaic,
        direction_mosaic.direction,
    )

    assessment = assess_layout_gates(problem, solution)
    by_id = {item.id: item for item in assessment.items}

    assert by_id["demand-coverage"].status == "pass"
    assert by_id["demand-coverage"].actual == 100.0
    assert by_id["anchorage"].target == 40.0
    assert by_id["anchorage"].absolute_deviation == 0.0
    assert by_id["step-multiple"].status == "pass"
    assert by_id["a101-allowed-positions"].status == "not_checked"
    assert assessment.summary == {
        "pass": 5,
        "fail": 0,
        "warning": 0,
        "not_checked": 2,
    }


def test_disabled_gap_check_is_unchecked_even_without_diagnostics(direction_mosaic):
    problem, solution = _direction_solution(direction_mosaic, direction_mosaic.direction)
    solution = replace(solution, diagnostics=())

    item = next(item for item in assess_layout_gates(problem, solution).items
                if item.id == "postprocessing-conflicts")

    assert item.status == "not_checked"
    assert item.actual is None
    assert item.absolute_deviation is None


def test_enabled_gap_check_can_pass_for_single_valid_zone(direction_mosaic):
    problem = build_layout_problem(direction_mosaic, LayoutConstraints(min_width_cells=1))
    solution = built_in_optimizer_registry().create("bbox").solve(problem, AlgorithmRequest(max_details=1))

    item = next(item for item in assess_layout_gates(problem, solution).items
                if item.id == "postprocessing-conflicts")

    assert item.status == "pass"
    assert item.actual == 0


def test_reported_conflict_is_not_hidden_by_disabled_check(direction_mosaic):
    problem, solution = _direction_solution(direction_mosaic, direction_mosaic.direction)
    solution = replace(solution, diagnostics=("WARNING: зоны конфликтуют после детализации",))

    item = next(item for item in assess_layout_gates(problem, solution).items
                if item.id == "postprocessing-conflicts")

    assert item.status == "warning"
    assert item.actual == 1


def test_one_disabled_direction_prevents_plate_gap_pass(direction_mosaic):
    results = []
    for index, direction in enumerate(PLATE_DIRECTIONS):
        problem = build_layout_problem(
            replace(direction_mosaic, direction=direction),
            LayoutConstraints(min_width_cells=1, enforce_zone_gap=index != 0),
        )
        solution = built_in_optimizer_registry().create("bbox").solve(problem, AlgorithmRequest(max_details=1))
        results.append((problem, replace(solution, diagnostics=())))
    problem = build_plate_problem(p for p, _ in results)
    solution = build_plate_solution(
        PlateDirectionSolution(direction=p.demand.direction, solution=s) for p, s in results
    )

    item = next(item for item in assess_plate_gates(problem, solution).items
                if item.id == "postprocessing-conflicts")

    assert item.status == "not_checked"
    assert item.actual is None
    export = build_plate_solution_revit_export(problem, solution)
    assert export["checks"]["export_eligible"] is False
    assert "postprocessing-conflicts" in export["checks"]["blocking_check_ids"]


def test_plate_gate_assessment_reports_absolute_and_relative_golden_deviation(
    direction_mosaic,
):
    direction_results = tuple(
        _direction_solution(direction_mosaic, direction)
        for direction in PLATE_DIRECTIONS
    )
    problem = build_plate_problem(problem for problem, _ in direction_results)
    solution = build_plate_solution(
        PlateDirectionSolution(
            direction=problem.demand.direction,
            solution=direction_solution,
        )
        for problem, direction_solution in direction_results
    )

    assessment = assess_plate_gates(
        problem,
        solution,
        reference=PLATE_ZERO_K09,
    )
    by_id = {item.id: item for item in assessment.items}

    assert by_id["plate-directions"].status == "pass"
    assert by_id["plate-directions"].absolute_deviation == 0.0
    mass = by_id["reference-mass"]
    assert mass.target == PLATE_ZERO_K09.expected_mass_kg
    assert math.isclose(
        mass.absolute_deviation,
        solution.metrics.total_mass_kg - PLATE_ZERO_K09.expected_mass_kg,
    )
    assert math.isclose(
        mass.relative_deviation_pct,
        mass.absolute_deviation / mass.target * 100.0,
    )
    assert assessment.reference_id == "plate-zero-k09"


def test_catalog_gate_passes_position_listed_in_selected_profile(direction_mosaic):
    mosaic = _with_profile(
        direction_mosaic,
        additional=Rebar(step=100, diameter=16),
        profile_id=A101_244_ATS3_ZERO_T240.id,
    )
    problem, solution = _direction_solution(mosaic, mosaic.direction)

    assessment = assess_layout_gates(problem, solution)
    item = next(item for item in assessment.items if item.id == "a101-allowed-positions")

    assert solution.status.value == "feasible"
    assert item.status == "pass"
    assert item.actual == 100.0
    assert "разрешено: 1" in item.note


def test_catalog_gate_keeps_unlisted_recommendation_explicitly_unchecked(
    direction_mosaic,
):
    mosaic = _with_profile(
        direction_mosaic,
        additional=Rebar(step=100, diameter=20),
        profile_id=A101_244_ATS3_ZERO_T240.id,
    )
    problem, solution = _direction_solution(mosaic, mosaic.direction)

    assessment = assess_layout_gates(problem, solution)
    item = next(item for item in assessment.items if item.id == "a101-allowed-positions")

    assert solution.status.value == "feasible"
    assert item.status == "not_checked"
    assert item.actual == 0.0
    assert "нет в таблице: 1" in item.note


def test_explicit_red_a101_position_is_a_hard_error(direction_mosaic):
    mosaic = _with_profile(
        direction_mosaic,
        additional=Rebar(step=100, diameter=32),
        profile_id=A101_242_FOUNDATION_PARKING_T450_550.id,
    )
    problem, solution = _direction_solution(mosaic, mosaic.direction)

    assessment = assess_layout_gates(problem, solution)
    item = next(item for item in assessment.items if item.id == "a101-allowed-positions")

    assert solution.status.value == "error"
    assert any("⌀32/100 запрещена" in message for message in solution.diagnostics)
    assert item.status == "fail"
    assert item.actual == 0.0
    assert "запрещено красными строками: 1" in item.note
