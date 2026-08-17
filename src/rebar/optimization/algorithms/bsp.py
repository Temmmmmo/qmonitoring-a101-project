"""Детерминированный BSP baseline с независимой проверкой результата."""

from __future__ import annotations

from time import perf_counter

from ..contracts import (
    AlgorithmRequest,
    LayoutProblem,
    LayoutSolution,
    SolutionStatus,
)
from ..services import (
    demanded_cells,
    evaluate_layout,
    prepare_detailing,
)
from ._guillotine import generate_splits, zone_for_ids


class BspOptimizer:
    """Рекурсивно делить спрос ортогональными линиями при снижении стоимости."""

    name = "bsp"

    def solve(
        self,
        problem: LayoutProblem,
        request: AlgorithmRequest | None = None,
    ) -> LayoutSolution:
        started = perf_counter()
        request = request or AlgorithmRequest()
        min_improvement = float(request.params.get("min_improvement_kg", 0.0))
        if min_improvement < 0:
            raise ValueError("min_improvement_kg не может быть отрицательным")
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
                meta={"kind": "recursive_binary_space_partition", "baseline": True},
            )

        context = prepare_detailing(problem)
        leaves = [tuple(cell.id for cell in cells)]
        zones = [zone_for_ids(problem, leaves[0], "bsp-1", context)]
        split_log: list[dict[str, float | str]] = []
        while len(leaves) < detail_limit:
            candidates = generate_splits(problem, request, leaves, zones, context)
            useful = [
                candidate for candidate in candidates if candidate.objective_gain > min_improvement
            ]
            if not useful:
                break
            best = max(
                useful,
                key=lambda candidate: (
                    candidate.objective_gain,
                    -candidate.axis,
                    -candidate.coordinate,
                    -candidate.leaf_index,
                ),
            )
            leaves[best.leaf_index : best.leaf_index + 1] = [best.left, best.right]
            zones[best.leaf_index : best.leaf_index + 1] = [
                zone_for_ids(problem, best.left, "pending-left", context),
                zone_for_ids(problem, best.right, "pending-right", context),
            ]
            split_log.append(
                {
                    "axis": "X" if best.axis == 0 else "Y",
                    "coordinate_mm": best.coordinate,
                    "objective_gain": best.objective_gain,
                }
            )

        zones = [
            zone_for_ids(problem, ids, f"bsp-{index}", context)
            for index, ids in enumerate(leaves, 1)
        ]
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
                "kind": "recursive_binary_space_partition",
                "baseline": True,
                "split_log": split_log,
                "min_improvement_kg": min_improvement,
            },
        )
