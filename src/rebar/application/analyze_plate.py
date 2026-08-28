"""Прикладной сценарий анализа полного комплекта направлений одной плиты."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rebar.models import Direction
from rebar.optimization import (
    PLATE_DIRECTIONS,
    ComplexityAxis,
    PlateDirectionSolution,
    PlateParetoFront,
    PlateProblem,
    PlateSolution,
    build_plate_problem,
    build_plate_pareto_front,
    build_plate_solution,
    combine_direction_pareto_fronts,
)

from .analyze_direction import (
    DEFAULT_ALGORITHMS,
    DirectionAnalysis,
    _algorithm_names,
    analyze_direction,
)


@dataclass(frozen=True)
class PlateDirectionSource:
    """DXF и способ назначения шкалы для одного неизвестного заранее направления."""

    dxf_path: str | Path
    shk_path: str | Path | None = None
    mapping_id: str = "auto"


@dataclass(frozen=True)
class PlateAnalysis:
    """Четыре анализа направлений и сопоставимые общеплитные baseline-решения."""

    problem: PlateProblem
    direction_analyses: tuple[DirectionAnalysis, ...]
    solutions: tuple[PlateSolution, ...]
    front: PlateParetoFront | None = None

    def direction(self, direction: Direction) -> DirectionAnalysis:
        """Вернуть прикладной результат указанного направления."""

        for analysis in self.direction_analyses:
            if analysis.problem.demand.direction == direction:
                return analysis
        raise KeyError(f"направление {direction} отсутствует")


def _solution_for_algorithm(
    analysis: DirectionAnalysis,
    algorithm: str,
) -> PlateDirectionSolution:
    matches = tuple(
        solution for solution in analysis.solutions if solution.algorithm == algorithm
    )
    if len(matches) != 1:
        direction = analysis.problem.demand.direction
        raise RuntimeError(
            f"направление {direction}: ожидалось одно решение алгоритма {algorithm!r}, "
            f"получено {len(matches)}"
        )
    return PlateDirectionSolution(
        direction=analysis.problem.demand.direction,
        solution=matches[0],
    )


def analyze_plate(
    sources: Iterable[PlateDirectionSource],
    *,
    algorithm_names: tuple[str, ...] = DEFAULT_ALGORITHMS,
    max_details_per_direction: int | None = None,
    min_width_cells: int = 2,
    detail_penalty_kg: float = 0.0,
    cutting_profile: str = "continuous",
    case_id: str = "",
    complexity_axis: ComplexityAxis = ComplexityAxis.ZONE_COUNT,
    algorithm_params: Mapping[str, Mapping[str, Any]] | None = None,
) -> PlateAnalysis:
    """Разобрать четыре DXF и собрать по одному plate-baseline на алгоритм."""

    normalized_sources = tuple(sources)
    if len(normalized_sources) != len(PLATE_DIRECTIONS):
        raise ValueError(
            "для анализа плиты нужны ровно четыре DXF: bottom-X, bottom-Y, top-X, top-Y"
        )
    selected_algorithms = _algorithm_names(algorithm_names)

    analyses = tuple(
        analyze_direction(
            source.dxf_path,
            shk_path=source.shk_path,
            mapping_id=source.mapping_id,
            algorithm_names=selected_algorithms,
            max_details=max_details_per_direction,
            min_width_cells=min_width_cells,
            detail_penalty_kg=detail_penalty_kg,
            cutting_profile=cutting_profile,
            complexity_axis=complexity_axis,
            algorithm_params=algorithm_params,
        )
        for source in normalized_sources
    )
    problem = build_plate_problem(
        (analysis.problem for analysis in analyses),
        case_id=case_id,
        meta={"source_paths": tuple(str(source.dxf_path) for source in normalized_sources)},
    )
    by_direction = {
        analysis.problem.demand.direction: analysis for analysis in analyses
    }
    ordered_analyses = tuple(by_direction[direction] for direction in PLATE_DIRECTIONS)
    solutions = tuple(
        build_plate_solution(
            (
                _solution_for_algorithm(analysis, algorithm)
                for analysis in ordered_analyses
            ),
            meta={"case_id": case_id, "combination": "same-algorithm-baseline"},
        )
        for algorithm in selected_algorithms
    )
    existing_fronts = tuple(analysis.front for analysis in ordered_analyses)
    if all(front is not None for front in existing_fronts):
        front = combine_direction_pareto_fronts(
            problem,
            (front for front in existing_fronts if front is not None),
        )
    else:
        front = build_plate_pareto_front(
            problem,
            {
                analysis.problem.demand.direction: analysis.solutions
                for analysis in ordered_analyses
            },
            complexity_axis=complexity_axis,
        )
    return PlateAnalysis(
        problem=problem,
        direction_analyses=ordered_analyses,
        solutions=solutions,
        front=front,
    )
