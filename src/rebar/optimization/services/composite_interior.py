"""Явная внутренняя область для поиска, без удаления исходного спроса.

Первый профиль работает с целыми КЭ: пересечённый границей КЭ целиком остаётся
исключением. Это консервативнее геометрического клиппинга; его площадь не исчезает
из полной проверки и не засчитывается как сэкономленная арматура.
"""
from __future__ import annotations

import math

from rebar.models import Axis

from ..contracts.composite_search import CompositeSearchProblem
from ..contracts.problem import BBox
from .composite_coverage import _service_interval, validate_composite_demand
from .composite_detailing import validate_recipe_placement
from .geometry import cell_bbox, polygon_area, polygon_in_bbox

INTERIOR_POLICY = "whole-cell-interior-with-explicit-residual/research-v1"


def admissible_composite_bboxes(problem: CompositeSearchProblem) -> dict[int, BBox | None]:
    """Продольно cover+40d; поперечно — радиус/cover и область обслуживания осей.

    Прямоугольник ограничивает demand_bbox, не обрезает готовую арматуру. Каталог
    раскроя и проёмы проверяются после детализации каждого кандидата отдельно.
    """
    if problem.host_envelope is None:
        raise ValueError("внутренний поиск требует явный host")
    validate_composite_demand(problem.demand)
    host, demand, constraints = problem.host_envelope, problem.demand, problem.constraints
    if not math.isfinite(constraints.anchorage_diameters):
        raise ValueError("анкеровка должна быть конечной")
    placements = dict(problem.placements)
    axis = demand.direction.axis
    along, across = ((0, 2), (1, 3)) if axis is Axis.X else ((1, 3), (0, 2))
    result = {}
    for level in demand.levels:
        if not level.requires_extra:
            continue
        recipe, placement = level.recipe, placements[level.index]
        validate_recipe_placement(recipe, placement)
        diameter = max(spec.diameter for spec in recipe.additions)
        extension = constraints.anchorage_diameters * diameter
        start = host.outer_mm[along[0]] + host.side_cover_mm + extension
        end = host.outer_mm[along[1]] - host.side_cover_mm - extension
        lower = host.outer_mm[across[0]] + host.side_cover_mm + diameter / 2
        upper = host.outer_mm[across[1]] - host.side_cover_mm - diameter / 2
        if start >= end or lower >= upper:
            result[level.index] = None
            continue
        try:
            services = [_service_interval(part, (lower, upper)) for part in placement.additions]
        except ValueError:  # no actual axes fit; do not fabricate an empty covering set
            result[level.index] = None
            continue
        lower = max(lower, *(pair[0] for pair in services))
        upper = min(upper, *(pair[1] for pair in services))
        result[level.index] = ((start, lower, end, upper) if axis is Axis.X else (lower, start, upper, end)) if lower < upper else None
    return result


def partition_interior_demand(problem: CompositeSearchProblem) -> dict:
    boxes = admissible_composite_bboxes(problem)
    # One fixed rectangle for all recipes: do not change the task while selecting
    # a diameter. Per-recipe strips would leave targets a stronger zone cannot serve.
    common = None
    if boxes and all(box is not None for box in boxes.values()):
        common = (max(box[0] for box in boxes.values()), max(box[1] for box in boxes.values()),
                  min(box[2] for box in boxes.values()), min(box[3] for box in boxes.values()))
        if common[0] >= common[2] or common[1] >= common[3]:
            common = None
    eligible, exceptions = [], []
    total_area = eligible_area = 0.0
    for cell in problem.demand.cells:
        if not problem.demand.level(cell.level_index).requires_extra:
            continue
        area = polygon_area(cell.poly)
        total_area += area
        box = common
        if box is not None and polygon_in_bbox(cell.poly, box):
            eligible.append(cell.id)
            eligible_area += area
        else:
            exceptions.append({"cell_id": cell.id, "level_index": cell.level_index,
                               "bbox_mm": cell_bbox(cell), "area_mm2": area,
                               "reason": "whole_cell_not_inside_common_admissible_rectangle"})
    return {"policy_id": INTERIOR_POLICY, "scope": "one_direction_whole_cells_not_clipped_fragments",
            "original_demanded_cell_count": len(eligible) + len(exceptions),
            "original_demanded_area_mm2": total_area,
            "target_cell_ids": eligible, "target_cell_count": len(eligible), "target_area_mm2": eligible_area,
            "boundary_exception_cell_count": len(exceptions), "boundary_exceptions": exceptions,
            "boundary_exception_area_mm2": math.fsum(row["area_mm2"] for row in exceptions),
            "admissible_bboxes_by_level_mm": boxes, "common_target_bbox_mm": common,
            "source_demand_modified": False,
            "minimum_width_source": "all_original_FE_not_the_restricted_subset",
            "placement_eligible": False,
            "warning": "Краевые КЭ сохранены целиком в исходной задаче и итоговом покрытии. "
                       "Внутренняя масса не является массой завершённой плиты. Проёмы/раскрой проверяются отдельно."}
