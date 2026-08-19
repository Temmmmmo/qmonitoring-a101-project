"""Детерминированный Greedy baseline по поперечным полосам."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from rebar.models import Axis

from ..contracts import (
    AlgorithmRequest,
    DemandCell,
    LayoutProblem,
    LayoutSolution,
    LayoutZone,
    SolutionStatus,
)
from ..services import (
    DetailingContext,
    build_zone,
    demanded_cells,
    evaluate_layout,
    prepare_detailing,
    zones_conflict,
)
from ..services.geometry import GEOMETRY_TOLERANCE_MM, cell_bbox


@dataclass(frozen=True)
class _Band:
    """КЭ с одинаковым поперечным интервалом сетки."""

    lower: float
    upper: float
    cell_ids: tuple[int, ...]


class GreedyStripOptimizer:
    """Жадно объединять соседние поперечные полосы с минимальной потерей.

    Начальное разбиение покрывает весь спрос полосами минимальной ширины. Затем соседние
    полосы объединяются до ``max_details``; на каждом шаге выбирается слияние с наименьшим
    приростом общей целевой стоимости. Это быстрый baseline, а не гарантия глобального
    оптимума weighted rectangle cover.
    """

    name = "greedy"

    @staticmethod
    def _transverse_bounds(problem: LayoutProblem, cell: DemandCell) -> tuple[float, float]:
        xmin, ymin, xmax, ymax = cell_bbox(cell)
        return (ymin, ymax) if problem.demand.direction.axis is Axis.X else (xmin, xmax)

    def _bands(
        self,
        problem: LayoutProblem,
        cells: tuple[DemandCell, ...],
    ) -> list[_Band]:
        grouped: dict[tuple[float, float], list[int]] = {}
        original_bounds: dict[tuple[float, float], tuple[float, float]] = {}
        for cell in cells:
            lower, upper = self._transverse_bounds(problem, cell)
            key = (round(lower, 6), round(upper, 6))
            grouped.setdefault(key, []).append(cell.id)
            original_bounds.setdefault(key, (lower, upper))
        return sorted(
            (
                _Band(
                    lower=original_bounds[key][0],
                    upper=original_bounds[key][1],
                    cell_ids=tuple(sorted(cell_ids)),
                )
                for key, cell_ids in grouped.items()
            ),
            key=lambda band: (band.lower, band.upper, band.cell_ids),
        )

    @staticmethod
    def _initial_groups(
        bands: list[_Band],
        minimum_width_mm: float,
    ) -> list[tuple[int, ...]]:
        groups: list[tuple[int, ...]] = []
        pending_ids: list[int] = []
        pending_lower = 0.0
        pending_upper = 0.0

        for band in bands:
            if not pending_ids:
                pending_lower = band.lower
                pending_upper = band.upper
            else:
                pending_lower = min(pending_lower, band.lower)
                pending_upper = max(pending_upper, band.upper)
            pending_ids.extend(band.cell_ids)
            if pending_upper - pending_lower + GEOMETRY_TOLERANCE_MM >= minimum_width_mm:
                groups.append(tuple(sorted(pending_ids)))
                pending_ids = []

        if pending_ids:
            tail = tuple(sorted(pending_ids))
            if groups:
                groups[-1] = tuple(sorted((*groups[-1], *tail)))
            else:
                groups.append(tail)
        return groups

    @staticmethod
    def _zone(
        problem: LayoutProblem,
        ids: tuple[int, ...],
        zone_id: str,
        context: DetailingContext,
        *,
        collect_coverage: bool = True,
    ) -> LayoutZone:
        strongest = max(context.cells_by_id[cell_id].level_index for cell_id in ids)
        return build_zone(
            problem,
            ids,
            strongest,
            zone_id,
            collect_coverage=collect_coverage,
            context=context,
        )

    @staticmethod
    def _cost(zone: LayoutZone, request: AlgorithmRequest) -> float:
        return request.objective.mass * zone.mass_kg + request.objective.detail_penalty_kg

    def _zones(
        self,
        problem: LayoutProblem,
        groups: list[tuple[int, ...]],
        context: DetailingContext,
        *,
        collect_coverage: bool,
    ) -> list[LayoutZone]:
        return [
            self._zone(
                problem,
                ids,
                f"greedy-{index}",
                context,
                collect_coverage=collect_coverage,
            )
            for index, ids in enumerate(groups, 1)
        ]

    def _best_merge(
        self,
        problem: LayoutProblem,
        request: AlgorithmRequest,
        groups: list[tuple[int, ...]],
        zones: list[LayoutZone],
        context: DetailingContext,
    ) -> tuple[float, int, LayoutZone]:
        candidates: list[tuple[float, int, LayoutZone]] = []
        for index in range(len(groups) - 1):
            merged_ids = tuple(sorted((*groups[index], *groups[index + 1])))
            merged = self._zone(
                problem,
                merged_ids,
                "greedy-merge-candidate",
                context,
                collect_coverage=False,
            )
            delta = self._cost(merged, request) - self._cost(
                zones[index], request
            ) - self._cost(zones[index + 1], request)
            candidates.append((delta, index, merged))
        return min(candidates, key=lambda candidate: (candidate[0], candidate[1]))

    @staticmethod
    def _merge_groups(groups: list[tuple[int, ...]], index: int) -> None:
        groups[index : index + 2] = [
            tuple(sorted((*groups[index], *groups[index + 1])))
        ]

    def solve(
        self,
        problem: LayoutProblem,
        request: AlgorithmRequest | None = None,
    ) -> LayoutSolution:
        started = perf_counter()
        request = request or AlgorithmRequest()
        detail_limit = request.max_details or int(request.params.get("max_details", 8))
        if detail_limit < 1:
            raise ValueError("лимит деталей должен быть не меньше 1")
        min_improvement = float(request.params.get("min_improvement_kg", 0.0))
        if min_improvement < 0:
            raise ValueError("min_improvement_kg не может быть отрицательным")

        cells = demanded_cells(problem)
        if not cells:
            evaluation = evaluate_layout(problem, (), request)
            return LayoutSolution(
                algorithm=self.name,
                status=SolutionStatus.OPTIMAL,
                zones=(),
                metrics=evaluation.metrics,
                request=request,
                runtime_ms=(perf_counter() - started) * 1000.0,
                diagnostics=evaluation.diagnostics,
                meta={"kind": "greedy_transverse_strip_merge", "baseline": True},
            )

        context = prepare_detailing(problem)
        minimum_width = (
            problem.constraints.min_width_cells
            * context.typical_transverse_cell_size_mm
        )
        groups = self._initial_groups(self._bands(problem, cells), minimum_width)
        initial_strip_count = len(groups)
        merge_log: list[dict[str, float | int | str]] = []

        # Нерегулярная сетка может дать перекрывающиеся поперечные интервалы. Сливаем
        # такие соседние полосы до оптимизационных шагов, чтобы сохранить допустимость.
        while len(groups) > 1:
            zones = self._zones(
                problem, groups, context, collect_coverage=False
            )
            conflict_index = next(
                (
                    index
                    for index in range(len(zones) - 1)
                    if zones_conflict(problem, zones[index], zones[index + 1])
                ),
                None,
            )
            if conflict_index is None:
                break
            self._merge_groups(groups, conflict_index)
            merge_log.append({"index": conflict_index, "reason": "overlap"})

        while len(groups) > 1:
            zones = self._zones(
                problem, groups, context, collect_coverage=False
            )
            delta, index, _merged = self._best_merge(
                problem, request, groups, zones, context
            )
            must_reduce_count = len(groups) > detail_limit
            improves_objective = delta < -min_improvement
            if not must_reduce_count and not improves_objective:
                break
            self._merge_groups(groups, index)
            merge_log.append(
                {
                    "index": index,
                    "objective_delta": delta,
                    "reason": "detail_limit" if must_reduce_count else "objective",
                }
            )

        zones = self._zones(problem, groups, context, collect_coverage=True)
        evaluation = evaluate_layout(problem, zones, request)
        return LayoutSolution(
            algorithm=self.name,
            status=SolutionStatus.FEASIBLE if evaluation.valid else SolutionStatus.ERROR,
            zones=tuple(zones),
            metrics=evaluation.metrics,
            request=request,
            runtime_ms=(perf_counter() - started) * 1000.0,
            diagnostics=evaluation.diagnostics,
            meta={
                "kind": "greedy_transverse_strip_merge",
                "baseline": True,
                "initial_strip_count": initial_strip_count,
                "merge_log": merge_log,
                "min_improvement_kg": min_improvement,
            },
        )
