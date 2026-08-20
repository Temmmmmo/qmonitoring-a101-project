"""Общая механика допустимых гильотинных разрезов для baseline-алгоритмов."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from ..contracts import AlgorithmRequest, LayoutProblem, LayoutZone
from ..services import (
    DetailingContext,
    build_zone,
    partition_zones_conflict,
    resolve_zone_phases,
)


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


def finalize_compatible_leaves(
    problem: LayoutProblem,
    leaves: list[tuple[int, ...]],
    prefix: str,
    context: DetailingContext,
) -> tuple[list[tuple[int, ...]], list[LayoutZone], list[dict[str, int]]]:
    """Подобрать фазы и применить необязательный строгий профиль overlap."""

    finalized_leaves = list(leaves)
    merge_log: list[dict[str, int]] = []
    while True:
        zones = resolve_zone_phases(
            problem,
            [
                zone_for_ids(
                    problem,
                    ids,
                    f"{prefix}-{index}",
                    context,
                    collect_coverage=False,
                )
                for index, ids in enumerate(finalized_leaves, 1)
            ],
            context=context,
            collect_coverage=False,
        )
        conflict = next(
            (
                (first, second)
                for first, zone in enumerate(zones)
                for second in range(first + 1, len(zones))
                if partition_zones_conflict(problem, zone, zones[second])
            ),
            None,
        )
        if conflict is None:
            final_zones = resolve_zone_phases(
                problem,
                [
                    zone_for_ids(
                        problem,
                        ids,
                        f"{prefix}-{index}",
                        context,
                    )
                    for index, ids in enumerate(finalized_leaves, 1)
                ],
                context=context,
            )
            return finalized_leaves, final_zones, merge_log

        first, second = conflict
        finalized_leaves[first] = tuple(
            sorted((*finalized_leaves[first], *finalized_leaves[second]))
        )
        del finalized_leaves[second]
        merge_log.append({"first": first, "second": second})


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
    maximum_cuts_per_axis = int(
        request.params.get("maximum_guillotine_cuts_per_axis", 64)
    )
    if maximum_cuts_per_axis < 1:
        raise ValueError("maximum_guillotine_cuts_per_axis должен быть не меньше 1")
    for leaf_index, ids in enumerate(leaves):
        if len(ids) < 2:
            continue
        parent_cost = objective_cost(zones[leaf_index], request)
        for axis in (0, 1):
            coordinates = sorted({by_id[cell_id].centroid[axis] for cell_id in ids})
            intervals = list(pairwise(coordinates))
            if len(intervals) > maximum_cuts_per_axis:
                if maximum_cuts_per_axis == 1:
                    intervals = [intervals[len(intervals) // 2]]
                else:
                    indexes = {
                        round(
                            index
                            * (len(intervals) - 1)
                            / (maximum_cuts_per_axis - 1)
                        )
                        for index in range(maximum_cuts_per_axis)
                    }
                    intervals = [intervals[index] for index in sorted(indexes)]
            for lower, upper in intervals:
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
