"""Единый детерминированный подбор поперечной фазы групп стержней."""

from __future__ import annotations

from ..contracts import LayoutProblem, LayoutZone
from .bar_geometry import (
    bars_conflict,
    intervals_overlap,
    longitudinal_interval,
    transverse_axis_gap,
    transverse_interval,
)
from .detailing import DetailingContext, build_zone_from_bbox, prepare_detailing


def feasible_first_bar_coordinates(
    problem: LayoutProblem,
    zone: LayoutZone,
) -> tuple[float, ...]:
    """Перечислить характерные фазы, сохраняющие покрытие demand_bbox."""

    axis = problem.demand.direction.axis
    demand_start, demand_end = transverse_interval(axis, zone.demand_bbox)
    half_step = zone.rebar.step / 2.0
    minimum = demand_end - zone.width_mm - half_step
    maximum = demand_start + half_step
    if maximum <= minimum:
        return (minimum,)
    values = sorted(
        {
            minimum + (maximum - minimum) * index / 8.0
            for index in range(9)
        }
    )
    return tuple(values)


def _rebuild_at_phase(
    problem: LayoutProblem,
    zone: LayoutZone,
    first_bar_coordinate_mm: float,
    context: DetailingContext,
    *,
    collect_coverage: bool,
) -> LayoutZone:
    seed_ids = zone.meta.get("seed_cell_ids")
    if not isinstance(seed_ids, tuple):
        raise ValueError(f"зона {zone.id} не хранит seed_cell_ids для подбора фазы")
    # Seed IDs describe provenance: a spatial zone may cover only part of a FE.
    # Rebuilding its bounding box from those IDs would change the partition and mass.
    return build_zone_from_bbox(
        problem,
        zone.demand_bbox,
        zone.level_index,
        zone.id,
        seed_cell_ids=seed_ids,
        collect_coverage=collect_coverage,
        context=context,
        first_bar_coordinate_mm=first_bar_coordinate_mm,
    )


def resolve_zone_phases(
    problem: LayoutProblem,
    zones: tuple[LayoutZone, ...] | list[LayoutZone],
    *,
    context: DetailingContext | None = None,
    collect_coverage: bool = True,
) -> list[LayoutZone]:
    """Постобработка: подобрать фазы и по возможности раздвинуть крайние оси.

    Метод является детерминированным baseline, а не глобальным phase-solver. Он сначала
    прижимает оси к нижней поперечной границе допустимого диапазона, затем пробует
    характерные положения внутри диапазона. Конфликты детализации используются только
    для best-effort выбора фазы и не меняют прямоугольное разбиение. При неизменных
    политиках детализации сохраняются длины, ширина, количество стержней и масса;
    seed_cell_ids не определяют границы уже построенной пространственной зоны.
    """

    if not zones:
        return []
    context = context or prepare_detailing(problem)
    axis = problem.demand.direction.axis
    indexed = list(enumerate(zones))
    indexed.sort(
        key=lambda item: (
            transverse_interval(axis, item[1].demand_bbox)[0],
            transverse_interval(axis, item[1].demand_bbox)[1],
            item[1].id,
        )
    )

    placed: list[LayoutZone] = []
    selected_by_index: dict[int, LayoutZone] = {}

    def conflicts_after_detailing(candidate: LayoutZone, other: LayoutZone) -> bool:
        if bars_conflict(
            axis,
            candidate,
            other,
            minimum_clear_spacing_mm=problem.constraints.minimum_clear_spacing_mm,
        ):
            return True
        if not problem.constraints.enforce_zone_gap:
            return False
        if not intervals_overlap(
            longitudinal_interval(axis, candidate.bbox),
            longitudinal_interval(axis, other.bbox),
        ):
            return False
        required_gap = max(candidate.rebar.step, other.rebar.step)
        return transverse_axis_gap(axis, candidate, other) < required_gap

    for original_index, zone in indexed:
        candidates = feasible_first_bar_coordinates(problem, zone)
        selected: LayoutZone | None = None
        for coordinate in candidates:
            candidate = _rebuild_at_phase(
                problem,
                zone,
                coordinate,
                context,
                collect_coverage=False,
            )
            if not any(conflicts_after_detailing(candidate, other) for other in placed):
                selected = candidate
                break
        if selected is None:
            selected = _rebuild_at_phase(
                problem,
                zone,
                zone.first_bar_coordinate_mm,
                context,
                collect_coverage=False,
            )
        placed.append(selected)
        selected_by_index[original_index] = selected

    resolved = [selected_by_index[index] for index in range(len(zones))]
    if not collect_coverage:
        return resolved
    return [
        _rebuild_at_phase(
            problem,
            zone,
            zone.first_bar_coordinate_mm,
            context,
            collect_coverage=True,
        )
        for zone in resolved
    ]
