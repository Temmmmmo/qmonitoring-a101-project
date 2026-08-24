"""Тесты общеплитных контрактов и независимой агрегации метрик."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rebar import Axis, Direction, Layer
from rebar.optimization import (
    PLATE_DIRECTIONS,
    LayoutProblem,
    PlateDirectionSolution,
    SolutionStatus,
    StrongestBBoxOptimizer,
    build_layout_problem,
    build_plate_problem,
    build_plate_solution,
)


def _problems(mosaic_with_legend) -> tuple[LayoutProblem, ...]:
    return tuple(
        build_layout_problem(replace(mosaic_with_legend, direction=direction))
        for direction in PLATE_DIRECTIONS
    )


def _solutions(mosaic_with_legend) -> tuple[PlateDirectionSolution, ...]:
    return tuple(
        PlateDirectionSolution(
            direction=problem.demand.direction,
            solution=StrongestBBoxOptimizer().solve(problem),
        )
        for problem in _problems(mosaic_with_legend)
    )


def test_plate_problem_requires_and_canonicalizes_four_directions(mosaic_with_legend):
    problems = _problems(mosaic_with_legend)

    plate = build_plate_problem(reversed(problems), case_id="plate-1")

    assert plate.case_id == "plate-1"
    assert tuple(problem.demand.direction for problem in plate.direction_problems) == (
        PLATE_DIRECTIONS
    )
    assert plate.problem(Direction(Layer.TOP, Axis.Y)).demand.direction == Direction(
        Layer.TOP,
        Axis.Y,
    )


def test_plate_problem_rejects_missing_and_duplicate_directions(mosaic_with_legend):
    problems = _problems(mosaic_with_legend)

    with pytest.raises(ValueError, match=r"отсутствуют: top-Y"):
        build_plate_problem(problems[:-1])
    with pytest.raises(ValueError, match=r"направления повторяются: bottom-X"):
        build_plate_problem((*problems[:-1], problems[0]))


def test_plate_solution_aggregates_metrics_without_mixing_counts(mosaic_with_legend):
    direction_solutions = _solutions(mosaic_with_legend)

    plate = build_plate_solution(reversed(direction_solutions))

    assert plate.valid is True
    assert plate.status is SolutionStatus.FEASIBLE
    assert plate.metrics.direction_count == 4
    assert plate.metrics.zone_count == sum(
        item.solution.metrics.detail_count for item in direction_solutions
    )
    assert plate.metrics.physical_bar_count == sum(
        item.solution.metrics.physical_bar_count for item in direction_solutions
    )
    assert plate.metrics.total_mass_kg == pytest.approx(
        sum(item.solution.metrics.total_mass_kg for item in direction_solutions)
    )
    assert plate.metrics.total_bar_length_mm == pytest.approx(
        sum(item.solution.metrics.total_bar_length_mm for item in direction_solutions)
    )
    assert plate.solution(Direction(Layer.BOTTOM, Axis.Y)).algorithm == "bbox"


def test_plate_solution_rejects_under_reinforcement_in_any_direction(
    mosaic_with_legend,
):
    direction_solutions = list(_solutions(mosaic_with_legend))
    broken = direction_solutions[-1]
    direction_solutions[-1] = replace(
        broken,
        solution=replace(
            broken.solution,
            metrics=replace(
                broken.solution.metrics,
                under_reinforced_cell_count=1,
            ),
        ),
    )

    plate = build_plate_solution(direction_solutions)

    assert plate.valid is False
    assert plate.status is SolutionStatus.ERROR
    assert plate.metrics.under_reinforced_cell_count == 1
    assert any(
        diagnostic == "ERROR: общеплитное решение содержит недоармированные КЭ: 1"
        for diagnostic in plate.diagnostics
    )


def test_plate_solution_propagates_non_usable_direction_status(mosaic_with_legend):
    direction_solutions = list(_solutions(mosaic_with_legend))
    failed = direction_solutions[1]
    direction_solutions[1] = replace(
        failed,
        solution=replace(
            failed.solution,
            status=SolutionStatus.ERROR,
            diagnostics=("ERROR: тестовая ошибка",),
        ),
    )

    plate = build_plate_solution(direction_solutions)

    assert plate.valid is False
    assert plate.status is SolutionStatus.ERROR
    assert "ERROR: bottom-Y: тестовая ошибка" in plate.diagnostics
