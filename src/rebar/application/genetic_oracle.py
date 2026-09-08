"""Сравнение публичного GA с точным фронтом на малых синтетических задачах."""

from __future__ import annotations

from dataclasses import dataclass, replace

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar
from rebar.optimization import (
    AlgorithmRequest,
    GeneticParetoOptimizer,
    LayoutConstraints,
    LayoutProblem,
    LayoutSolution,
    LayoutZone,
    SolutionStatus,
    build_layout_problem,
    evaluate_layout,
)
from rebar.optimization.algorithms.genetic.exact_oracle import (
    MASS_TOLERANCE_KG,
    ExactOracleResult,
    solution_complexity,
    solve_exact_candidate_front,
)
from rebar.optimization.algorithms.genetic_pareto import (
    _build_search_space,
    _materialize_genome,
)
from rebar.optimization.services import prepare_detailing

from .genetic_benchmark import GeneticRunConfig


@dataclass(frozen=True)
class OracleBudgetGap:
    complexity: int
    exact_mass_kg: float
    genetic_mass_kg: float | None
    gap_pct: float | None


@dataclass(frozen=True)
class GeneticOracleRun:
    case_id: str
    problem: LayoutProblem
    config: GeneticRunConfig
    candidate_zones: tuple[LayoutZone, ...]
    baseline_seed_genomes: tuple[tuple[int, ...], ...]
    oracle: ExactOracleResult
    genetic_solutions: tuple[LayoutSolution, ...]
    rejected_genetic_count: int
    minimum_mass_gap_pct: float | None
    recovered_exact_points: int
    budget_gaps: tuple[OracleBudgetGap, ...]


def _mass_gap_pct(actual: float, exact: float) -> float:
    delta = actual - exact
    if delta < -MASS_TOLERANCE_KG:
        raise ValueError("GA оказался ниже exact-front: наборы кандидатов или проверки различаются")
    if abs(delta) <= MASS_TOLERANCE_KG:
        return 0.0
    if exact == 0:
        raise ValueError("относительный gap к нулевой массе не определён")
    return 100.0 * delta / exact


def compare_genetic_with_oracle(
    problem: LayoutProblem,
    config: GeneticRunConfig,
    *,
    max_details: int | None = None,
    max_candidates: int = 24,
    max_subsets: int = 100_000,
) -> GeneticOracleRun:
    """Один seed/политика/ось; никаких усечённых пулов или привилегий oracle.

    Оба запуска строят CandidateSet одной детерминированной функцией с одинаковыми
    параметрами. Возвращённые GA геномы дополнительно восстанавливаются по пулу oracle:
    при расхождении геометрии или метрик сравнение останавливается.
    """

    request = AlgorithmRequest(
        max_details=(len(problem.demand.cells) or None) if max_details is None else max_details,
        params=config.algorithm_params(),
    )
    space = _build_search_space(
        problem, request,
        candidate_window=config.candidate_window,
        candidate_trajectories=config.candidate_trajectories,
        layer_bridge_span=config.layer_bridge_span,
        maximum_merge_reduction=config.maximum_merge_reduction,
        maximum_pool_merges=config.maximum_pool_merges,
        baseline_seed_algorithms=config.baseline_seed_algorithms,
        random_seed=config.random_seed,
        deadline=None,
        candidate_expansion=config.candidate_expansion,
        maximum_layer_variants=config.maximum_layer_variants,
        coverage_atoms=config.coverage_atoms,
    )
    oracle = solve_exact_candidate_front(
        problem, space, request, complexity_axis=config.complexity_axis,
        max_candidates=max_candidates, max_subsets=max_subsets,
    )
    returned = GeneticParetoOptimizer().solve_many(problem, request)
    accepted: list[LayoutSolution] = []
    context = prepare_detailing(problem)
    for solution in returned:
        genome = frozenset(solution.meta.get("genome_candidate_indexes", ()))
        if solution.zones and solution.meta.get("candidate_pool_size") != len(space.candidates):
            raise ValueError("размер CandidateSet GA не совпадает с пулом oracle")
        expected_zones, _diagnostic = _materialize_genome(problem, space, genome, context)
        if solution.zones != expected_zones:
            raise ValueError("геометрия GA не совпадает с тем же геномом CandidateSet oracle")
        evaluation = evaluate_layout(problem, solution.zones, request)
        if evaluation.metrics != solution.metrics:
            raise ValueError("метрики GA не совпадают с независимой проверкой")
        if evaluation.valid and solution.status in {SolutionStatus.FEASIBLE, SolutionStatus.OPTIMAL}:
            accepted.append(solution)

    axis = config.complexity_axis
    for solution in accepted:
        eligible = [
            point.metrics.total_mass_kg for point in oracle.solutions
            if solution_complexity(point, axis) <= solution_complexity(solution, axis)
        ]
        if not eligible:
            raise ValueError("допустимый геном GA отсутствует в классе решений oracle")
        _mass_gap_pct(solution.metrics.total_mass_kg, min(eligible))

    budget_gaps = []
    recovered = 0
    for point in oracle.solutions:
        complexity = solution_complexity(point, axis)
        eligible = [
            solution.metrics.total_mass_kg for solution in accepted
            if solution_complexity(solution, axis) <= complexity
        ]
        mass = min(eligible) if eligible else None
        gap = None if mass is None else _mass_gap_pct(mass, point.metrics.total_mass_kg)
        if gap == 0.0:
            recovered += 1
        budget_gaps.append(OracleBudgetGap(complexity, point.metrics.total_mass_kg, mass, gap))
    minimum_gap = None
    if accepted and oracle.solutions:
        minimum_gap = _mass_gap_pct(
            min(solution.metrics.total_mass_kg for solution in accepted),
            min(point.metrics.total_mass_kg for point in oracle.solutions),
        )
    return GeneticOracleRun(
        case_id=problem.case_id,
        problem=problem,
        config=config,
        candidate_zones=tuple(candidate.rectangle.zone for candidate in space.candidates),
        baseline_seed_genomes=tuple(tuple(sorted(genome)) for genome in space.baseline_seed_genomes),
        oracle=oracle,
        genetic_solutions=tuple(accepted),
        rejected_genetic_count=len(returned) - len(accepted),
        minimum_mass_gap_pct=minimum_gap,
        recovered_exact_points=recovered,
        budget_gaps=tuple(budget_gaps),
    )


