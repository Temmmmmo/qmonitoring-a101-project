"""Адресные расширения геометрии конечного CandidateSet без смены правил покрытия."""

from __future__ import annotations

from ...contracts import LayoutProblem
from ...services import DetailingContext
from ...services.cutting import CutLengthInfeasibleError
from ..spatial_partition_greedy import _Grid, _make_rectangle, _Rectangle


def layered_geometry_variants(
    problem: LayoutProblem,
    grid: _Grid,
    context: DetailingContext,
    rectangles: tuple[_Rectangle, ...],
    *,
    maximum_variants: int,
) -> tuple[_Rectangle, ...]:
    """Предложить слабые оболочки под сильными локальными зонами.

    Слабая оболочка обслуживает только самостоятельно достаточные для неё КЭ.
    Требования сильных КЭ не складываются с оболочкой и остаются для repair.
    Все новые геометрии сеточные; исходные baseline не округляются/не изменяются.
    """

    known = {
        (rectangle.zone.demand_bbox, rectangle.level_index) for rectangle in rectangles
    }
    hulls = {
        (rectangle.row_start, rectangle.row_end, rectangle.column_start, rectangle.column_end)
        for rectangle in rectangles
    }
    demanded_tiles = [
        (row, column, level)
        for row, levels in enumerate(grid.levels)
        for column, level in enumerate(levels)
        if level is not None
    ]
    if demanded_tiles:
        hulls.add((
            min(row for row, _column, _level in demanded_tiles),
            max(row for row, _column, _level in demanded_tiles) + 1,
            min(column for _row, column, _level in demanded_tiles),
            max(column for _row, column, _level in demanded_tiles) + 1,
        ))
    levels = tuple(level.index for level in problem.demand.levels if level.requires_extra is True)
    ranked = []
    for hull in sorted(hulls):
        if not grid.is_fully_allowed(*hull):
            continue
        inside = [
            (row, column, demand_level) for row, column, demand_level in demanded_tiles
            if hull[0] <= row < hull[1] and hull[2] <= column < hull[3]
        ]
        for level in levels:
            signature = (grid.bbox(*hull), level)
            if signature in known:
                continue
            eligible = [(row, column) for row, column, demand_level in inside if demand_level <= level]
            if not eligible:
                continue
            # Нет смысла повышать уровень уже достаточной геометрии в этой гипотезе.
            if not any(demand_level >= level for _row, _column, demand_level in inside):
                continue
            source_ids = tuple(sorted({
                cell_id for row, column in eligible for cell_id in grid.source_cell_ids[row][column]
            }))
            try:
                rectangle = _make_rectangle(
                    problem, grid, context, key=5_000_000 + len(ranked),
                    row_start=hull[0], row_end=hull[1],
                    column_start=hull[2], column_end=hull[3],
                    level_index=level, source_cell_ids=source_ids,
                )
            except CutLengthInfeasibleError:
                # Например, новая длина не помещается в согласованный каталог раскроя.
                continue
            ranked.append((rectangle.zone.mass_kg / len(eligible), signature, rectangle))
            known.add(signature)
    ranked.sort(key=lambda item: (item[0], item[1]))
    return tuple(item[2] for item in ranked[:maximum_variants])
