"""Явное расширение окна до обслуживающих осей периодической схемы.

Не обрезает оси по host. После этого шага host проверяется отдельно.
"""
from __future__ import annotations

import math

from rebar.models import Axis

from ..contracts import BBox, DemandMap, LayoutConstraints
from ..contracts.placement import RecipePlacement
from .axis_patterns import pattern_coordinates
from .bar_geometry import transverse_interval
from .detailing import typical_transverse_cell_size_mm
from .composite_detailing import CompositeDetailingContext
from .geometry import GEOMETRY_TOLERANCE_MM


def covering_composite_window(
    demand: DemandMap, bbox: BBox, level_index: int, placement: RecipePlacement,
    *, constraints: LayoutConstraints, admissible_bbox: BBox | None = None,
    context: CompositeDetailingContext | None = None,
) -> BBox:
    """Сохранить исходный bbox внутри окна, включив крайние обслуживающие оси.

    Кратность — НОК условных шагов всех добавок. Это выбор геометрии кандидата,
    а не доказательство минимальной ширины фактического набора или нормативности.
    """
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox) or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError("нужно конечное невырожденное окно")
    if admissible_bbox is not None and (len(admissible_bbox) != 4
            or not all(math.isfinite(v) for v in admissible_bbox)
            or admissible_bbox[0] >= admissible_bbox[2] or admissible_bbox[1] >= admissible_bbox[3]):
        raise ValueError("нужна конечная невырожденная допустимая область")
    recipe = demand.level(level_index).recipe
    if recipe is None or not recipe.additions or len(placement.additions) != len(recipe.additions):
        raise ValueError("нужны все добавки и их размещения")
    lower, upper = transverse_interval(demand.direction.axis, bbox)
    lo, hi = lower, upper
    for axes in placement.additions:
        period = axes.pattern.period_mm
        for coordinate in (lower, upper):
            neighbours = pattern_coordinates(axes, (coordinate - 2 * period, coordinate + 2 * period))
            # Voronoi cell of the nearest axis contains the boundary; at a tie choose
            # the interior axis so that a boundary on a midpoint needs no extra bar.
            selected = min(neighbours, key=lambda value: (
                abs(value - coordinate), -value if coordinate == lower else value))
            lo, hi = min(lo, selected), max(hi, selected)
    if context is not None and context.demand is not demand:
        raise ValueError("контекст детализации не соответствует исходной задаче")
    minimum = constraints.min_width_cells * (typical_transverse_cell_size_mm(demand)
        if context is None else context.typical_transverse_size_mm)
    steps = [spec.step for spec in recipe.additions]
    if any(not isinstance(step, int) or isinstance(step, bool) or step <= 0 for step in steps):
        raise ValueError("кратность окна требует положительные целые условные шаги")
    quantum = math.lcm(*steps)
    width = quantum * math.ceil((max(hi - lo, minimum) - GEOMETRY_TOLERANCE_MM) / quantum)
    required_lo, required_hi = lo, hi
    middle = (lo + hi) / 2
    lo, hi = middle - width / 2, middle + width / 2
    if admissible_bbox is not None:
        bound_lo, bound_hi = transverse_interval(demand.direction.axis, admissible_bbox)
        # Move the expanded window as a whole; never shorten installed bars or omit
        # the axes needed to service the original request bbox.
        start_min, start_max = max(bound_lo, required_hi - width), min(required_lo, bound_hi - width)
        if start_min > start_max + GEOMETRY_TOLERANCE_MM:
            raise ValueError("минимальное периодическое окно не помещается в допустимую область")
        lo = min(max(lo, start_min), start_max)
        hi = lo + width
        along = (0, 2) if demand.direction.axis is Axis.X else (1, 3)
        if bbox[along[0]] < admissible_bbox[along[0]] - GEOMETRY_TOLERANCE_MM or bbox[along[1]] > admissible_bbox[along[1]] + GEOMETRY_TOLERANCE_MM:
            raise ValueError("продольный спрос вне допустимой области")
    return (bbox[0], lo, bbox[2], hi) if demand.direction.axis is Axis.X else (lo, bbox[1], hi, bbox[3])
