"""Грубый baseline: одна деталь максимального уровня на весь спрос."""

from __future__ import annotations

from time import perf_counter

from ..contracts import AlgorithmRequest, LayoutProblem, LayoutSolution, SolutionStatus
from ..services import build_zone, demanded_cells, evaluate_layout


class StrongestBBoxOptimizer:
    """Построить одну охватывающую деталь с максимальным требуемым уровнем."""

    name = "bbox"

    def solve(
        self,
        problem: LayoutProblem,
        request: AlgorithmRequest | None = None,
    ) -> LayoutSolution:
        started = perf_counter()
        request = request or AlgorithmRequest()
        cells = demanded_cells(problem)
        zones = ()
        if cells:
            strongest_level = max(cell.level_index for cell in cells)
            zones = (
                build_zone(
                    problem,
                    (cell.id for cell in cells),
                    strongest_level,
                    "bbox-1",
                ),
            )
        evaluation = evaluate_layout(problem, zones, request)
        return LayoutSolution(
            algorithm=self.name,
            status=SolutionStatus.FEASIBLE if evaluation.valid else SolutionStatus.ERROR,
            zones=zones,
            metrics=evaluation.metrics,
            request=request,
            runtime_ms=(perf_counter() - started) * 1000.0,
            diagnostics=evaluation.diagnostics,
            meta={"kind": "single_strongest_bounding_box", "baseline": True},
        )
