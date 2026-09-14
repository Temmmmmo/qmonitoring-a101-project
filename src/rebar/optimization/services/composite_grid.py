"""Консервативные атомы прямоугольного покрытия, не ресэмплинг по центрам КЭ."""
from __future__ import annotations

import math

from rebar.models import Axis

from ..contracts.composite_search import CompositeSearchProblem
from .geometry import polygon_area, polygon_bbox_intersection_area


def make_fragment_grid(problem: CompositeSearchProblem, partition: dict, *, along_step_mm: float, across_step_mm: float,
                       across_origin_mm: float | None = None) -> dict:
    """В каждом атоме сохраняются ВСЕ уровни пересечённых целевых КЭ.

    Край и множество целевых ID задаёт тот же interior-профиль, что у целоклеточного
    контроля. Граница атома может пересекать КЭ. Ни спрос, ни размер КЭ не изменяются.
    """
    for step in (along_step_mm, across_step_mm):
        if isinstance(step, bool) or not math.isfinite(step) or step <= 0:
            raise ValueError("шаг сетки должен быть конечным положительным")
    if across_origin_mm is not None and (isinstance(across_origin_mm, bool) or not math.isfinite(across_origin_mm)
                                          or abs(across_origin_mm) > 1e9):
        raise ValueError("начало поперечной сетки должно быть конечным в пределах 1e9 мм")
    box = partition["common_target_bbox_mm"]
    if box is None:
        raise ValueError("нет внутренней области")
    along, across = ((0, 2), (1, 3)) if problem.demand.direction.axis is Axis.X else ((1, 3), (0, 2))

    def coordinates(indices, step, origin=None):
        lo, hi = box[indices[0]], box[indices[1]]
        if origin is not None:
            # Only move INTERNAL partition lines. Original target/envelope and
            # all edge atoms remain; this is not another boundary exclusion.
            first = math.floor((lo - origin) / step) + 1
            last = math.ceil((hi - origin) / step) - 1
            if last - first + 2 > 64:
                raise ValueError("не более 64 интервалов сетки на ось")
            internal = [origin + k * step for k in range(first, last + 1)]
            return (lo, *(x for x in internal if lo + 1e-6 < x < hi - 1e-6), hi)
        count = math.ceil((hi - lo) / step)
        if count > 64:
            raise ValueError("не более 64 интервалов сетки на ось")
        return tuple([lo + i * step for i in range(count)] + [hi])

    xs, ys = coordinates(along, along_step_mm), coordinates(across, across_step_mm, across_origin_mm)
    nx, ny = len(xs) - 1, len(ys) - 1
    state_count = nx * (nx + 1) * ny * (ny + 1) // 4
    if nx * ny > 1024 or state_count > 50000:
        raise ValueError("сетка превышает 1024 атома / 50000 прямоугольных состояний")
    targets = set(partition["target_cell_ids"])
    cells = tuple(c for c in problem.demand.cells if c.id in targets)
    if len(cells) * nx * ny > 3000000:
        raise ValueError("превышен лимит 3000000 пар КЭ-атом")
    rows, areas, fragments = [], [], 0
    area_by_cell = {cell.id: 0.0 for cell in cells}
    for j in range(ny):
        row = []
        for i in range(nx):
            rect = grid_bbox(problem.demand.direction.axis, xs, ys, (i, i + 1, j, j + 1))
            mask = 0
            for cell in cells:
                area = polygon_bbox_intersection_area(cell.poly, rect)
                if area > 0:
                    mask |= 1 << cell.level_index
                    area_by_cell[cell.id] += area
                    areas.append(area)
                    fragments += 1
            row.append(mask)
        rows.append(tuple(row))
    for cell in cells:
        if not math.isclose(area_by_cell[cell.id], polygon_area(cell.poly), rel_tol=1e-9, abs_tol=1e-6):
            raise ValueError(f"сетка потеряла площадь целевого КЭ {cell.id}")
    return {"along_coordinates_mm": xs, "across_coordinates_mm": ys, "required_masks": tuple(rows),
            "fragment_count": fragments, "target_area_mm2": math.fsum(areas), "rectangle_state_count": state_count,
            "target_cell_count": len(cells), "source_demand_modified": False,
            "policy": "positive_polygon_intersections_not_centroid_sampling",
            "across_origin_mm": across_origin_mm}


def grid_bbox(axis, along, across, state):
    a, b, c, d = state
    return ((along[a], across[c], along[b], across[d]) if axis is Axis.X
            else (across[c], along[a], across[d], along[b]))
