"""Единый прикладной сценарий анализа одного направления из DXF."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rebar.dxf_ingest import read_mosaic
from rebar.models import Mosaic
from rebar.optimization import (
    PLATE_ZERO_D12,
    PLATE_11700_CATALOG,
    AlgorithmRequest,
    LayoutConstraints,
    LayoutProblem,
    LayoutSolution,
    ObjectiveWeights,
    RebarMapping,
    apply_rebar_mapping,
    build_layout_problem,
    built_in_optimizer_registry,
)

DEFAULT_ALGORITHMS = (
    "spatial-partition-greedy",
    "row-run-greedy",
    "strip-profile-dp",
    "bsp",
    "agglomerative",
)
_MAPPINGS: dict[str, RebarMapping] = {PLATE_ZERO_D12.id: PLATE_ZERO_D12}
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


def analyze_direction(
    dxf_path: str | Path,
    *,
    shk_path: str | Path | None = None,
    mapping_id: str = "auto",
    algorithm_names: tuple[str, ...] = DEFAULT_ALGORITHMS,
    max_details: int = 32,
    min_width_cells: int = 2,
    detail_penalty_kg: float = 0.0,
    cutting_profile: str = "continuous",
) -> DirectionAnalysis:
    """Разобрать один DXF и выполнить выбранные взаимозаменяемые оптимизаторы."""

    selected_mapping = _mapping(mapping_id)
    if selected_mapping is not None and shk_path is not None:
        raise ValueError("нельзя одновременно передать .shk и ручную таблицу армирования")

    selected_algorithms = _algorithm_names(algorithm_names)
    allowed_cut_lengths = _cutting_lengths(cutting_profile)
    mosaic = read_mosaic(
        str(dxf_path),
        shk_path=str(shk_path) if shk_path is not None else None,
    )
    if selected_mapping is not None:
        mosaic = apply_rebar_mapping(mosaic, selected_mapping)

    problem = build_layout_problem(
        mosaic,
        LayoutConstraints(
            min_width_cells=min_width_cells,
            allowed_cut_lengths_mm=allowed_cut_lengths,
            cutting_profile=cutting_profile.strip().casefold(),
        ),
    )
    request = AlgorithmRequest(
        objective=ObjectiveWeights(detail_penalty_kg=detail_penalty_kg),
        max_details=max_details,
    )
    registry = built_in_optimizer_registry()
    solutions = tuple(
        registry.create(name).solve(problem, request) for name in selected_algorithms
    )
    return DirectionAnalysis(mosaic=mosaic, problem=problem, solutions=solutions)
