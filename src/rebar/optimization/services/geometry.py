"""Низкоуровневые геометрические операции без инженерной семантики."""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise

from ..contracts import BBox, DemandCell

GEOMETRY_TOLERANCE_MM = 1e-6


def cell_bbox(cell: DemandCell) -> BBox:
    """Вернуть осевой bbox конечного элемента."""

    xs = [point[0] for point in cell.poly]
    ys = [point[1] for point in cell.poly]
    return min(xs), min(ys), max(xs), max(ys)


def polygon_area(points: Sequence[tuple[float, float]]) -> float:
    """Вычислить абсолютную площадь простого полигона формулой шнуровки."""

    if len(points) < 3:
        return 0.0
    return abs(
        sum(
            x1 * y2 - x2 * y1
            for (x1, y1), (x2, y2) in zip(points, (*points[1:], points[0]))
        )
        / 2.0
    )


def point_in_bbox(point: tuple[float, float], bbox: BBox) -> bool:
    """Проверить попадание точки в закрытый прямоугольник с допуском."""

    x, y = point
    xmin, ymin, xmax, ymax = bbox
    return (
        xmin - GEOMETRY_TOLERANCE_MM <= x <= xmax + GEOMETRY_TOLERANCE_MM
        and ymin - GEOMETRY_TOLERANCE_MM <= y <= ymax + GEOMETRY_TOLERANCE_MM
    )


def bboxes_overlap(first: BBox, second: BBox) -> bool:
    """Проверить пересечение внутренностей прямоугольников ненулевой площади."""

    ax1, ay1, ax2, ay2 = first
    bx1, by1, bx2, by2 = second
    return min(ax2, bx2) - max(ax1, bx1) > GEOMETRY_TOLERANCE_MM and min(
        ay2, by2
    ) - max(ay1, by1) > GEOMETRY_TOLERANCE_MM


def bboxes_distance(first: BBox, second: BBox) -> float:
    """Вернуть кратчайшее расстояние между двумя закрытыми прямоугольниками."""

    ax1, ay1, ax2, ay2 = first
    bx1, by1, bx2, by2 = second
    dx = max(bx1 - ax2, ax1 - bx2, 0.0)
    dy = max(by1 - ay2, ay1 - by2, 0.0)
    return math.hypot(dx, dy)


def polygon_in_bbox(points: Sequence[tuple[float, float]], bbox: BBox) -> bool:
    """Проверить, что прямоугольник полностью содержит геометрию полигона."""

    return bool(points) and all(point_in_bbox(point, bbox) for point in points)


def _clip_polygon_axis(
    points: Sequence[tuple[float, float]],
    *,
    axis: int,
    boundary: float,
    keep_greater: bool,
) -> list[tuple[float, float]]:
    """Отсечь полигон одной осевой полуплоскостью."""

    if not points:
        return []

    def inside(point: tuple[float, float]) -> bool:
        value = point[axis]
        if keep_greater:
            return value >= boundary - GEOMETRY_TOLERANCE_MM
        return value <= boundary + GEOMETRY_TOLERANCE_MM

    def intersection(
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> tuple[float, float]:
        delta = end[axis] - start[axis]
        if abs(delta) <= GEOMETRY_TOLERANCE_MM:
            result = [start[0], start[1]]
            result[axis] = boundary
            return result[0], result[1]
        ratio = (boundary - start[axis]) / delta
        return (
            start[0] + ratio * (end[0] - start[0]),
            start[1] + ratio * (end[1] - start[1]),
        )

    clipped: list[tuple[float, float]] = []
    previous = points[-1]
    previous_inside = inside(previous)
    for current in points:
        current_inside = inside(current)
        if current_inside:
            if not previous_inside:
                clipped.append(intersection(previous, current))
            clipped.append(current)
        elif previous_inside:
            clipped.append(intersection(previous, current))
        previous = current
        previous_inside = current_inside
    return clipped


def polygon_bbox_intersection_area(
    points: Sequence[tuple[float, float]],
    bbox: BBox,
) -> float:
    """Вычислить площадь пересечения КЭ с осевым прямоугольником."""

    xmin, ymin, xmax, ymax = bbox
    clipped: Sequence[tuple[float, float]] = points
    clipped = _clip_polygon_axis(clipped, axis=0, boundary=xmin, keep_greater=True)
    clipped = _clip_polygon_axis(clipped, axis=0, boundary=xmax, keep_greater=False)
    clipped = _clip_polygon_axis(clipped, axis=1, boundary=ymin, keep_greater=True)
    clipped = _clip_polygon_axis(clipped, axis=1, boundary=ymax, keep_greater=False)
    return polygon_area(clipped)


def polygon_bboxes_union_intersection_area(
    points: Sequence[tuple[float, float]],
    bboxes: Sequence[BBox],
) -> float:
    """Площадь пересечения полигона с объединением осевых прямоугольников.

    Простое суммирование площадей пересечений некорректно при разрешённых overlap:
    одна и та же половина КЭ могла бы быть посчитана дважды и ложно закрыть весь спрос.
    Sweep по уникальным X-границам разбивает объединение на непересекающиеся полосы.
    """

    if len(points) < 3 or not bboxes:
        return 0.0

    polygon_xmin = min(point[0] for point in points)
    polygon_ymin = min(point[1] for point in points)
    polygon_xmax = max(point[0] for point in points)
    polygon_ymax = max(point[1] for point in points)
    clipped_boxes: list[BBox] = []
    for xmin, ymin, xmax, ymax in bboxes:
        clipped = (
            max(xmin, polygon_xmin),
            max(ymin, polygon_ymin),
            min(xmax, polygon_xmax),
            min(ymax, polygon_ymax),
        )
        if (
            clipped[2] - clipped[0] > GEOMETRY_TOLERANCE_MM
            and clipped[3] - clipped[1] > GEOMETRY_TOLERANCE_MM
        ):
            clipped_boxes.append(clipped)
    if not clipped_boxes:
        return 0.0

    x_edges = sorted(
        {
            coordinate
            for xmin, _ymin, xmax, _ymax in clipped_boxes
            for coordinate in (xmin, xmax)
        }
    )
    total = 0.0
    for x_start, x_end in pairwise(x_edges):
        if x_end - x_start <= GEOMETRY_TOLERANCE_MM:
            continue
        x_middle = (x_start + x_end) / 2.0
        intervals = sorted(
            (ymin, ymax)
            for xmin, ymin, xmax, ymax in clipped_boxes
            if xmin < x_middle < xmax
        )
        if not intervals:
            continue

        merged: list[tuple[float, float]] = []
        for start, end in intervals:
            if not merged or start > merged[-1][1] + GEOMETRY_TOLERANCE_MM:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        total += sum(
            polygon_bbox_intersection_area(
                points,
                (x_start, y_start, x_end, y_end),
            )
            for y_start, y_end in merged
        )

    return min(total, polygon_area(points))
