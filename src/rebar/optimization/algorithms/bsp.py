"""Детерминированный BSP baseline с независимой проверкой результата."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from ..contracts import (
    AlgorithmRequest,
    LayoutProblem,
    LayoutSolution,
    LayoutZone,
    SolutionStatus,
)
from ..services import (
    DetailingContext,
    bboxes_overlap,
    build_zone,
    demanded_cells,
    evaluate_layout,
    prepare_detailing,
)


@dataclass(frozen=True)
class _Split:
    gain: float
    leaf_index: int
    axis: int
    coordinate: float
    left: tuple[int, ...]
    right: tuple[int, ...]


class BspOptimizer:
    """Рекурсивно делить спрос ортогональными линиями при снижении стоимости."""

    name = "bsp"

    @staticmethod
    def _zone(
        problem: LayoutProblem,
        ids: tuple[int, ...],
        zone_id: str,
        *,
        collect_coverage: bool = True,
        context: DetailingContext | None = None,
    ) -> LayoutZone:
        by_id = context.cells_by_id if context else {
            cell.id: cell for cell in problem.demand.cells
        }
        strongest = max(by_id[cell_id].level_index for cell_id in ids)
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

    def _splits(
        self,
        problem: LayoutProblem,
        request: AlgorithmRequest,
        leaves: list[tuple[int, ...]],
        zones: list[LayoutZone],
        context: DetailingContext,
    ) -> list[_Split]:
        by_id = context.cells_by_id
        candidates: list[_Split] = []
        for leaf_index, ids in enumerate(leaves):
            if len(ids) < 2:
                continue
            parent_cost = self._cost(zones[leaf_index], request)
            for axis in (0, 1):
                coordinates = sorted({by_id[cell_id].centroid[axis] for cell_id in ids})
                for lower, upper in zip(coordinates, coordinates[1:]):
                    cut = (lower + upper) / 2.0
                    left = tuple(cell_id for cell_id in ids if by_id[cell_id].centroid[axis] <= cut)
                    right = tuple(cell_id for cell_id in ids if by_id[cell_id].centroid[axis] > cut)
                    if not left or not right:
                        continue
                    left_zone = self._zone(
                        problem,
                        left,
                        "candidate-left",
                        collect_coverage=False,
                        context=context,
                    )
                    right_zone = self._zone(
                        problem,
                        right,
                        "candidate-right",
                        collect_coverage=False,
                        context=context,
                    )
                    if not problem.constraints.allow_overlaps:
                        other_zones = [*zones[:leaf_index], *zones[leaf_index + 1 :]]
                        if bboxes_overlap(left_zone.bbox, right_zone.bbox) or any(
                            bboxes_overlap(candidate.bbox, other.bbox)
                            for candidate in (left_zone, right_zone)
                            for other in other_zones
                        ):
                            continue
                    children_cost = self._cost(left_zone, request) + self._cost(
                        right_zone, request
                    )
                    candidates.append(
                        _Split(
                            gain=parent_cost - children_cost,
                            leaf_index=leaf_index,
                            axis=axis,
                            coordinate=cut,
                            left=left,
                            right=right,
                        )
                    )
        return candidates

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
        zones = [self._zone(problem, leaves[0], "bsp-1", context=context)]
        split_log: list[dict[str, float | str]] = []
        while len(leaves) < detail_limit:
            candidates = self._splits(problem, request, leaves, zones, context)
            useful = [candidate for candidate in candidates if candidate.gain > min_improvement]
            if not useful:
                break
            best = max(
                useful,
                key=lambda candidate: (
                    candidate.gain,
                    -candidate.axis,
                    -candidate.coordinate,
                    -candidate.leaf_index,
                ),
            )
            leaves[best.leaf_index : best.leaf_index + 1] = [best.left, best.right]
            zones[best.leaf_index : best.leaf_index + 1] = [
                self._zone(problem, best.left, "pending-left", context=context),
                self._zone(problem, best.right, "pending-right", context=context),
            ]
            split_log.append(
                {
                    "axis": "X" if best.axis == 0 else "Y",
                    "coordinate_mm": best.coordinate,
                    "objective_gain": best.gain,
                }
            )

        zones = [
            self._zone(problem, ids, f"bsp-{index}", context=context)
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
