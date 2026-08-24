"""Сборка проверенных кандидатов и Парето-фронтов."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from itertools import product
from math import prod

from rebar.models import Direction

from ..contracts import (
    PLATE_DIRECTIONS,
    CandidateRejection,
    ComplexityAxis,
    DirectionCandidate,
    DirectionParetoFront,
    LayoutProblem,
    LayoutSolution,
    PlateCandidate,
    PlateDirectionSolution,
    PlateParetoFront,
    PlateProblem,
    SolutionStatus,
)
from .constructability import (
    measure_constructability,
    measure_plate_constructability,
)
from .evaluation import evaluate_layout
from .plate import build_plate_solution

_MASS_TOLERANCE_KG = 1e-6


def _candidate_id(direction: Direction, solution: LayoutSolution, index: int) -> str:
    return f"{direction}:{solution.algorithm}:{index + 1}"


def _validated_candidates(
    problem: LayoutProblem,
    solutions: Iterable[LayoutSolution],
) -> tuple[tuple[DirectionCandidate, ...], tuple[CandidateRejection, ...]]:
    candidates: list[DirectionCandidate] = []
    rejections: list[CandidateRejection] = []
    for index, solution in enumerate(solutions):
        candidate_id = _candidate_id(problem.demand.direction, solution, index)
        evaluation = evaluate_layout(problem, solution.zones, solution.request)
        diagnostics = tuple(
            dict.fromkeys((*evaluation.diagnostics, *solution.diagnostics))
        )
        usable_status = solution.status in {
            SolutionStatus.FEASIBLE,
            SolutionStatus.OPTIMAL,
        }
        if not evaluation.valid or not usable_status:
            if not usable_status:
                diagnostics = (
                    *diagnostics,
                    f"ERROR: статус алгоритма не допускает вариант: "
                    f"{solution.status.value}",
                )
            rejections.append(
                CandidateRejection(
                    id=candidate_id,
                    algorithm=solution.algorithm,
                    diagnostics=diagnostics,
                )
            )
            continue

        normalized_solution = replace(
            solution,
            metrics=evaluation.metrics,
            diagnostics=diagnostics,
        )
        candidates.append(
            DirectionCandidate(
                id=candidate_id,
                direction=problem.demand.direction,
                solution=normalized_solution,
                constructability=measure_constructability(normalized_solution),
            )
        )
    return tuple(candidates), tuple(rejections)


def _objective(candidate, axis: ComplexityAxis) -> tuple[float, int]:
    return (
        candidate.solution.metrics.total_mass_kg,
        candidate.constructability.value(axis),
    )


def _dominates(first, second, axis: ComplexityAxis) -> bool:
    first_mass, first_complexity = _objective(first, axis)
    second_mass, second_complexity = _objective(second, axis)
    no_worse = (
        first_mass <= second_mass + _MASS_TOLERANCE_KG
        and first_complexity <= second_complexity
    )
    strictly_better = (
        first_mass < second_mass - _MASS_TOLERANCE_KG
        or first_complexity < second_complexity
    )
    return no_worse and strictly_better


def _same_point(first, second, axis: ComplexityAxis) -> bool:
    first_mass, first_complexity = _objective(first, axis)
    second_mass, second_complexity = _objective(second, axis)
    return (
        abs(first_mass - second_mass) <= _MASS_TOLERANCE_KG
        and first_complexity == second_complexity
    )


def _pareto_candidates(candidates, axis: ComplexityAxis):
    nondominated = [
        candidate
        for candidate in candidates
        if not any(
            other is not candidate and _dominates(other, candidate, axis)
            for other in candidates
        )
    ]
    nondominated.sort(key=lambda item: (*reversed(_objective(item, axis)), item.id))

    representatives = []
    equivalent_count = 0
    for candidate in nondominated:
        if representatives and _same_point(representatives[-1], candidate, axis):
            representative = representatives[-1]
            representatives[-1] = replace(
                representative,
                equivalent_candidate_ids=(
                    *representative.equivalent_candidate_ids,
                    candidate.id,
                    *candidate.equivalent_candidate_ids,
                ),
            )
            equivalent_count += 1 + len(candidate.equivalent_candidate_ids)
        else:
            representatives.append(candidate)
    return tuple(representatives), equivalent_count


def build_direction_pareto_front(
    problem: LayoutProblem,
    solutions: Iterable[LayoutSolution],
    *,
    complexity_axis: ComplexityAxis = ComplexityAxis.ZONE_COUNT,
) -> DirectionParetoFront:
    """Перепроверить решения и оставить недоминируемые варианты направления."""

    candidates, rejections = _validated_candidates(problem, solutions)
    front, equivalent_count = _pareto_candidates(candidates, complexity_axis)
    return DirectionParetoFront(
        direction=problem.demand.direction,
        complexity_axis=complexity_axis,
        candidates=front,
        source_candidate_count=len(candidates) + len(rejections),
        dominated_candidate_count=len(candidates) - len(front) - equivalent_count,
        equivalent_candidate_count=equivalent_count,
        rejections=rejections,
    )


def _direction_solution_map(
    problem: PlateProblem,
    solutions_by_direction: Mapping[Direction, Iterable[LayoutSolution]],
) -> dict[Direction, tuple[LayoutSolution, ...]]:
    unexpected = [
        direction for direction in solutions_by_direction if direction not in PLATE_DIRECTIONS
    ]
    missing = [
        direction for direction in PLATE_DIRECTIONS if direction not in solutions_by_direction
    ]
    if missing or unexpected:
        fragments = []
        if missing:
            fragments.append(f"отсутствуют: {', '.join(map(str, missing))}")
        if unexpected:
            fragments.append(f"неизвестны: {', '.join(map(str, unexpected))}")
        raise ValueError(
            "PlateParetoFront: нужны кандидаты четырёх направлений; "
            + "; ".join(fragments)
        )
    return {
        direction: tuple(solutions_by_direction[direction])
        for direction in PLATE_DIRECTIONS
    }


def build_plate_pareto_front(
    problem: PlateProblem,
    solutions_by_direction: Mapping[Direction, Iterable[LayoutSolution]],
    *,
    complexity_axis: ComplexityAxis = ComplexityAxis.ZONE_COUNT,
) -> PlateParetoFront:
    """Собрать допустимые комбинации четырёх направлений и общеплитный фронт."""

    normalized = _direction_solution_map(problem, solutions_by_direction)
    direction_fronts = tuple(
        build_direction_pareto_front(
            problem.problem(direction),
            normalized[direction],
            complexity_axis=complexity_axis,
        )
        for direction in PLATE_DIRECTIONS
    )
    return combine_direction_pareto_fronts(problem, direction_fronts)


def combine_direction_pareto_fronts(
    problem: PlateProblem,
    direction_fronts: Iterable[DirectionParetoFront],
) -> PlateParetoFront:
    """Объединить уже проверенные фронты без повторного запуска hard-валидатора."""

    normalized_fronts = tuple(direction_fronts)
    by_direction: dict[Direction, DirectionParetoFront] = {}
    for front in normalized_fronts:
        if front.direction in by_direction:
            raise ValueError(
                f"PlateParetoFront: направление повторяется: {front.direction}"
            )
        by_direction[front.direction] = front
    missing = [
        direction for direction in PLATE_DIRECTIONS if direction not in by_direction
    ]
    unexpected = [
        direction for direction in by_direction if direction not in PLATE_DIRECTIONS
    ]
    if missing or unexpected:
        fragments = []
        if missing:
            fragments.append(f"отсутствуют: {', '.join(map(str, missing))}")
        if unexpected:
            fragments.append(f"неизвестны: {', '.join(map(str, unexpected))}")
        raise ValueError(
            "PlateParetoFront: нужны фронты четырёх направлений; "
            + "; ".join(fragments)
        )
    canonical_fronts = tuple(by_direction[direction] for direction in PLATE_DIRECTIONS)
    axes = {front.complexity_axis for front in canonical_fronts}
    if len(axes) != 1:
        raise ValueError("PlateParetoFront: ось сложности направлений должна совпадать")
    complexity_axis = next(iter(axes))
    for direction in PLATE_DIRECTIONS:
        problem.problem(direction)

    candidate_groups = tuple(front.candidates for front in canonical_fronts)
    combination_count = prod(len(group) for group in candidate_groups)

    plate_candidates: list[PlateCandidate] = []
    for index, combination in enumerate(product(*candidate_groups)):
        plate_solution = build_plate_solution(
            (
                PlateDirectionSolution(
                    direction=candidate.direction,
                    solution=candidate.solution,
                )
                for candidate in combination
            ),
            meta={
                "combination": "pareto-direction-candidates",
                "direction_candidate_ids": tuple(
                    candidate.id for candidate in combination
                ),
            },
        )
        if not plate_solution.valid:
            continue
        plate_candidates.append(
            PlateCandidate(
                id=f"plate:{index + 1}",
                solution=plate_solution,
                constructability=measure_plate_constructability(plate_solution),
                direction_candidate_ids=tuple(
                    candidate.id for candidate in combination
                ),
            )
        )

    front, equivalent_count = _pareto_candidates(
        tuple(plate_candidates),
        complexity_axis,
    )
    return PlateParetoFront(
        complexity_axis=complexity_axis,
        candidates=front,
        direction_fronts=canonical_fronts,
        combination_count=combination_count,
        dominated_candidate_count=(
            len(plate_candidates) - len(front) - equivalent_count
        ),
        equivalent_candidate_count=equivalent_count,
    )
