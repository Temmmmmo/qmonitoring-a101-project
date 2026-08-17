"""Низкоуровневые геометрические операции без инженерной семантики."""

from __future__ import annotations

from collections.abc import Sequence

from ..contracts import BBox, DemandCell

GEOMETRY_TOLERANCE_MM = 1e-6


def cell_bbox(cell: DemandCell) -> BBox:
    """Вернуть осевой bbox конечного элемента."""

    xs = [point[0] for point in cell.poly]
    ys = [point[1] for point in cell.poly]
    return min(xs), min(ys), max(xs), max(ys)


def polygon_area(points: Sequence[tuple[float, float]]) -> float:
    """Вычислить абсолютную площадь простого полигона формулой шнуровки."""

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
