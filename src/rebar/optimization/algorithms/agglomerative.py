"""Bottom-up baseline: связные пятна уровней и слияние ближайших зон."""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from time import perf_counter

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
    bboxes_distance,
    build_zone,
    cell_bbox,
    demanded_cells,
    evaluate_layout,
    prepare_detailing,
    zones_conflict,
)
from ..services.geometry import GEOMETRY_TOLERANCE_MM


@dataclass(frozen=True)
class _Cluster:
    """Одна изменяемая по составу, но иммутабельная версия кластера КЭ."""

    key: int
    cell_ids: tuple[int, ...]
    zone: LayoutZone


def _same_level_components(cells: tuple[DemandCell, ...]) -> list[tuple[int, ...]]:
    """Найти связные по касанию bbox компоненты каждого точного уровня."""

    parent = {cell.id: cell.id for cell in cells}

    def find(cell_id: int) -> int:
        while parent[cell_id] != cell_id:
            parent[cell_id] = parent[parent[cell_id]]
            cell_id = parent[cell_id]
        return cell_id

    def union(first: int, second: int) -> None:
        first_root = find(first)
        second_root = find(second)
        if first_root == second_root:
            return
        if first_root < second_root:
            parent[second_root] = first_root
        else:
            parent[first_root] = second_root

    by_level: dict[int, list[tuple[DemandCell, tuple[float, float, float, float]]]] = {}
    for cell in cells:
        by_level.setdefault(cell.level_index, []).append((cell, cell_bbox(cell)))

    for level_cells in by_level.values():
        ordered = sorted(level_cells, key=lambda item: (item[1][0], item[1][1], item[0].id))
        active: list[tuple[DemandCell, tuple[float, float, float, float]]] = []
        for cell, box in ordered:
            xmin = box[0]
            active = [
                item
                for item in active
                if item[1][2] + GEOMETRY_TOLERANCE_MM >= xmin
            ]
            for other, other_box in active:
                if bboxes_distance(box, other_box) <= GEOMETRY_TOLERANCE_MM:
                    union(cell.id, other.id)
            active.append((cell, box))

    components: dict[int, list[int]] = {}
    for cell in cells:
        components.setdefault(find(cell.id), []).append(cell.id)
    return sorted(
        (tuple(sorted(cell_ids)) for cell_ids in components.values()),
        key=lambda cell_ids: (cell_ids[0], len(cell_ids)),
    )


class AgglomerativeOptimizer:
    """Собирать раскладку снизу вверх слиянием ближайших кластеров.

    Начальные кластеры — связные пятна одного точного уровня спроса. Конфликтующие
    прямоугольники сливаются обязательно, затем ближайшие пары сливаются до ограничения
    ``max_details``. Это детерминированный пространственный baseline без гарантии
    глобального оптимума.
    """

    name = "agglomerative"

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
    def _pair_entry(
        problem: LayoutProblem,
        first: _Cluster,
        second: _Cluster,
    ) -> tuple[int, float, int, int]:
        conflict_rank = 0 if zones_conflict(problem, first.zone, second.zone) else 1
        return (
            conflict_rank,
            bboxes_distance(first.zone.bbox, second.zone.bbox),
            min(first.key, second.key),
            max(first.key, second.key),
        )

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
                meta={
                    "kind": "level_component_agglomerative_merge",
                    "baseline": True,
                    "initial_component_count": 0,
                    "merge_log": [],
                },
            )

        context = prepare_detailing(problem)
        initial_groups = _same_level_components(cells)
        clusters: dict[int, _Cluster] = {}
        for key, cell_ids in enumerate(initial_groups):
            clusters[key] = _Cluster(
                key=key,
                cell_ids=cell_ids,
                zone=self._zone(problem, cell_ids, f"initial-{key}", context),
            )

        pair_heap: list[tuple[int, float, int, int]] = []
        cluster_values = list(clusters.values())
        for index, first in enumerate(cluster_values):
            for second in cluster_values[index + 1 :]:
                heapq.heappush(pair_heap, self._pair_entry(problem, first, second))

        merge_log: list[dict[str, float | int | str]] = []
        next_key = len(clusters)
        while len(clusters) > 1:
            pair: tuple[int, float, int, int] | None = None
            while pair_heap:
                candidate = heapq.heappop(pair_heap)
                if candidate[2] in clusters and candidate[3] in clusters:
                    pair = candidate
                    break
            if pair is None:
                break

            conflict_rank, distance, first_key, second_key = pair
            if conflict_rank != 0 and len(clusters) <= detail_limit:
                break

            first = clusters.pop(first_key)
            second = clusters.pop(second_key)
            merged_ids = tuple(sorted((*first.cell_ids, *second.cell_ids)))
            merged = _Cluster(
                key=next_key,
                cell_ids=merged_ids,
                zone=self._zone(problem, merged_ids, f"merged-{next_key}", context),
            )
            next_key += 1
            merge_log.append(
                {
                    "first_size": len(first.cell_ids),
                    "second_size": len(second.cell_ids),
                    "result_size": len(merged_ids),
                    "distance_mm": distance,
                    "reason": "conflict" if conflict_rank == 0 else "detail_limit",
                }
            )
            for other in clusters.values():
                heapq.heappush(pair_heap, self._pair_entry(problem, merged, other))
            clusters[merged.key] = merged

        final_clusters = sorted(
            clusters.values(),
            key=lambda cluster: (
                cluster.zone.bbox[1],
                cluster.zone.bbox[0],
                cluster.cell_ids[0],
            ),
        )
        zones = tuple(
            self._zone(
                problem,
                cluster.cell_ids,
                f"agglomerative-{index}",
                context,
                collect_coverage=True,
            )
            for index, cluster in enumerate(final_clusters, 1)
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
            meta={
                "kind": "level_component_agglomerative_merge",
                "baseline": True,
                "component_rule": "same_level_touching_cell_bboxes",
                "initial_component_count": len(initial_groups),
                "merge_log": merge_log,
            },
        )
