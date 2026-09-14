"""Точный исследовательский фронт внутри малого конечного CandidateSet GA.

Это не новый production-оптимизатор и не поиск всех геометрий/поперечных фаз.
Перебор не использует GA repair, leaf_ids, отбор, reward или seed-популяцию.
Детализация и hard-валидатор общие: сравнивается поиск, а не инженерные формулы.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from time import perf_counter

from ...contracts import (
    AlgorithmRequest,
    ComplexityAxis,
    LayoutProblem,
    LayoutSolution,
    SolutionStatus,
)
from ...services import build_zone_from_bbox, demanded_cells, evaluate_layout, prepare_detailing
from ...services.bar_schedule import straight_bar_key, zone_position_keys
from ..genetic_pareto import _materialize_genome, _SearchSpace

MASS_TOLERANCE_KG = 1e-6
EXACT_SCOPE = "finite_candidate_set_with_deterministic_detailing"


@dataclass(frozen=True)
class ExactOracleResult:
    """Сертификат полного перебора в пределах явно указанного класса решений."""

    solutions: tuple[LayoutSolution, ...]
    complexity_axis: ComplexityAxis
    candidate_count: int
    maximum_zones: int
    subset_count: int
    evaluated_count: int
    coverage_pruned_count: int
    objective_pruned_count: int
    rejected_count: int
    runtime_ms: float
    scope: str = EXACT_SCOPE
    complete: bool = True


def solution_complexity(solution: LayoutSolution, axis: ComplexityAxis) -> int:
    if axis is ComplexityAxis.ZONE_COUNT:
        return solution.metrics.detail_count
    if axis is ComplexityAxis.PHYSICAL_BAR_COUNT:
        return solution.metrics.physical_bar_count
    if axis is ComplexityAxis.POSITION_COUNT:
        return len(zone_position_keys(solution.zones))
    raise ValueError(f"неподдерживаемая ось сложности oracle: {axis}")


def solve_exact_candidate_front(
    problem: LayoutProblem,
    space: _SearchSpace,
    request: AlgorithmRequest,
    *,
    complexity_axis: ComplexityAxis = ComplexityAxis.ZONE_COUNT,
    max_candidates: int = 24,
    max_subsets: int = 100_000,
) -> ExactOracleResult:
    """Перебрать подмножества; при превышении бюджета отказать до перебора.

    Сначала проверяется необходимое (не достаточное) условие: каждый требуемый КЭ
    пересекает хотя бы один достаточный прямоугольник. Полноту геометрического
    объединения, коллизии и нормативные запреты проверяет только evaluate_layout.
    Отсечение по стоимости использует уже найденную hard-valid точку и проверенные
    общим конструктором массы. Точность сравнения масс — 1e-6 кг.
    """

    started = perf_counter()
    if not isinstance(complexity_axis, ComplexityAxis):
        raise ValueError("неподдерживаемая ось сложности oracle")
    if max_candidates < 1 or max_subsets < 1:
        raise ValueError("лимиты oracle должны быть положительными")
    if request.time_limit_s is not None:
        raise ValueError("exact-oracle не принимает time_limit_s: используйте max_subsets")
    if space.stopped_by_time_limit:
        raise ValueError("CandidateSet остановлен по времени; точный benchmark не запускается")
    candidate_count = len(space.candidates)
    if candidate_count > max_candidates:
        raise ValueError(f"CandidateSet: {candidate_count} > max_candidates={max_candidates}")
    demanded = demanded_cells(problem)
    maximum_zones = min(candidate_count, len(problem.demand.cells)) if demanded else 0
    if request.max_details is not None:
        maximum_zones = min(maximum_zones, request.max_details)
    subset_count = sum(math.comb(candidate_count, count) for count in range(maximum_zones + 1))
    if subset_count > max_subsets:
        raise ValueError(f"перебор: {subset_count} > max_subsets={max_subsets}")

    context = prepare_detailing(problem) if space.candidates else None
    masks: list[int] = []
    costs: list[float] = []
    bar_counts: list[int] = []
    position_keys: list[tuple] = []
    for candidate in space.candidates:
        zone = candidate.rectangle.zone
        canonical = build_zone_from_bbox(
            problem, zone.demand_bbox, zone.level_index, zone.id,
            seed_cell_ids=candidate.source_cell_ids, context=context,
        )
        if (
            not math.isclose(zone.mass_kg, canonical.mass_kg, rel_tol=0, abs_tol=MASS_TOLERANCE_KG)
            or zone.bar_count != canonical.bar_count
            or zone.mass_kg <= 0
        ):
            raise ValueError("стоимость CandidateSet не совпадает с общим конструктором")
        costs.append(canonical.mass_kg)
        bar_counts.append(canonical.bar_count)
        position_keys.append(straight_bar_key(canonical.rebar.diameter, canonical.installed_length_mm))
        masks.append(sum(
            1 << index
            for index, cell in enumerate(demanded)
            if cell.id in canonical.covered_cell_ids
        ))

    required_mask = (1 << len(demanded)) - 1
    front: list[LayoutSolution] = []
    evaluated = coverage_pruned = objective_pruned = rejected = 0
    for count in range(maximum_zones + 1):
        for indexes in itertools.combinations(range(candidate_count), count):
            coverage_mask = 0
            for index in indexes:
                coverage_mask |= masks[index]
            if coverage_mask != required_mask:
                coverage_pruned += 1
                continue
            mass = sum(costs[index] for index in indexes)
            complexity = count if complexity_axis is ComplexityAxis.ZONE_COUNT else sum(
                bar_counts[index] for index in indexes
            )
            if complexity_axis is ComplexityAxis.POSITION_COUNT:
                complexity = len({position_keys[index] for index in indexes})
            if any(
                solution_complexity(point, complexity_axis) <= complexity
                and point.metrics.total_mass_kg <= mass + MASS_TOLERANCE_KG
                for point in front
            ):
                objective_pruned += 1
                continue
            if indexes:
                zones, phase_diagnostic = _materialize_genome(
                    problem, space, frozenset(indexes), context,
                )
            else:
                zones, phase_diagnostic = (), None
            evaluation = evaluate_layout(problem, zones, request)
            evaluated += 1
            if not evaluation.valid:
                rejected += 1
                continue
            actual_complexity = (
                evaluation.metrics.detail_count
                if complexity_axis is ComplexityAxis.ZONE_COUNT
                else evaluation.metrics.physical_bar_count
            )
            if complexity_axis is ComplexityAxis.POSITION_COUNT:
                actual_complexity = len(zone_position_keys(zones))
            if (
                not math.isclose(
                    mass, evaluation.metrics.total_mass_kg, rel_tol=0, abs_tol=MASS_TOLERANCE_KG,
                )
                or complexity != actual_complexity
            ):
                raise ValueError("постобработка изменила стоимость: отсечение oracle недопустимо")
            diagnostics = evaluation.diagnostics
            if phase_diagnostic is not None:
                diagnostics = (*diagnostics, phase_diagnostic)
            front = [
                point for point in front
                if not (
                    complexity <= solution_complexity(point, complexity_axis)
                    and mass <= point.metrics.total_mass_kg + MASS_TOLERANCE_KG
                )
            ]
            front.append(LayoutSolution(
                algorithm="exact-candidate-oracle",
                # Не маркируем геометрию как глобально OPTIMAL или пригодную к Revit.
                status=SolutionStatus.FEASIBLE,
                zones=zones,
                metrics=evaluation.metrics,
                request=request,
                diagnostics=diagnostics,
                meta={"exact_scope": EXACT_SCOPE, "genome_candidate_indexes": indexes},
            ))
    return ExactOracleResult(
        solutions=tuple(sorted(front, key=lambda point: (
            solution_complexity(point, complexity_axis), point.metrics.total_mass_kg,
        ))),
        complexity_axis=complexity_axis,
        candidate_count=candidate_count,
        maximum_zones=maximum_zones,
        subset_count=subset_count,
        evaluated_count=evaluated,
        coverage_pruned_count=coverage_pruned,
        objective_pruned_count=objective_pruned,
        rejected_count=rejected,
        runtime_ms=(perf_counter() - started) * 1000.0,
    )
