"""Геометрия дискретных осей, восстанавливаемая из параметров ``LayoutZone``."""

from __future__ import annotations

import math
from dataclasses import dataclass

from rebar.models import Axis

from ..contracts import BBox, LayoutZone
from .geometry import GEOMETRY_TOLERANCE_MM


@dataclass(frozen=True)
class BarSegment:
    """Один прямой стержень в локальном представлении валидатора."""

    coordinate_mm: float
    longitudinal_start_mm: float
    longitudinal_end_mm: float


def longitudinal_interval(axis: Axis, bbox: BBox) -> tuple[float, float]:
    """Вернуть интервал bbox вдоль направления стержней."""

    return (bbox[0], bbox[2]) if axis is Axis.X else (bbox[1], bbox[3])


def transverse_interval(axis: Axis, bbox: BBox) -> tuple[float, float]:
    """Вернуть интервал bbox поперёк направления стержней."""

    return (bbox[1], bbox[3]) if axis is Axis.X else (bbox[0], bbox[2])


def coverage_bbox(axis: Axis, bbox: BBox, step_mm: float) -> BBox:
    """Расширить envelope осей до области обслуживания на полшага с краёв."""

    half_step = step_mm / 2.0
    xmin, ymin, xmax, ymax = bbox
    if axis is Axis.X:
        return xmin, ymin - half_step, xmax, ymax + half_step
    return xmin - half_step, ymin, xmax + half_step, ymax


def zone_coverage_bbox(axis: Axis, zone: LayoutZone) -> BBox:
    """Вернуть область обслуживания параметрической группы."""

    return coverage_bbox(axis, zone.bbox, zone.rebar.step)


def bar_coordinates(zone: LayoutZone) -> tuple[float, ...]:
    """Развернуть фазу, шаг и количество в поперечные координаты осей."""

    if zone.bar_count < 1 or zone.rebar.step <= 0:
        return ()
    return tuple(
        zone.first_bar_coordinate_mm + index * zone.rebar.step
        for index in range(zone.bar_count)
    )


def bar_segments(axis: Axis, zone: LayoutZone) -> tuple[BarSegment, ...]:
    """Развернуть параметрическую группу во временные прямые сегменты."""

    start, end = longitudinal_interval(axis, zone.bbox)
    return tuple(BarSegment(coordinate, start, end) for coordinate in bar_coordinates(zone))


def intervals_overlap(first: tuple[float, float], second: tuple[float, float]) -> bool:
    """Проверить пересечение внутренностей одномерных интервалов."""

    return min(first[1], second[1]) - max(first[0], second[0]) > GEOMETRY_TOLERANCE_MM


def transverse_axis_gap(axis: Axis, first: LayoutZone, second: LayoutZone) -> float:
    """Вернуть зазор между крайними осями двух неперекрывающихся групп."""

    first_start, first_end = transverse_interval(axis, first.bbox)
    second_start, second_end = transverse_interval(axis, second.bbox)
    return max(second_start - first_end, first_start - second_end, 0.0)


def bars_conflict(
    axis: Axis,
    first: LayoutZone,
    second: LayoutZone,
    *,
    minimum_clear_spacing_mm: float = 0.0,
) -> bool:
    """Проверить физическое наложение параллельных стержней двух групп."""

    if not intervals_overlap(
        longitudinal_interval(axis, first.bbox),
        longitudinal_interval(axis, second.bbox),
    ):
        return False

    minimum_axis_distance = (
        first.rebar.diameter / 2.0
        + second.rebar.diameter / 2.0
        + minimum_clear_spacing_mm
    )
    first_start, first_end = transverse_interval(axis, first.bbox)
    second_start, second_end = transverse_interval(axis, second.bbox)
    if (
        max(second_start - first_end, first_start - second_end, 0.0)
        + GEOMETRY_TOLERANCE_MM
        >= minimum_axis_distance
    ):
        return False

    # Два отсортированных арифметических ряда сравниваются за O(n + m), а не
    # декартовым произведением всех стержней. Это критично при переборе BSP-разрезов.
    first_index = max(
        0,
        math.floor(
            (second_start - minimum_axis_distance - first.first_bar_coordinate_mm)
            / first.rebar.step
        ),
    )
    second_index = max(
        0,
        math.floor(
            (first_start - minimum_axis_distance - second.first_bar_coordinate_mm)
            / second.rebar.step
        ),
    )
    while first_index < first.bar_count and second_index < second.bar_count:
        first_coordinate = (
            first.first_bar_coordinate_mm + first_index * first.rebar.step
        )
        second_coordinate = (
            second.first_bar_coordinate_mm + second_index * second.rebar.step
        )
        distance = math.fabs(first_coordinate - second_coordinate)
        if distance + GEOMETRY_TOLERANCE_MM < minimum_axis_distance:
            return True
        if first_coordinate < second_coordinate:
            first_index += 1
        else:
            second_index += 1
    return False
