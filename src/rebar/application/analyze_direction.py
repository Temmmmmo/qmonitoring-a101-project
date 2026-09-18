"""Единый прикладной сценарий анализа одного направления из DXF."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rebar.dxf_ingest import read_mosaic
from rebar.models import Mosaic
from rebar.png_legend import apply_png_legend
from rebar.optimization.mappings.legacy_s1 import LEGACY_S1_D18
from rebar.optimization import (
    K09_ABOVE_3_D10,
    K09_MINUS_2_D12,
    PLATE_ZERO_D12,
    PLATE_11700_CATALOG,
    AlgorithmRequest,
    ComplexityAxis,
    DirectionParetoFront,
    LayoutCandidateGenerator,
    LayoutConstraints,
    LayoutProblem,
    LayoutSolution,
    ObjectiveWeights,
    RebarMapping,
    SolutionStatus,
    apply_rebar_mapping,
    apply_single_cell_rule,
    build_direction_pareto_front,
    build_layout_problem,
    built_in_optimizer_registry,
    resolve_zone_count_limit,
)

DEFAULT_ALGORITHMS = ("genetic-pareto",)
_MAPPINGS: dict[str, RebarMapping] = {
    mapping.id: mapping
    for mapping in (K09_ABOVE_3_D10, K09_MINUS_2_D12, PLATE_ZERO_D12, LEGACY_S1_D18)
}
_CUTTING_PROFILES: dict[str, tuple[float, ...]] = {
    "continuous": (),
    PLATE_11700_CATALOG.id: PLATE_11700_CATALOG.lengths_mm,
}


@dataclass(frozen=True)
class DirectionAnalysis:
    """Результат одного запуска, достаточный для API и локальных отчётов."""

    mosaic: Mosaic
    problem: LayoutProblem
    solutions: tuple[LayoutSolution, ...]
    candidate_solutions: tuple[LayoutSolution, ...] = ()
    front: DirectionParetoFront | None = None


def available_mapping_ids() -> tuple[str, ...]:
    """Вернуть стабильные идентификаторы ручных таблиц армирования."""

    return tuple(sorted(_MAPPINGS))


def available_cutting_profile_ids() -> tuple[str, ...]:
    """Вернуть доступные профили фактических длин отрезков."""

    return tuple(_CUTTING_PROFILES)


def _cutting_lengths(profile_id: str) -> tuple[float, ...]:
    normalized = profile_id.strip().casefold()
    try:
        return _CUTTING_PROFILES[normalized]
    except KeyError as error:
        raise ValueError(
            f"неизвестный профиль раскроя {profile_id!r}; "
            f"доступны: {list(available_cutting_profile_ids())}"
        ) from error


def _mapping(mapping_id: str) -> RebarMapping | None:
    normalized = mapping_id.strip().casefold()
    if normalized == "auto":
        return None
    try:
        return _MAPPINGS[normalized]
    except KeyError as error:
        available = ", ".join(("auto", *available_mapping_ids()))
        raise ValueError(
            f"неизвестная таблица армирования {mapping_id!r}; доступны: {available}"
        ) from error


def _algorithm_names(names: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(name.strip().casefold() for name in names if name.strip()))
    if not normalized:
        raise ValueError("нужно выбрать хотя бы один алгоритм")

    available = set(built_in_optimizer_registry().names())
    unknown = [name for name in normalized if name not in available]
    if unknown:
        raise ValueError(f"неизвестные алгоритмы: {unknown}; доступны: {sorted(available)}")
    return normalized


def load_direction_mosaic(
    dxf_path: str | Path, *, shk_path: str | Path | None = None,
    png_path: str | Path | None = None, mapping_id: str = "auto",
) -> Mosaic:
    """Одна входная граница для старого GA и составного полного комплекта."""
    selected_mapping = _mapping(mapping_id)
    if sum(value is not None for value in (selected_mapping, shk_path, png_path)) > 1:
        raise ValueError("нельзя одновременно выбрать .shk, .png и таблицу армирования")
    mosaic = read_mosaic(
        str(dxf_path), shk_path=str(shk_path) if shk_path is not None else None,
        auto_shk=png_path is None and selected_mapping is None,
    )
    if png_path is not None:
        return apply_png_legend(mosaic, png_path)
    return apply_rebar_mapping(mosaic, selected_mapping) if selected_mapping is not None else mosaic


def _algorithm_requests(
    names: tuple[str, ...],
    *,
    max_details: int | None,
    detail_penalty_kg: float,
    complexity_axis: ComplexityAxis,
    algorithm_params: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, AlgorithmRequest]:
    normalized_params = {
        name.strip().casefold(): dict(params)
        for name, params in (algorithm_params or {}).items()
    }
    unexpected = sorted(set(normalized_params) - set(names))
    if unexpected:
        raise ValueError(
            "параметры переданы для незапущенных алгоритмов: "
            f"{unexpected}"
        )
    requests: dict[str, AlgorithmRequest] = {}
    for name in names:
        params = dict(normalized_params.get(name, {}))
        if name in {"genetic-pareto", "genetic-source-recovery"}:
            params.setdefault("complexity_axis", complexity_axis.value)
        requests[name] = AlgorithmRequest(
            objective=ObjectiveWeights(detail_penalty_kg=detail_penalty_kg),
            max_details=max_details,
            params=params,
        )
    return requests


def _representative(candidates: tuple[LayoutSolution, ...],
                    complexity_axis: ComplexityAxis = ComplexityAxis.ZONE_COUNT) -> LayoutSolution:
    """Выбрать стабильный центральный вариант, не называя его «Точкой 3»."""

    usable = tuple(
        solution
        for solution in candidates
        if solution.status in {SolutionStatus.FEASIBLE, SolutionStatus.OPTIMAL}
        and solution.metrics.under_reinforced_cell_count == 0
    ) or candidates
    if len(usable) == 1:
        return usable[0]

    masses = [solution.metrics.total_mass_kg for solution in usable]
    from rebar.optimization.services.constructability import measure_constructability

    counts = [measure_constructability(solution).value(complexity_axis) for solution in usable]
    mass_span = max(masses) - min(masses)
    count_span = max(counts) - min(counts)

    def distance(solution: LayoutSolution) -> tuple[float, float, int]:
        normalized_mass = (
            (solution.metrics.total_mass_kg - min(masses)) / mass_span
            if mass_span > 0
            else 0.0
        )
        normalized_count = (
            (measure_constructability(solution).value(complexity_axis) - min(counts)) / count_span
            if count_span > 0
            else 0.0
        )
        return (
            normalized_mass**2 + normalized_count**2,
            solution.metrics.total_mass_kg,
            solution.metrics.detail_count,
        )

    return min(usable, key=distance)


def analyze_direction(
    dxf_path: str | Path,
    *,
    shk_path: str | Path | None = None,
    png_path: str | Path | None = None,
    mapping_id: str = "auto",
    algorithm_names: tuple[str, ...] = DEFAULT_ALGORITHMS,
    max_details: int | None = None,
    min_width_cells: int = 2,
    detail_penalty_kg: float = 0.0,
    cutting_profile: str = "continuous",
    complexity_axis: ComplexityAxis = ComplexityAxis.ZONE_COUNT,
    algorithm_params: Mapping[str, Mapping[str, Any]] | None = None,
    single_cell_policy: str = "preserve",
) -> DirectionAnalysis:
    """Разобрать один DXF и выполнить выбранные взаимозаменяемые оптимизаторы."""

    selected_algorithms = _algorithm_names(algorithm_names)
    allowed_cut_lengths = _cutting_lengths(cutting_profile)
    mosaic = load_direction_mosaic(dxf_path, shk_path=shk_path, png_path=png_path, mapping_id=mapping_id)

    problem = apply_single_cell_rule(
        build_layout_problem(
            mosaic,
            LayoutConstraints(
                min_width_cells=min_width_cells,
                allowed_cut_lengths_mm=allowed_cut_lengths,
                cutting_profile=cutting_profile.strip().casefold(),
            ),
        ), policy=single_cell_policy,
    )
    effective_max_details = resolve_zone_count_limit(problem, max_details)
    requests = _algorithm_requests(
        selected_algorithms,
        max_details=effective_max_details,
        detail_penalty_kg=detail_penalty_kg,
        complexity_axis=complexity_axis,
        algorithm_params=algorithm_params,
    )
    registry = built_in_optimizer_registry()
    solutions: list[LayoutSolution] = []
    candidate_solutions: list[LayoutSolution] = []
    for name in selected_algorithms:
        optimizer = registry.create(name)
        request = requests[name]
        generated = (
            optimizer.solve_many(problem, request)
            if isinstance(optimizer, LayoutCandidateGenerator)
            else (optimizer.solve(problem, request),)
        )
        if not generated:
            raise RuntimeError(f"алгоритм {name!r} не вернул ни одного решения")
        candidate_solutions.extend(generated)
        solutions.append(_representative(generated, complexity_axis))
    front = build_direction_pareto_front(
        problem,
        candidate_solutions,
        complexity_axis=complexity_axis,
    )
    return DirectionAnalysis(
        mosaic=mosaic,
        problem=problem,
        solutions=tuple(solutions),
        candidate_solutions=tuple(candidate_solutions),
        front=front,
    )
