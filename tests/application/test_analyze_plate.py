"""Тесты прикладного сценария полного комплекта DXF одной плиты."""

from __future__ import annotations

import importlib
from dataclasses import replace

import pytest

from rebar import Direction
from rebar.application import (
    DirectionAnalysis,
    PlateDirectionSource,
    analyze_plate,
)
from rebar.optimization import (
    PLATE_DIRECTIONS,
    LayoutConstraints,
    SolutionStatus,
    StrongestBBoxOptimizer,
    build_layout_problem,
)

scenario = importlib.import_module("rebar.application.analyze_plate")


def _sources() -> tuple[PlateDirectionSource, ...]:
    return tuple(
        PlateDirectionSource(
            dxf_path=f"{direction}.dxf",
            mapping_id="plate-zero-d12-v1" if index == 0 else "auto",
        )
        for index, direction in enumerate(reversed(PLATE_DIRECTIONS))
    )


def _fake_analyzer(direction_mosaic, *, invalid_direction: Direction | None = None):
    calls = []

    def fake(path, **kwargs):
        calls.append((path, kwargs))
        direction = next(
            candidate for candidate in PLATE_DIRECTIONS if str(candidate) in str(path)
        )
        mosaic = replace(
            direction_mosaic,
            direction=direction,
            source_path=str(path),
        )
        problem = build_layout_problem(
            mosaic,
            LayoutConstraints(min_width_cells=kwargs["min_width_cells"]),
        )
        baseline = StrongestBBoxOptimizer().solve(problem)
        solutions = tuple(
            replace(
                baseline,
                algorithm=algorithm,
                status=(
                    SolutionStatus.ERROR
                    if direction == invalid_direction
                    else baseline.status
                ),
            )
            for algorithm in kwargs["algorithm_names"]
        )
        return DirectionAnalysis(mosaic=mosaic, problem=problem, solutions=solutions)

    return calls, fake


def test_analyze_plate_builds_same_algorithm_baselines_in_canonical_order(
    monkeypatch,
    direction_mosaic,
):
    calls, fake = _fake_analyzer(direction_mosaic)
    monkeypatch.setattr(scenario, "analyze_direction", fake)

    analysis = analyze_plate(
        _sources(),
        algorithm_names=("bbox", "bsp"),
        max_details_per_direction=7,
        min_width_cells=1,
        case_id="demo-plate",
    )

    assert len(calls) == 4
    assert calls[0][1]["mapping_id"] == "plate-zero-d12-v1"
    assert all(call[1]["max_details"] == 7 for call in calls)
    assert tuple(
        item.problem.demand.direction for item in analysis.direction_analyses
    ) == PLATE_DIRECTIONS
    assert analysis.problem.case_id == "demo-plate"
    assert len(analysis.solutions) == 2
    assert set(analysis.solutions[0].meta["algorithm_by_direction"].values()) == {"bbox"}
    assert set(analysis.solutions[1].meta["algorithm_by_direction"].values()) == {"bsp"}
    assert analysis.solutions[0].metrics.direction_count == 4
    assert analysis.front is not None
    assert analysis.front.combination_count >= 1
    assert analysis.front.candidates
    assert analysis.direction(PLATE_DIRECTIONS[-1]).mosaic.direction == PLATE_DIRECTIONS[-1]


def test_analyze_plate_rejects_wrong_source_count_before_reading(
    monkeypatch,
    direction_mosaic,
):
    _calls, fake = _fake_analyzer(direction_mosaic)
    monkeypatch.setattr(scenario, "analyze_direction", fake)

    with pytest.raises(ValueError, match="ровно четыре DXF"):
        analyze_plate(_sources()[:-1], algorithm_names=("bbox",))


def test_analyze_plate_rejects_duplicate_detected_directions(
    monkeypatch,
    direction_mosaic,
):
    def duplicate_analyzer(path, **kwargs):
        mosaic = replace(direction_mosaic, source_path=str(path))
        problem = build_layout_problem(mosaic)
        solution = StrongestBBoxOptimizer().solve(problem, None)
        return DirectionAnalysis(mosaic=mosaic, problem=problem, solutions=(solution,))

    monkeypatch.setattr(scenario, "analyze_direction", duplicate_analyzer)

    with pytest.raises(ValueError, match="направления повторяются: bottom-X"):
        analyze_plate(_sources(), algorithm_names=("bbox",))


def test_analyze_plate_propagates_invalid_direction(monkeypatch, direction_mosaic):
    _calls, fake = _fake_analyzer(
        direction_mosaic,
        invalid_direction=PLATE_DIRECTIONS[-1],
    )
    monkeypatch.setattr(scenario, "analyze_direction", fake)

    analysis = analyze_plate(_sources(), algorithm_names=("bbox",), min_width_cells=1)

    assert analysis.solutions[0].status is SolutionStatus.ERROR
    assert analysis.solutions[0].valid is False


def test_analyze_plate_runs_real_plate_zero_complete_set(plate_zero_dxf_files):
    assert len(plate_zero_dxf_files) == 4

    analysis = analyze_plate(
        tuple(
            PlateDirectionSource(path, mapping_id="plate-zero-d12-v1")
            for path in plate_zero_dxf_files
        ),
        algorithm_names=("bbox",),
        max_details_per_direction=32,
        min_width_cells=2,
        case_id="plate-zero",
    )

    assert tuple(
        item.problem.demand.direction for item in analysis.direction_analyses
    ) == PLATE_DIRECTIONS
    assert len(analysis.solutions) == 1
    assert analysis.solutions[0].valid is True
    assert analysis.solutions[0].metrics.direction_count == 4
    assert analysis.solutions[0].metrics.physical_bar_count > 0
    assert analysis.solutions[0].metrics.total_mass_kg > 0
    assert analysis.front is not None
    assert all(candidate.solution.valid for candidate in analysis.front.candidates)
