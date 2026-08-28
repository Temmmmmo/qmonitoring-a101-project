"""Сборка проверенных кандидатов и Парето-фронтов."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
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


def _pareto_candidates(candidates, axis: ComplexityAxis):
    """Отфильтровать двумерный фронт за ``O(n log n)``.

    После сортировки по сложности кандидат недоминируем только тогда, когда его масса
    строго улучшает лучшую массу всех меньших значений сложности. Кандидаты одной
    точки сохраняются как эквивалентные идентификаторы.
    """

    ordered = sorted(
        candidates,
        key=lambda item: (
            _objective(item, axis)[1],
            _objective(item, axis)[0],
            item.id,
        ),
    )
    representatives = []
    equivalent_count = 0
    best_mass = float("inf")
    index = 0
    while index < len(ordered):
        complexity = _objective(ordered[index], axis)[1]
        group_end = index + 1
        while (
            group_end < len(ordered)
            and _objective(ordered[group_end], axis)[1] == complexity
        ):
            group_end += 1

        group = ordered[index:group_end]
        minimum_mass = _objective(group[0], axis)[0]
        equivalent = tuple(
            candidate
            for candidate in group
            if abs(_objective(candidate, axis)[0] - minimum_mass)
            <= _MASS_TOLERANCE_KG
        )
        if minimum_mass < best_mass - _MASS_TOLERANCE_KG:
            representative = equivalent[0]
            if len(equivalent) > 1:
                representative = replace(
                    representative,
                    equivalent_candidate_ids=(
                        *representative.equivalent_candidate_ids,
                        *(
                            candidate_id
                            for candidate in equivalent[1:]
                            for candidate_id in (
                                candidate.id,
                                *candidate.equivalent_candidate_ids,
                            )
                        ),
                    ),
                )
                equivalent_count += sum(
                    1 + len(candidate.equivalent_candidate_ids)
                    for candidate in equivalent[1:]
                )
            representatives.append(representative)
            best_mass = minimum_mass
        index = group_end
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

    partial_combinations: tuple[tuple[DirectionCandidate, ...], ...] = ((),)
    for group in candidate_groups:
        expanded = tuple(
            (*partial, candidate)
            for partial in partial_combinations
            for candidate in group
        )
        ordered = sorted(
            expanded,
            key=lambda combination: (
                sum(
                    candidate.constructability.value(complexity_axis)
                    for candidate in combination
                ),
                sum(
                    candidate.solution.metrics.total_mass_kg
                    for candidate in combination
                ),
                tuple(candidate.id for candidate in combination),
            ),
        )
        partial_front: list[tuple[DirectionCandidate, ...]] = []
        best_mass = float("inf")
        index = 0
        while index < len(ordered):
            complexity = sum(
                candidate.constructability.value(complexity_axis)
                for candidate in ordered[index]
            )
            group_end = index + 1
            while group_end < len(ordered) and sum(
                candidate.constructability.value(complexity_axis)
                for candidate in ordered[group_end]
            ) == complexity:
                group_end += 1
            minimum_mass_combination = ordered[index]
            minimum_mass = sum(
                candidate.solution.metrics.total_mass_kg
                for candidate in minimum_mass_combination
            )
            if minimum_mass < best_mass - _MASS_TOLERANCE_KG:
                partial_front.append(minimum_mass_combination)
                best_mass = minimum_mass
            index = group_end
        partial_combinations = tuple(partial_front)

    plate_candidates: list[PlateCandidate] = []
    for index, combination in enumerate(partial_combinations):
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
            combination_count - len(front) - equivalent_count
        ),
        equivalent_candidate_count=equivalent_count,
    )
