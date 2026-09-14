"""Полное расширение малого пула сеточными прямоугольниками для диагностики H2."""

from __future__ import annotations

from dataclasses import replace

from ...contracts import LayoutProblem
from ...services import prepare_detailing
from ...services.cutting import CutLengthInfeasibleError
from ..genetic_pareto import (
    _atomic_leaves,
    _leaf_ids_for_rectangle,
    _PoolCandidate,
    _SearchSpace,
)
from ..spatial_partition_greedy import _make_rectangle


def expand_to_complete_grid_space(
    problem: LayoutProblem,
    space: _SearchSpace,
    *,
    max_grid_tiles: int = 6,
) -> _SearchSpace:
    """Добавить все отсутствующие grid-bbox × уровни, сохранив исходный пул.

    Только малый исследовательский эталон, не production-генератор. Новые bbox
    лежат на границах текущей сетки и не проходят через запрещённые плитки.
    Уже существующие baseline (в том числе несеточные) не удаляются/не округляются.
    Полнота относится к геометриям/уровням, а не ко всем физическим фазам.
    """

    grid = space.grid
    if max_grid_tiles < 1 or grid.row_count * grid.column_count > max_grid_tiles:
        raise ValueError("полный сеточный oracle ограничен max_grid_tiles")
    if space.stopped_by_time_limit:
        raise ValueError("CandidateSet остановлен по времени")
    leaves = space.leaves or _atomic_leaves(grid)
    context = prepare_detailing(problem)
    candidates = list(space.candidates)
    known = {
        (candidate.rectangle.zone.demand_bbox, candidate.rectangle.level_index)
        for candidate in candidates
    }
    levels = tuple(level.index for level in problem.demand.levels if level.requires_extra is True)
    for row_start in range(grid.row_count):
        for row_end in range(row_start + 1, grid.row_count + 1):
            for column_start in range(grid.column_count):
                for column_end in range(column_start + 1, grid.column_count + 1):
                    bounds = (row_start, row_end, column_start, column_end)
                    if not grid.is_fully_allowed(*bounds):
                        continue
                    for level in levels:
                        signature = (grid.bbox(*bounds), level)
                        if signature in known:
                            continue
                        eligible = tuple(
                            leaf for leaf in leaves
                            if row_start <= leaf.row_start and leaf.row_end <= row_end
                            and column_start <= leaf.column_start and leaf.column_end <= column_end
                            and leaf.level_index <= level
                        )
                        if not eligible:
                            continue
                        source_ids = tuple(sorted({
                            cell_id for leaf in eligible for cell_id in leaf.source_cell_ids
                        }))
                        try:
                            rectangle = _make_rectangle(
                                problem, grid, context, key=4_000_000 + len(candidates),
                                row_start=row_start, row_end=row_end,
                                column_start=column_start, column_end=column_end,
                                level_index=level, source_cell_ids=source_ids,
                            )
                        except CutLengthInfeasibleError:
                            continue
                        candidates.append(_PoolCandidate(
                            rectangle=rectangle,
                            leaf_ids=_leaf_ids_for_rectangle(rectangle, leaves),
                            source_cell_ids=source_ids,
                            origins=frozenset(("exhaustive-grid-oracle",)),
                        ))
                        known.add(signature)
    return replace(space, candidates=tuple(candidates))
