"""Greedy baseline, приоритетно изолирующий дорогие уровни спроса."""

from __future__ import annotations

from collections.abc import Mapping
from time import perf_counter

from ..contracts import (
    AlgorithmRequest,
    DemandCell,
    LayoutProblem,
    LayoutSolution,
    SolutionStatus,
)
from ..services import demanded_cells, evaluate_layout, polygon_area, prepare_detailing
from ._guillotine import GuillotineSplit, generate_splits, zone_for_ids


class PriorityGreedyOptimizer:
    """Выбирать разрез по экономии массы и изоляции сильных цветовых уровней."""

    name = "greedy-priority"

    @staticmethod
    def _steel_density_kg_m2(problem: LayoutProblem, level_index: int) -> float:
        """Вернуть теоретическую массу дополнительной сетки на квадратный метр."""

        rebar = problem.demand.level(level_index).additional
        if rebar is None:
            return 0.0
        return 6.165 * rebar.diameter**2 / rebar.step

    def _overstrength_mass_kg(
        self,
        problem: LayoutProblem,
        cell_ids: tuple[int, ...],
        applied_level_index: int,
        cells_by_id: Mapping[int, DemandCell],
    ) -> float:
        """Оценить лишнюю массу от назначения всей группе одного сильного уровня."""

        applied_density = self._steel_density_kg_m2(problem, applied_level_index)
        result = 0.0
        for cell_id in cell_ids:
            cell = cells_by_id[cell_id]
            required_density = self._steel_density_kg_m2(problem, cell.level_index)
            excess_density = max(0.0, applied_density - required_density)
            result += polygon_area(cell.poly) / 1_000_000.0 * excess_density
        return result

    def _priority_gain_kg(
        self,
        problem: LayoutProblem,
        split: GuillotineSplit,
        cells_by_id: Mapping[int, DemandCell],
    ) -> float:
        parent_ids = (*split.left, *split.right)
        parent_level = max(cells_by_id[cell_id].level_index for cell_id in parent_ids)
        left_level = max(cells_by_id[cell_id].level_index for cell_id in split.left)
        right_level = max(cells_by_id[cell_id].level_index for cell_id in split.right)
        parent_excess = self._overstrength_mass_kg(problem, parent_ids, parent_level, cells_by_id)
        children_excess = self._overstrength_mass_kg(
            problem, split.left, left_level, cells_by_id
        ) + self._overstrength_mass_kg(problem, split.right, right_level, cells_by_id)
        return parent_excess - children_excess

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
        priority_weight = float(request.params.get("priority_weight", 1.0))
        if priority_weight < 0:
            raise ValueError("priority_weight не может быть отрицательным")

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
                meta={"kind": "priority_guillotine_greedy", "baseline": True},
            )

        context = prepare_detailing(problem)
        leaves = [tuple(cell.id for cell in cells)]
        zones = [zone_for_ids(problem, leaves[0], "greedy-priority-1", context)]
        split_log: list[dict[str, float | str]] = []

        while len(leaves) < detail_limit:
            candidates = generate_splits(problem, request, leaves, zones, context)
            scored: list[tuple[float, float, GuillotineSplit]] = []
            for candidate in candidates:
                priority_gain = self._priority_gain_kg(problem, candidate, context.cells_by_id)
                selection_gain = candidate.objective_gain + priority_weight * priority_gain
                if selection_gain > min_improvement:
                    scored.append((selection_gain, priority_gain, candidate))
            if not scored:
                break

            selection_gain, priority_gain, best = max(
                scored,
                key=lambda item: (
                    item[0],
                    item[1],
                    item[2].objective_gain,
                    -item[2].axis,
                    -item[2].coordinate,
                    -item[2].leaf_index,
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
                    "objective_gain_kg": best.objective_gain,
                    "priority_gain_kg": priority_gain,
                    "selection_gain_kg": selection_gain,
                }
            )

        zones = [
            zone_for_ids(problem, ids, f"greedy-priority-{index}", context)
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
                "kind": "priority_guillotine_greedy",
                "baseline": True,
                "priority_weight": priority_weight,
                "priority_rule": "avoided_overstrength_mass_kg",
                "split_log": split_log,
                "min_improvement_kg": min_improvement,
            },
        )
