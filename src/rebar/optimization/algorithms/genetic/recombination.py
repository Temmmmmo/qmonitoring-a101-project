"""Геометрическое скрещивание соседних кандидатов без привязки к цветовым границам."""

from __future__ import annotations

from dataclasses import replace
from time import perf_counter
from typing import TYPE_CHECKING

from rebar.models import Axis

from ...contracts import BBox, LayoutProblem
from ...services import build_zone_from_bbox, polygon_bbox_intersection_area, prepare_detailing
from ...services.cutting import CutLengthInfeasibleError
from ...services.geometry import GEOMETRY_TOLERANCE_MM, bboxes_distance, cell_bbox
from .coverage import demand_fragments

if TYPE_CHECKING:
    from ..genetic_pareto import _SearchSpace


def expand_recombined_space(
    problem: LayoutProblem,
    space: _SearchSpace,
    *,
    maximum_variants: int = 1000,
    neighbor_count: int = 8,
    deadline: float | None = None,
) -> _SearchSpace:
    """Добавить hull/поперечные полосы соседей и уточнить атомы для ВСЕХ новых границ.

    Старые индексы, seed и физические геометрии сохраняются. Кандидат слабого уровня
    вправе проходить под сильным спросом, но не заявляет его покрытие. Никакого
    сложения As, обрезки анкеровки или изменения минимальной ширины здесь нет.
    """
    from ..genetic_pareto import _covering_grid_range, _leaf_ids_for_rectangle, _PoolCandidate
    from ..spatial_partition_greedy import _Rectangle

    if maximum_variants < 0 or neighbor_count < 1:
        raise ValueError("лимиты геометрического скрещивания некорректны")
    if not maximum_variants or not space.candidates:
        return space
    context = prepare_detailing(problem)
    known = {(c.rectangle.zone.demand_bbox, c.rectangle.level_index) for c in space.candidates}
    parents = sorted(known)
    cells = tuple((cell, cell_bbox(cell)) for cell in problem.demand.cells
                  if problem.demand.level(cell.level_index).requires_extra is True)
    axis = problem.demand.direction.axis
    proposed: set[tuple[BBox, int]] = set()
    stopped = False
    for first, level in parents:
        if deadline is not None and perf_counter() >= deadline:
            stopped = True
            break
        neighbors = sorted(
            ((bboxes_distance(first, second), second) for second, other_level in parents
             if other_level == level and second != first),
            key=lambda item: (item[0], item[1]),
        )[:neighbor_count]
        for _distance, second in neighbors:
            hull = (min(first[0], second[0]), min(first[1], second[1]),
                    max(first[2], second[2]), max(first[3], second[3]))
            proposed.add((hull, level))
            # Продлить полосу каждого родителя по оси стержней: не только один hull.
            for parent in (first, second):
                strip = ((hull[0], parent[1], hull[2], parent[3]) if axis is Axis.X
                         else (parent[0], hull[1], parent[2], hull[3]))
                proposed.add((strip, level))
    ranked = []
    for bbox, level in sorted(proposed - known):
        if deadline is not None and perf_counter() >= deadline:
            stopped = True
            break
        columns = _covering_grid_range(space.grid.x_edges, bbox[0], bbox[2])
        rows = _covering_grid_range(space.grid.y_edges, bbox[1], bbox[3])
        if not space.grid.is_fully_allowed(*rows, *columns):
            continue
        eligible = [(cell, box) for cell, box in cells if cell.level_index <= level
                    and min(box[2], bbox[2]) - max(box[0], bbox[0]) > GEOMETRY_TOLERANCE_MM
                    and min(box[3], bbox[3]) - max(box[1], bbox[1]) > GEOMETRY_TOLERANCE_MM
                    and polygon_bbox_intersection_area(cell.poly, bbox) > GEOMETRY_TOLERANCE_MM]
        if not eligible:
            continue
        # Убрать пустые края demand_bbox, не трогая установленную анкеровку.
        tight = (max(bbox[0], min(box[0] for _, box in eligible)),
                 max(bbox[1], min(box[1] for _, box in eligible)),
                 min(bbox[2], max(box[2] for _, box in eligible)),
                 min(bbox[3], max(box[3] for _, box in eligible)))
        if (tight, level) in known:
            continue
        try:
            zone = build_zone_from_bbox(problem, tight, level, "recombined-proposal",
                                        seed_cell_ids=(c.id for c, _ in eligible),
                                        collect_coverage=False, context=context)
        except CutLengthInfeasibleError:
            continue
        source_ids = tuple(c.id for c, _ in eligible)
        rectangle = _Rectangle(6_000_000 + len(ranked), *rows, *columns, level, source_ids, zone)
        area = sum(polygon_bbox_intersection_area(c.poly, tight) for c, _ in eligible)
        ranked.append((zone.mass_kg / area, tight, level, rectangle))
        known.add((tight, level))
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    added = tuple(item[3] for item in ranked[:maximum_variants])
    if not added:
        return replace(space, stopped_by_time_limit=space.stopped_by_time_limit or stopped)
    rectangles = (*tuple(c.rectangle for c in space.candidates), *added)
    leaves = demand_fragments(problem, space.grid, tuple(r.zone.demand_bbox for r in rectangles))
    candidates = tuple(replace(c, leaf_ids=_leaf_ids_for_rectangle(c.rectangle, leaves))
                       for c in space.candidates) + tuple(_PoolCandidate(
        rectangle=r, leaf_ids=_leaf_ids_for_rectangle(r, leaves), source_cell_ids=r.source_cell_ids,
        origins=frozenset(("geometric-recombination",)),
    ) for r in added)
    return replace(space, candidates=candidates, leaves=leaves, leaf_count=len(leaves),
                   stopped_by_time_limit=space.stopped_by_time_limit or stopped)
