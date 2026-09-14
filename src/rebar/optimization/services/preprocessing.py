"""Явная предобработка карты требований перед оптимизацией."""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise

from ..contracts import DemandCell, LayoutProblem

_COORDINATE_DIGITS = 6
_SINGLE_CELL_POLICY = "isolated-same-level-component-to-previous-band-v1"


def _point_key(point: tuple[float, float]) -> tuple[float, float]:
    return round(point[0], _COORDINATE_DIGITS), round(point[1], _COORDINATE_DIGITS)


def _edge_key(
    first: tuple[float, float],
    second: tuple[float, float],
) -> tuple[tuple[float, float], tuple[float, float]]:
    endpoints = sorted((_point_key(first), _point_key(second)))
    return endpoints[0], endpoints[1]


def _edge_adjacency(cells: tuple[DemandCell, ...]) -> dict[int, set[int]]:
    """Построить соседство только по общему ребру, не по касанию углами."""

    owners_by_edge: dict[
        tuple[tuple[float, float], tuple[float, float]],
        list[int],
    ] = {}
    for cell in cells:
        if len(cell.poly) < 2:
            continue
        closed = (*cell.poly, cell.poly[0])
        for first, second in pairwise(closed):
            if _point_key(first) == _point_key(second):
                continue
            owners_by_edge.setdefault(_edge_key(first, second), []).append(cell.id)

    adjacency = {cell.id: set() for cell in cells}
    for owners in owners_by_edge.values():
        unique = tuple(dict.fromkeys(owners))
        for index, first in enumerate(unique):
            for second in unique[index + 1 :]:
                adjacency[first].add(second)
                adjacency[second].add(first)
    return adjacency


def apply_single_cell_rule(problem: LayoutProblem, *, policy: str = "legacy-research") -> LayoutProblem:
    """Сохранить спрос либо явно воспроизвести старое исследовательское понижение.

    Одиночным считается связный по общим рёбрам компонент ровно одного уровня,
    содержащий один КЭ. Проверка выполняется одновременно по исходной карте, поэтому
    понижения за один запуск не образуют каскад.
    """

    if policy not in {"preserve", "legacy-research"}:
        raise ValueError("неизвестная политика одиночных КЭ")
    if policy == "preserve":
        report = {"policy": "preserve-original-demand-v1", "changed_count": 0,
                  "changes": (), "reason": "Осреднение СТО не применено; исходная потребность сохранена."}
        return replace(problem, demand=replace(problem.demand, meta={
            **problem.demand.meta, "single_cell_preprocessing": report,
        }), meta={**problem.meta, "single_cell_preprocessing": report})
    cells = problem.demand.cells
    adjacency = _edge_adjacency(cells)
    by_id = {cell.id: cell for cell in cells}
    changed: dict[int, int] = {}
    visited: set[int] = set()

    for cell in cells:
        if cell.id in visited or cell.level_index <= 0:
            continue
        level = problem.demand.level(cell.level_index)
        if level.requires_extra is not True:
            continue

        component: list[int] = []
        pending = [cell.id]
        visited.add(cell.id)
        while pending:
            current_id = pending.pop()
            component.append(current_id)
            current = by_id[current_id]
            for neighbor_id in adjacency[current_id]:
                neighbor = by_id[neighbor_id]
                if (
                    neighbor_id not in visited
                    and neighbor.level_index == current.level_index
                ):
                    visited.add(neighbor_id)
                    pending.append(neighbor_id)

        if len(component) == 1:
            changed[component[0]] = cell.level_index - 1

    updated_cells = tuple(
        replace(cell, level_index=changed.get(cell.id, cell.level_index))
        for cell in cells
    )
    changes = tuple(
        {
            "cell_id": cell_id,
            "from_level_index": by_id[cell_id].level_index,
            "to_level_index": target_level,
        }
        for cell_id, target_level in sorted(changed.items())
    )
    demand_meta = dict(problem.demand.meta)
    demand_meta["single_cell_preprocessing"] = {
        "policy": _SINGLE_CELL_POLICY,
        "research_only": True,
        "changed_count": len(changes),
        "changes": changes,
    }
    problem_meta = dict(problem.meta)
    problem_meta["single_cell_preprocessing"] = demand_meta[
        "single_cell_preprocessing"
    ]
    return replace(
        problem,
        demand=replace(problem.demand, cells=updated_cells, meta=demand_meta),
        meta=problem_meta,
    )


def zone_count_bounds(problem: LayoutProblem) -> tuple[int, int]:
    """Вернуть естественный диапазон числа зон после предобработки.

    Для непустого спроса минимум равен одной зоне, максимум — общему числу входных
    КЭ. Это оставляет место разрешённому случаю, когда один требуемый КЭ закрывается
    частями нескольких зон. Пустой спрос имеет отдельный диапазон ``0..0``.
    """

    has_demand = any(
        problem.demand.level(cell.level_index).requires_extra is True
        for cell in problem.demand.cells
    )
    return (1, len(problem.demand.cells)) if has_demand else (0, 0)


def resolve_zone_count_limit(
    problem: LayoutProblem,
    requested: int | None,
) -> int | None:
    """Проверить пользовательский cap и подставить естественный максимум."""

    minimum, maximum = zone_count_bounds(problem)
    if maximum == 0:
        if requested is not None:
            raise ValueError(
                "для мозаики без дополнительного армирования число зон должно быть 0"
            )
        return None
    if requested is None:
        return maximum
    if requested < minimum or requested > maximum:
        raise ValueError(
            "максимальное число зон должно быть в диапазоне "
            f"{minimum}..{maximum} для {maximum} входных КЭ; "
            f"получено {requested}"
        )
    return requested
