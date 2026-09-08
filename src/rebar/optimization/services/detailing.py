"""Единые инженерные формулы построения прямоугольной детали."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from rebar.models import Axis

from ..contracts import DemandCell, DemandMap, LayoutProblem, LayoutZone
from .anchorage import FixedDiameterAnchoragePolicy
from .bar_geometry import coverage_bbox
from .cutting import select_installed_length_mm
from .geometry import (
    GEOMETRY_TOLERANCE_MM,
    cell_bbox,
    polygon_bbox_intersection_area,
)
from .spatial_index import CellSpatialIndex

STEEL_KG_PER_M_PER_MM2 = 0.006165


@dataclass(frozen=True)
class DetailingContext:
    """Предвычисления одной задачи, переиспользуемые при переборе кандидатов."""

    cells_by_id: dict[int, DemandCell]
    typical_transverse_cell_size_mm: float
    spatial_index: CellSpatialIndex | None = None


def demanded_cells(problem: LayoutProblem) -> tuple[DemandCell, ...]:
    """Вернуть КЭ, которым назначена дополнительная арматура."""

    return tuple(
        cell
        for cell in problem.demand.cells
        if problem.demand.level(cell.level_index).requires_extra is True
    )


def typical_transverse_cell_size_mm(demand: DemandMap) -> float:
    """Общий перевод минимальной ширины в КЭ в миллиметры, включая составные схемы."""
    axis = demand.direction.axis
    spans: list[float] = []
    for cell in demand.cells:
        xmin, ymin, xmax, ymax = cell_bbox(cell)
        span = ymax - ymin if axis is Axis.X else xmax - xmin
        if span > GEOMETRY_TOLERANCE_MM:
            spans.append(span)
    if not spans:
        raise ValueError("невозможно определить поперечный размер КЭ")
    return statistics.median(spans)


def prepare_detailing(problem: LayoutProblem) -> DetailingContext:
    """Один раз подготовить индексы и статистики геометрии задачи."""

    return DetailingContext(
        cells_by_id={cell.id: cell for cell in problem.demand.cells},
        typical_transverse_cell_size_mm=typical_transverse_cell_size_mm(problem.demand),
        spatial_index=CellSpatialIndex.build(problem.demand.cells),
    )


def rebar_mass_kg(
    diameter_mm: int,
    installed_length_mm: float,
    bar_count: int,
) -> float:
    """Посчитать массу одинаковых продольных стержней одной зоны."""

    if diameter_mm <= 0:
        raise ValueError("диаметр арматуры должен быть положительным")
    if installed_length_mm <= 0:
        raise ValueError("установленная длина должна быть положительной")
    if bar_count < 1:
        raise ValueError("набор должен содержать хотя бы один стержень")
    return (
        STEEL_KG_PER_M_PER_MM2
        * diameter_mm**2
        * (installed_length_mm / 1000.0)
        * bar_count
    )


def build_zone(
    problem: LayoutProblem,
    cell_ids: Iterable[int],
    level_index: int,
    zone_id: str,
    *,
    collect_coverage: bool = True,
    context: DetailingContext | None = None,
    first_bar_coordinate_mm: float | None = None,
) -> LayoutZone:
    """Детализировать группу стержней по единым формулам и проектным политикам."""

    ids = tuple(sorted(set(cell_ids)))
    if not ids:
        raise ValueError("для построения зоны нужен хотя бы один КЭ")
    context = context or prepare_detailing(problem)
    try:
        selected = [context.cells_by_id[cell_id] for cell_id in ids]
    except KeyError as error:
        raise KeyError(f"КЭ {error.args[0]} отсутствует в карте спроса") from error

    if any(cell.level_index > level_index for cell in selected):
        raise ValueError("уровень зоны слабее требования выбранных КЭ")

    boxes = [cell_bbox(cell) for cell in selected]
    demand_bbox = (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )
    return build_zone_from_bbox(
        problem,
        demand_bbox,
        level_index,
        zone_id,
        seed_cell_ids=ids,
        collect_coverage=collect_coverage,
        context=context,
        first_bar_coordinate_mm=first_bar_coordinate_mm,
    )


def build_zone_from_bbox(
    problem: LayoutProblem,
    demand_bbox: tuple[float, float, float, float],
    level_index: int,
    zone_id: str,
    *,
    seed_cell_ids: Iterable[int] = (),
    collect_coverage: bool = True,
    context: DetailingContext | None = None,
    first_bar_coordinate_mm: float | None = None,
) -> LayoutZone:
    """Детализировать уже построенный пространственный прямоугольник.

    В отличие от :func:`build_zone`, прямоугольник здесь является первичным объектом,
    а не bbox произвольного множества КЭ. Это позволяет пространственным алгоритмам
    делить один КЭ между соседними непересекающимися зонами.
    """

    context = context or prepare_detailing(problem)
    ids = tuple(sorted(set(seed_cell_ids)))
    unknown_ids = [cell_id for cell_id in ids if cell_id not in context.cells_by_id]
    if unknown_ids:
        raise KeyError(f"КЭ {unknown_ids[0]} отсутствует в карте спроса")

    xmin, ymin, xmax, ymax = demand_bbox
    if not all(math.isfinite(value) for value in demand_bbox):
        raise ValueError("demand_bbox должен содержать конечные координаты")
    if xmax - xmin <= GEOMETRY_TOLERANCE_MM or ymax - ymin <= GEOMETRY_TOLERANCE_MM:
        raise ValueError("demand_bbox должен иметь положительную площадь")

    level = problem.demand.level(level_index)
    if level.additional is None:
        raise ValueError(f"уровень {level_index} не задаёт дополнительную арматуру")
    if level.additional.step <= 0:
        raise ValueError("шаг арматуры должен быть положительным")
    if level.additional.diameter <= 0:
        raise ValueError("диаметр арматуры должен быть положительным")
    if any(context.cells_by_id[cell_id].level_index > level_index for cell_id in ids):
        raise ValueError("уровень зоны слабее требования выбранных КЭ")
    axis = problem.demand.direction.axis

    required_length = xmax - xmin if axis is Axis.X else ymax - ymin
    raw_width = ymax - ymin if axis is Axis.X else xmax - xmin
    transverse_lower = ymin if axis is Axis.X else xmin
    transverse_upper = ymax if axis is Axis.X else xmax
    typical_cell_width = context.typical_transverse_cell_size_mm
    minimum_width = problem.constraints.min_width_cells * typical_cell_width
    target_width = max(raw_width, minimum_width)
    width_spaces = max(
        1,
        math.ceil(
            (target_width - GEOMETRY_TOLERANCE_MM) / level.additional.step
        ),
    )
    width = float(width_spaces * level.additional.step)
    if first_bar_coordinate_mm is None:
        first_bar_coordinate_mm = transverse_lower - (width - raw_width) / 2.0
    last_bar_coordinate_mm = first_bar_coordinate_mm + width
    half_step = level.additional.step / 2.0
    if (
        first_bar_coordinate_mm - half_step > transverse_lower + GEOMETRY_TOLERANCE_MM
        or last_bar_coordinate_mm + half_step + GEOMETRY_TOLERANCE_MM < transverse_upper
    ):
        raise ValueError("фаза стержней не покрывает поперечный интервал выбранных КЭ")

    anchorage_policy = FixedDiameterAnchoragePolicy(
        problem.constraints.anchorage_diameters
    )
    anchorage = anchorage_policy.extension_each_end_mm(level.additional)
    anchored_length = required_length + 2.0 * anchorage
    installed_length = select_installed_length_mm(
        anchored_length,
        problem.constraints.allowed_cut_lengths_mm,
    )
    installed_extension_each_end = (installed_length - required_length) / 2.0

    if axis is Axis.X:
        bbox = (
            xmin - installed_extension_each_end,
            first_bar_coordinate_mm,
            xmax + installed_extension_each_end,
            last_bar_coordinate_mm,
        )
    else:
        bbox = (
            first_bar_coordinate_mm,
            ymin - installed_extension_each_end,
            last_bar_coordinate_mm,
            ymax + installed_extension_each_end,
        )

    bar_count = width_spaces + 1
    mass = rebar_mass_kg(level.additional.diameter, installed_length, bar_count)

    covered: list[int] = []
    overcovered: list[int] = []
    if collect_coverage:
        service_bbox = coverage_bbox(axis, bbox, level.additional.step)
        query_bbox = (min(service_bbox[0], demand_bbox[0]), min(service_bbox[1], demand_bbox[1]),
                      max(service_bbox[2], demand_bbox[2]), max(service_bbox[3], demand_bbox[3]))
        cells = (problem.demand.cells if context.spatial_index is None
                 else context.spatial_index.query(query_bbox))
        for cell in cells:
            demand_intersection_area = polygon_bbox_intersection_area(
                cell.poly,
                demand_bbox,
            )
            cell_level = problem.demand.level(cell.level_index)
            if (
                demand_intersection_area > GEOMETRY_TOLERANCE_MM
                and cell_level.requires_extra is True
                and level_index >= cell.level_index
            ):
                covered.append(cell.id)
            service_intersection_area = polygon_bbox_intersection_area(
                cell.poly,
                service_bbox,
            )
            if service_intersection_area <= GEOMETRY_TOLERANCE_MM:
                continue
            if cell_level.requires_extra is not True or level_index > cell.level_index:
                overcovered.append(cell.id)

    return LayoutZone(
        id=zone_id,
        bbox=bbox,
        demand_bbox=demand_bbox,
        level_index=level_index,
        rebar=level.additional,
        width_mm=width,
        required_length_mm=required_length,
        anchored_length_mm=anchored_length,
        installed_length_mm=installed_length,
        first_bar_coordinate_mm=first_bar_coordinate_mm,
        bar_count=bar_count,
        mass_kg=mass,
        covered_cell_ids=tuple(covered),
        overcovered_cell_ids=tuple(overcovered),
        meta={
            "seed_cell_ids": ids,
            "construction": "spatial_bbox" if not ids else "cell_group_bbox",
            "coverage_rule": "union_area_of_sufficient_demand_bboxes",
            "minimum_width_rule": "median_transverse_cell_size",
            "typical_transverse_cell_size_mm": typical_cell_width,
            "minimum_width_mm": minimum_width,
            "width_space_count": width_spaces,
            "anchorage_each_end_mm": anchorage,
            "anchorage_policy": anchorage_policy.name,
            "cutting_profile": problem.constraints.cutting_profile,
            "allowed_cut_lengths_mm": problem.constraints.allowed_cut_lengths_mm,
            "installed_extra_each_end_mm": installed_extension_each_end,
        },
    )