def small_oracle_problems() -> tuple[LayoutProblem, ...]:
    """Открытые маски без материалов заказчика: уровни, фон и отсутствующая геометрия."""

    background = Rebar(300, 12)
    bands = [
        Band(0, 181, "s300d12", 3.77, background, None),
        Band(1, 254, "s300d12+s300d12", 7.54, background, Rebar(300, 12)),
        Band(2, 2, "s300d12+s100d20", 35.19, background, Rebar(100, 20)),
    ]
    masks = {
        "weak-strong-weak": ((1, 2, 1),),
        "checkerboard": ((1, 2), (2, 1)),
        "background-bridge": ((1, 0, 1),),
        "missing-tile": ((1, 1), (1, None)),
        "central-hotspot": ((1, 2, 1), (1, 1, 1)),
        "background-corridor": ((1, 0, 2), (2, 0, 1)),
    }
    result = []
    for name, mask in masks.items():
        for axis in (Axis.X, Axis.Y):
            cells = []
            for row, levels in enumerate(mask):
                for column, level in enumerate(levels):
                    if level is None:
                        continue
                    x, y = column * 500, row * 500
                    cells.append(Cell(
                        [(x, y), (x + 500, y), (x + 500, y + 500), (x, y + 500)],
                        (x + 250, y + 250), bands[level].aci, bands[level],
                    ))
            mosaic = Mosaic(
                Direction(Layer.BOTTOM, axis), cells, bands,
                (0, 0, len(mask[0]) * 500, len(mask) * 500),
            )
            result.append(replace(
                build_layout_problem(mosaic, LayoutConstraints(min_width_cells=1)),
                case_id=f"{name}-{axis.value}",
            ))
    # Реальная граница КЭ 600 мм не совпадает с поисковой сеткой 0/500/1000 мм.
    # Эти случаи ловят ложное покрытие округлённым baseline-hull.
    for axis in (Axis.X, Axis.Y):
        cells = [
            Cell([(0, 0), (600, 0), (600, 500), (0, 500)], (300, 250), bands[1].aci, bands[1]),
            Cell([(600, 0), (1000, 0), (1000, 500), (600, 500)], (800, 250), bands[2].aci, bands[2]),
        ]
        mosaic = Mosaic(Direction(Layer.BOTTOM, axis), cells, bands, (0, 0, 1000, 500))
        result.append(replace(
            build_layout_problem(mosaic, LayoutConstraints(min_width_cells=1)),
            case_id=f"off-grid-boundary-{axis.value}",
        ))
    return tuple(result)
