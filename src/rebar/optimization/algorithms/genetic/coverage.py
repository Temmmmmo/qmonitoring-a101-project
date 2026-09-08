"""Предметная дискретизация покрытия: ячейки сетки и их требуемые фрагменты."""

from __future__ import annotations

from dataclasses import dataclass

from ...contracts import BBox, LayoutProblem
from ...services import polygon_bbox_intersection_area
from ...services.geometry import GEOMETRY_TOLERANCE_MM
from ..spatial_partition_greedy import _Grid


@dataclass(frozen=True)
class CoverageAtom:
    row_start: int
    row_end: int
    column_start: int
    column_end: int
    level_index: int
    source_cell_ids: tuple[int, ...]
    bbox: BBox


def demand_fragments(
    problem: LayoutProblem,
    grid: _Grid,
    boundary_bboxes: tuple[BBox, ...],
    *,
    maximum_atoms: int = 50_000,
) -> tuple[CoverageAtom, ...]:
    """Разбить плитки по реальным границам несеточных кандидатов.

    Сеточные зоны уже лежат на границах плиток. После дополнительных разрезов каждый
    кандидат либо содержит весь фрагмент, либо не пересекает его внутренность.
    Требование определяется пересечением фрагмента с полигонами КЭ, не центроидом.
    Не меняет bbox кандидатов, ширины/анкеровку и не суммирует слабые уровни.
    """

    if maximum_atoms < 1:
        raise ValueError("maximum_atoms должен быть положительным")
    cells = {cell.id: cell for cell in problem.demand.cells}
    boundaries = tuple(sorted(set(boundary_bboxes)))
    atoms = []
    for row, levels in enumerate(grid.levels):
        for column, level in enumerate(levels):
            if level is None:
                continue
            tile = grid.bbox(row, row + 1, column, column + 1)
            x_edges = {tile[0], tile[2]}
            y_edges = {tile[1], tile[3]}
            for bbox in boundaries:
                if bbox[2] <= tile[0] or tile[2] <= bbox[0] or bbox[3] <= tile[1] or tile[3] <= bbox[1]:
                    continue
                x_edges.update(x for x in (bbox[0], bbox[2]) if tile[0] < x < tile[2])
                y_edges.update(y for y in (bbox[1], bbox[3]) if tile[1] < y < tile[3])
            xs, ys = sorted(x_edges), sorted(y_edges)
            for x0, x1 in zip(xs, xs[1:]):
                for y0, y1 in zip(ys, ys[1:]):
                    bbox = (x0, y0, x1, y1)
                    ids = tuple(
                        cell_id for cell_id in grid.source_cell_ids[row][column]
                        if polygon_bbox_intersection_area(cells[cell_id].poly, bbox) > GEOMETRY_TOLERANCE_MM
                    )
                    if not ids:
                        continue
                    atoms.append(CoverageAtom(
                        row, row + 1, column, column + 1,
                        max(cells[cell_id].level_index for cell_id in ids), ids, bbox,
                    ))
                    if len(atoms) > maximum_atoms:
                        raise ValueError("дискретизация demand_fragments превысила maximum_atoms")
    return tuple(atoms)
