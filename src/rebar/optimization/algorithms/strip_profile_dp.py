"""Динамическое программирование по последовательным поперечным полосам."""

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
    prepare_detailing,
    resolve_zone_phases,
)


@dataclass(frozen=True)
class _Band:
    lower: float
    upper: float
    cell_ids: tuple[int, ...]


@dataclass(frozen=True)
class _State:
    cost: float
    cuts: tuple[tuple[int, int], ...]


class StripProfileDpOptimizer:
    """Найти лучший Парето-вариант внутри класса последовательных полос."""

    name = "strip-profile-dp"

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
        originals: dict[tuple[float, float], tuple[float, float]] = {}
        for cell in cells:
            lower, upper = self._transverse_bounds(problem, cell)
            key = (round(lower, 6), round(upper, 6))
            grouped.setdefault(key, []).append(cell.id)
            originals.setdefault(key, (lower, upper))
        return sorted(
            (
                _Band(
                    lower=originals[key][0],
                    upper=originals[key][1],
                    cell_ids=tuple(sorted(cell_ids)),
                )
                for key, cell_ids in grouped.items()
            ),
            key=lambda band: (band.lower, band.upper, band.cell_ids),
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
                meta={"kind": "ordered_strip_dynamic_programming", "baseline": True},
            )

        context = prepare_detailing(problem)
        bands = self._bands(problem, cells)
        original_band_count = len(bands)
        maximum_bands = int(request.params.get("maximum_dp_bands", 48))
        if maximum_bands < 1:
            raise ValueError("maximum_dp_bands должен быть не меньше 1")
        if len(bands) > maximum_bands:
            chunk_size = math.ceil(len(bands) / maximum_bands)
            bands = [
                _Band(
                    lower=chunk[0].lower,
                    upper=chunk[-1].upper,
                    cell_ids=tuple(
                        sorted(cell_id for band in chunk for cell_id in band.cell_ids)
                    ),
                )
                for start in range(0, len(bands), chunk_size)
                if (chunk := bands[start : start + chunk_size])
            ]

        if detail_limit == 1:
            zone = self._zone(
                problem,
                tuple(sorted(cell.id for cell in cells)),
                "strip-profile-dp-1",
                context,
                collect_coverage=True,
            )
            zones = resolve_zone_phases(problem, [zone], context=context)
            evaluation = evaluate_layout(problem, zones, request)
            return LayoutSolution(
                algorithm=self.name,
                status=(
                    SolutionStatus.FEASIBLE if evaluation.valid else SolutionStatus.ERROR
                ),
                zones=tuple(zones),
                metrics=evaluation.metrics,
                request=request,
                runtime_ms=(perf_counter() - started) * 1000.0,
                diagnostics=evaluation.diagnostics,
                meta={
                    "kind": "ordered_strip_dynamic_programming",
                    "baseline": True,
                    "original_band_count": original_band_count,
                    "band_count": len(bands),
                    "selected_partition": ((0, len(bands)),),
                    "state_count": 1,
                    "optimal_within_strip_partition_class": evaluation.valid,
                },
            )

        candidate_zones: dict[tuple[int, int], LayoutZone] = {}
        for start in range(len(bands)):
            ids: list[int] = []
            for end in range(start + 1, len(bands) + 1):
                ids.extend(bands[end - 1].cell_ids)
                candidate_zones[(start, end)] = self._zone(
                    problem,
                    tuple(sorted(ids)),
                    f"strip-profile-candidate-{start}-{end}",
                    context,
                )

        states: dict[tuple[int, int], _State] = {(0, 0): _State(0.0, ())}
        for end in range(1, len(bands) + 1):
            for count in range(1, min(detail_limit, end) + 1):
                options: list[_State] = []
                for start in range(count - 1, end):
                    previous = states.get((start, count - 1))
                    if previous is None:
                        continue
                    zone = candidate_zones[(start, end)]
                    options.append(
                        _State(
                            cost=previous.cost + self._cost(zone, request),
                            cuts=(*previous.cuts, (start, end)),
                        )
                    )
                if options:
                    states[(end, count)] = min(
                        options,
                        key=lambda state: (state.cost, state.cuts),
                    )

        final_states = sorted(
            (
                state
                for (end, _count), state in states.items()
                if end == len(bands) and state.cuts
            ),
            key=lambda state: (state.cost, len(state.cuts), state.cuts),
        )
        selected_zones: list[LayoutZone] = []
        evaluation = None
        selected_state: _State | None = None
        for state in final_states:
            raw_zones = [
                self._zone(
                    problem,
                    tuple(
                        sorted(
                            cell_id
                            for band in bands[start:end]
                            for cell_id in band.cell_ids
                        )
                    ),
                    f"strip-profile-dp-{index}",
                    context,
                    collect_coverage=True,
                )
                for index, (start, end) in enumerate(state.cuts, 1)
            ]
            candidate = resolve_zone_phases(
                problem,
                raw_zones,
                context=context,
            )
            candidate_evaluation = evaluate_layout(problem, candidate, request)
            if candidate_evaluation.valid:
                selected_zones = candidate
                evaluation = candidate_evaluation
                selected_state = state
                break

        if evaluation is None:
            state = final_states[0]
            selected_state = state
            selected_zones = resolve_zone_phases(
                problem,
                [
                    self._zone(
                        problem,
                        tuple(
                            sorted(
                                cell_id
                                for band in bands[start:end]
                                for cell_id in band.cell_ids
                            )
                        ),
                        f"strip-profile-dp-{index}",
                        context,
                        collect_coverage=True,
                    )
                    for index, (start, end) in enumerate(state.cuts, 1)
                ],
                context=context,
            )
            evaluation = evaluate_layout(problem, selected_zones, request)

        return LayoutSolution(
            algorithm=self.name,
            status=SolutionStatus.FEASIBLE if evaluation.valid else SolutionStatus.ERROR,
            zones=tuple(selected_zones),
            metrics=evaluation.metrics,
            request=request,
            runtime_ms=(perf_counter() - started) * 1000.0,
            diagnostics=evaluation.diagnostics,
            meta={
                "kind": "ordered_strip_dynamic_programming",
                "baseline": True,
                "band_count": len(bands),
                "original_band_count": original_band_count,
                "maximum_dp_bands": maximum_bands,
                "selected_partition": selected_state.cuts,
                "state_count": len(states),
                "optimal_within_strip_partition_class": evaluation.valid,
            },
        )
