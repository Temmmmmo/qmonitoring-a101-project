"""Жадный baseline по продольным сериям спроса в поперечных строках КЭ."""

from __future__ import annotations

import math
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
    cell_bbox,
    demanded_cells,
    evaluate_layout,
    partition_zones_conflict,
    prepare_detailing,
    resolve_zone_phases,
)
from ..services.geometry import GEOMETRY_TOLERANCE_MM


@dataclass(frozen=True)
class _Run:
    transverse_lower: float
    longitudinal_lower: float
    cell_ids: tuple[int, ...]


class RowRunGreedyOptimizer:
    """Строить локальные продольные серии и жадно объединять соседние группы."""

    name = "row-run-greedy"

    @staticmethod
    def _bounds(problem: LayoutProblem, cell: DemandCell) -> tuple[float, float, float, float]:
        xmin, ymin, xmax, ymax = cell_bbox(cell)
        if problem.demand.direction.axis is Axis.X:
            return ymin, ymax, xmin, xmax
        return xmin, xmax, ymin, ymax

    def _runs(
        self,
        problem: LayoutProblem,
        cells: tuple[DemandCell, ...],
    ) -> list[_Run]:
        bands: dict[tuple[float, float], list[tuple[float, float, int]]] = {}
        originals: dict[tuple[float, float], tuple[float, float]] = {}
        for cell in cells:
            transverse_lower, transverse_upper, start, end = self._bounds(problem, cell)
            key = (round(transverse_lower, 6), round(transverse_upper, 6))
            bands.setdefault(key, []).append((start, end, cell.id))
            originals.setdefault(key, (transverse_lower, transverse_upper))

        runs: list[_Run] = []
        for key, intervals in bands.items():
            transverse_lower = originals[key][0]
            ordered = sorted(intervals)
            pending: list[int] = []
            run_start = 0.0
            run_end = 0.0
            for start, end, cell_id in ordered:
                if not pending or start <= run_end + GEOMETRY_TOLERANCE_MM:
                    if not pending:
                        run_start = start
                    pending.append(cell_id)
                    run_end = max(run_end, end)
                    continue
                runs.append(_Run(transverse_lower, run_start, tuple(sorted(pending))))
                pending = [cell_id]
                run_start = start
                run_end = end
            if pending:
                runs.append(_Run(transverse_lower, run_start, tuple(sorted(pending))))
        return sorted(
            runs,
            key=lambda run: (run.transverse_lower, run.longitudinal_lower, run.cell_ids),
        )

    @staticmethod
    def _zone(
        problem: LayoutProblem,
        cell_ids: tuple[int, ...],
        zone_id: str,
        context: DetailingContext,
        *,
        collect_coverage: bool = False,
    ) -> LayoutZone:
        strongest = max(context.cells_by_id[cell_id].level_index for cell_id in cell_ids)
        return build_zone(
            problem,
            cell_ids,
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
        zones = [
            self._zone(
                problem,
                ids,
                f"row-run-greedy-{index}",
                context,
                collect_coverage=collect_coverage,
            )
            for index, ids in enumerate(groups, 1)
        ]
        return resolve_zone_phases(
            problem,
            zones,
            context=context,
            collect_coverage=collect_coverage,
        )

    @staticmethod
    def _merge_pair(groups: list[tuple[int, ...]], first: int, second: int) -> None:
        merged = tuple(sorted((*groups[first], *groups[second])))
        groups[first] = merged
        del groups[second]

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
                meta={"kind": "row_run_greedy", "baseline": True},
            )

        context = prepare_detailing(problem)
        groups = [run.cell_ids for run in self._runs(problem, cells)]
        initial_run_count = len(groups)
        maximum_initial_runs = int(request.params.get("maximum_initial_runs", 80))
        if maximum_initial_runs < 1:
            raise ValueError("maximum_initial_runs должен быть не меньше 1")
        if detail_limit == 1:
            groups = [tuple(sorted(cell.id for cell in cells))]
        elif len(groups) > maximum_initial_runs:
            chunk_size = math.ceil(len(groups) / maximum_initial_runs)
            groups = [
                tuple(
                    sorted(
                        cell_id
                        for group in groups[start : start + chunk_size]
                        for cell_id in group
                    )
                )
                for start in range(0, len(groups), chunk_size)
            ]
        coarsened_run_count = len(groups)
        merge_log: list[dict[str, float | int | str]] = []

        while len(groups) > 1:
            zones = self._zones(
                problem,
                groups,
                context,
                collect_coverage=False,
            )
            conflict_pair = next(
                (
                    (first, second)
                    for first, zone in enumerate(zones)
                    for second in range(first + 1, len(zones))
                    if partition_zones_conflict(problem, zone, zones[second])
                ),
                None,
            )
            if conflict_pair is not None:
                self._merge_pair(groups, *conflict_pair)
                merge_log.append(
                    {
                        "first": conflict_pair[0],
                        "second": conflict_pair[1],
                        "reason": "partition_overlap",
                    }
                )
                continue

            candidates: list[tuple[float, int]] = []
            for index in range(len(groups) - 1):
                merged_ids = tuple(sorted((*groups[index], *groups[index + 1])))
                merged = self._zone(
                    problem,
                    merged_ids,
                    "row-run-merge-candidate",
                    context,
                )
                delta = (
                    self._cost(merged, request)
                    - self._cost(zones[index], request)
                    - self._cost(zones[index + 1], request)
                )
                candidates.append((delta, index))
            delta, index = min(candidates, key=lambda item: (item[0], item[1]))
            must_reduce_count = len(groups) > detail_limit
            if not must_reduce_count and delta >= -min_improvement:
                break
            self._merge_pair(groups, index, index + 1)
            merge_log.append(
                {
                    "first": index,
                    "second": index + 1,
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
                "kind": "row_run_greedy",
                "baseline": True,
                "initial_run_count": initial_run_count,
                "coarsened_run_count": coarsened_run_count,
                "maximum_initial_runs": maximum_initial_runs,
                "merge_log": merge_log,
                "min_improvement_kg": min_improvement,
            },
        )
