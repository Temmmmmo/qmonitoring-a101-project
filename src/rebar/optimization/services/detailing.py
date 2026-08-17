"""Единые инженерные формулы построения прямоугольной детали."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable
from dataclasses import dataclass

from rebar.models import Axis

from ..contracts import DemandCell, LayoutProblem, LayoutZone
from .geometry import GEOMETRY_TOLERANCE_MM, cell_bbox, point_in_bbox

STEEL_KG_PER_M_PER_MM2 = 0.006165


@dataclass(frozen=True)
class DetailingContext:
    """Предвычисления одной задачи, переиспользуемые при переборе кандидатов."""

    cells_by_id: dict[int, DemandCell]
    typical_transverse_cell_size_mm: float


def demanded_cells(problem: LayoutProblem) -> tuple[DemandCell, ...]:
    """Вернуть КЭ, которым назначена дополнительная арматура."""

    return tuple(
        cell
        for cell in problem.demand.cells
        if problem.demand.level(cell.level_index).requires_extra is True
    )


def _typical_transverse_cell_size(problem: LayoutProblem) -> float:
    axis = problem.demand.direction.axis
    spans: list[float] = []
    for cell in problem.demand.cells:
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
        typical_transverse_cell_size_mm=_typical_transverse_cell_size(problem),
    )


def build_zone(
    problem: LayoutProblem,
    cell_ids: Iterable[int],
    level_index: int,
    zone_id: str,
    *,
    collect_coverage: bool = True,
    context: DetailingContext | None = None,
) -> LayoutZone:
    """Детализировать охватывающий прямоугольник по единым формулам.

    Это временная MVP-детализация. Минимальная ширина оценивается через медианный
    поперечный размер КЭ, а достаточность покрытия — по центроидам. Обе аппроксимации
    записываются в ``meta`` и должны быть заменены после подтверждения правил инженером.
    """

    ids = tuple(sorted(set(cell_ids)))
    if not ids:
        raise ValueError("для построения зоны нужен хотя бы один КЭ")
    context = context or prepare_detailing(problem)
    try:
        selected = [context.cells_by_id[cell_id] for cell_id in ids]
    except KeyError as error:
        raise KeyError(f"КЭ {error.args[0]} отсутствует в карте спроса") from error

    level = problem.demand.level(level_index)
    if level.additional is None:
        raise ValueError(f"уровень {level_index} не задаёт дополнительную арматуру")
    if any(cell.level_index > level_index for cell in selected):
        raise ValueError("уровень зоны слабее требования выбранных КЭ")

    boxes = [cell_bbox(cell) for cell in selected]
    xmin = min(box[0] for box in boxes)
    ymin = min(box[1] for box in boxes)
    xmax = max(box[2] for box in boxes)
    ymax = max(box[3] for box in boxes)
    axis = problem.demand.direction.axis

    required_length = xmax - xmin if axis is Axis.X else ymax - ymin
    raw_width = ymax - ymin if axis is Axis.X else xmax - xmin
    typical_cell_width = context.typical_transverse_cell_size_mm
    minimum_width = problem.constraints.min_width_cells * typical_cell_width
    width = max(raw_width, minimum_width)
    width_padding = (width - raw_width) / 2.0
    anchorage = problem.constraints.anchorage_diameters * level.additional.diameter

    if axis is Axis.X:
        bbox = (xmin - anchorage, ymin - width_padding, xmax + anchorage, ymax + width_padding)
    else:
        bbox = (xmin - width_padding, ymin - anchorage, xmax + width_padding, ymax + anchorage)

    installed_length = required_length + 2.0 * anchorage
    bar_count = math.ceil(width / level.additional.step) + 1
    mass = (
        STEEL_KG_PER_M_PER_MM2
        * level.additional.diameter**2
        * (installed_length / 1000.0)
        * bar_count
    )

    covered: list[int] = []
    overcovered: list[int] = []
    if collect_coverage:
        for cell in problem.demand.cells:
            if not point_in_bbox(cell.centroid, bbox):
                continue
            cell_level = problem.demand.level(cell.level_index)
            if cell_level.requires_extra is True and level_index >= cell.level_index:
                covered.append(cell.id)
            if cell_level.requires_extra is not True or level_index > cell.level_index:
                overcovered.append(cell.id)

    return LayoutZone(
        id=zone_id,
        bbox=bbox,
        level_index=level_index,
        rebar=level.additional,
        width_mm=width,
        required_length_mm=required_length,
        installed_length_mm=installed_length,
        bar_count=bar_count,
        mass_kg=mass,
        covered_cell_ids=tuple(covered),
        overcovered_cell_ids=tuple(overcovered),
        meta={
            "seed_cell_ids": ids,
            "coverage_rule": "cell_centroid_inside_zone",
            "minimum_width_rule": "median_transverse_cell_size",
            "typical_transverse_cell_size_mm": typical_cell_width,
            "anchorage_each_end_mm": anchorage,
        },
    )
