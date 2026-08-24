"""Проверки измеримых гейтов результата и честных отклонений от эталона."""

from __future__ import annotations

import math
from dataclasses import replace

from rebar.application import assess_layout_gates, assess_plate_gates
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
        "not_checked": 1,
    }


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
