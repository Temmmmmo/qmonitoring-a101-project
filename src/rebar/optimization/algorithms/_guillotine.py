"""Общая механика допустимых гильотинных разрезов для baseline-алгоритмов."""

from __future__ import annotations

from dataclasses import dataclass

from ..contracts import AlgorithmRequest, LayoutProblem, LayoutZone
from ..services import DetailingContext, build_zone, zones_conflict


@dataclass(frozen=True)
class GuillotineSplit:
    """Один допустимый разрез листа на две непустые группы КЭ."""

    objective_gain: float
    leaf_index: int
    axis: int
    coordinate: float
    left: tuple[int, ...]
    right: tuple[int, ...]


def zone_for_ids(
    problem: LayoutProblem,
    ids: tuple[int, ...],
    zone_id: str,
    context: DetailingContext,
    *,
    collect_coverage: bool = True,
) -> LayoutZone:
    """Построить зону достаточного уровня для группы КЭ."""

    strongest = max(context.cells_by_id[cell_id].level_index for cell_id in ids)
    return build_zone(
        problem,
        ids,
        strongest,
        zone_id,
        collect_coverage=collect_coverage,
        context=context,
    )


def objective_cost(zone: LayoutZone, request: AlgorithmRequest) -> float:
    """Стоимость одной зоны в общей целевой функции."""

    return request.objective.mass * zone.mass_kg + request.objective.detail_penalty_kg


def generate_splits(
    problem: LayoutProblem,
    request: AlgorithmRequest,
    leaves: list[tuple[int, ...]],
    zones: list[LayoutZone],
    context: DetailingContext,
) -> list[GuillotineSplit]:
    """Перечислить допустимые разрезы листьев по координатам центроидов."""

    by_id = context.cells_by_id
    candidates: list[GuillotineSplit] = []
    for leaf_index, ids in enumerate(leaves):
        if len(ids) < 2:
            continue
        parent_cost = objective_cost(zones[leaf_index], request)
        for axis in (0, 1):
            coordinates = sorted({by_id[cell_id].centroid[axis] for cell_id in ids})
            for lower, upper in zip(coordinates, coordinates[1:]):
                cut = (lower + upper) / 2.0
                left = tuple(cell_id for cell_id in ids if by_id[cell_id].centroid[axis] <= cut)
                right = tuple(cell_id for cell_id in ids if by_id[cell_id].centroid[axis] > cut)
                if not left or not right:
                    continue
                left_zone = zone_for_ids(
                    problem,
                    left,
                    "candidate-left",
                    context,
                    collect_coverage=False,
                )
                right_zone = zone_for_ids(
                    problem,
                    right,
                    "candidate-right",
                    context,
                    collect_coverage=False,
                )
                if not problem.constraints.allow_overlaps:
                    other_zones = [*zones[:leaf_index], *zones[leaf_index + 1 :]]
                    if zones_conflict(problem, left_zone, right_zone) or any(
                        zones_conflict(problem, candidate, other)
                        for candidate in (left_zone, right_zone)
                        for other in other_zones
                    ):
                        continue
                children_cost = objective_cost(left_zone, request) + objective_cost(
                    right_zone, request
                )
                candidates.append(
                    GuillotineSplit(
                        objective_gain=parent_cost - children_cost,
                        leaf_index=leaf_index,
                        axis=axis,
                        coordinate=cut,
                        left=left,
                        right=right,
                    )
                )
    return candidates
