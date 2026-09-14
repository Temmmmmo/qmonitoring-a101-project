"""Жадное укрупнение непересекающегося пространственного разбиения."""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass, replace
from time import perf_counter

from rebar.models import Axis

from ..contracts import (
    AlgorithmRequest,
    BBox,
    DemandCell,
    LayoutProblem,
    LayoutSolution,
    LayoutZone,
    SolutionStatus,
)
from ..services import (
    DetailingContext,
    build_zone_from_bbox,
    demanded_cells,
    evaluate_layout,
    polygon_bbox_intersection_area,
    prepare_detailing,
)
from ..services.geometry import GEOMETRY_TOLERANCE_MM, cell_bbox
from ..services.cutting import CutLengthInfeasibleError


@dataclass(frozen=True)
class _Grid:
    """Регулярная диагностическая сетка поверх геометрии КЭ."""

    x_edges: tuple[float, ...]
    y_edges: tuple[float, ...]
    allowed: tuple[tuple[bool, ...], ...]
    levels: tuple[tuple[int | None, ...], ...]
    source_cell_ids: tuple[tuple[tuple[int, ...], ...], ...]
    allowed_prefix: tuple[tuple[int, ...], ...]

    @property
    def column_count(self) -> int:
        return len(self.x_edges) - 1

    @property
    def row_count(self) -> int:
        return len(self.y_edges) - 1

    def bbox(self, row_start: int, row_end: int, column_start: int, column_end: int) -> BBox:
        return (
            self.x_edges[column_start],
            self.y_edges[row_start],
            self.x_edges[column_end],
            self.y_edges[row_end],
        )

    def is_fully_allowed(
        self,
        row_start: int,
        row_end: int,
        column_start: int,
        column_end: int,
    ) -> bool:
        allowed_count = (
            self.allowed_prefix[row_end][column_end]
            - self.allowed_prefix[row_start][column_end]
            - self.allowed_prefix[row_end][column_start]
            + self.allowed_prefix[row_start][column_start]
        )
        return allowed_count == (row_end - row_start) * (column_end - column_start)


@dataclass(frozen=True)
class _Rectangle:
    """Один активный прямоугольник в индексах сетки и его детализация."""

    key: int
    row_start: int
    row_end: int
    column_start: int
    column_end: int
    level_index: int
    source_cell_ids: tuple[int, ...]
    zone: LayoutZone


@dataclass(frozen=True)
class _MergeCandidate:
    """Допустимое укрупнение одной или нескольких текущих зон."""

    absorbed_keys: tuple[int, ...]
    rectangle: _Rectangle
    objective_delta: float

    @property
    def detail_reduction(self) -> int:
        return len(self.absorbed_keys) - 1


def _grid_edges(lower: float, upper: float, target_size: float) -> tuple[float, ...]:
    span = upper - lower
    count = max(1, math.ceil(span / target_size))
    step = span / count
    return tuple(lower + step * index for index in range(count)) + (upper,)


def _median_cell_spans(problem: LayoutProblem) -> tuple[float, float]:
    x_spans: list[float] = []
    y_spans: list[float] = []
    for cell in problem.demand.cells:
        xmin, ymin, xmax, ymax = cell_bbox(cell)
        if xmax - xmin > GEOMETRY_TOLERANCE_MM:
            x_spans.append(xmax - xmin)
        if ymax - ymin > GEOMETRY_TOLERANCE_MM:
            y_spans.append(ymax - ymin)
    if not x_spans or not y_spans:
        raise ValueError("невозможно определить характерный размер сетки КЭ")
    return statistics.median(x_spans), statistics.median(y_spans)


def _cell_tile_range(
    edges: tuple[float, ...],
    lower: float,
    upper: float,
) -> range:
    start = max(0, bisect.bisect_right(edges, lower) - 1)
    end = min(len(edges) - 1, bisect.bisect_left(edges, upper))
    return range(start, max(start, end))


def _prefix_sum(values: list[list[bool]]) -> tuple[tuple[int, ...], ...]:
    row_count = len(values)
    column_count = len(values[0]) if values else 0
    prefix = [[0] * (column_count + 1) for _ in range(row_count + 1)]
    for row in range(row_count):
        running = 0
        for column in range(column_count):
            running += int(values[row][column])
            prefix[row + 1][column + 1] = prefix[row][column + 1] + running
    return tuple(tuple(row) for row in prefix)


def _build_grid(
    problem: LayoutProblem,
    request: AlgorithmRequest,
) -> _Grid:
    xmin, ymin, xmax, ymax = problem.demand.bbox
    median_x, median_y = _median_cell_spans(problem)
    requested_size = request.params.get("grid_cell_size_mm")
    if requested_size is not None:
        requested_size = float(requested_size)
        if requested_size <= 0:
            raise ValueError("grid_cell_size_mm должен быть положительным")
        median_x = median_y = requested_size

    maximum_tiles = int(request.params.get("maximum_grid_tiles", 12_000))
    if maximum_tiles < 1:
        raise ValueError("maximum_grid_tiles должен быть не меньше 1")
    expected_tiles = math.ceil((xmax - xmin) / median_x) * math.ceil(
        (ymax - ymin) / median_y
    )
    if expected_tiles > maximum_tiles:
        scale = math.sqrt(expected_tiles / maximum_tiles)
        median_x *= scale
        median_y *= scale

    x_edges = _grid_edges(xmin, xmax, median_x)
    y_edges = _grid_edges(ymin, ymax, median_y)
    row_count = len(y_edges) - 1
    column_count = len(x_edges) - 1
    candidate_ids: list[list[list[int]]] = [
        [[] for _ in range(column_count)] for _ in range(row_count)
    ]
    cells_by_id = {cell.id: cell for cell in problem.demand.cells}
    for cell in problem.demand.cells:
        cell_xmin, cell_ymin, cell_xmax, cell_ymax = cell_bbox(cell)
        for row in _cell_tile_range(y_edges, cell_ymin, cell_ymax):
            for column in _cell_tile_range(x_edges, cell_xmin, cell_xmax):
                candidate_ids[row][column].append(cell.id)

    allowed: list[list[bool]] = []
    levels: list[list[int | None]] = []
    sources: list[list[tuple[int, ...]]] = []
    for row in range(row_count):
        allowed_row: list[bool] = []
        level_row: list[int | None] = []
        source_row: list[tuple[int, ...]] = []
        for column in range(column_count):
            tile_bbox = (
                x_edges[column],
                y_edges[row],
                x_edges[column + 1],
                y_edges[row + 1],
            )
            intersecting: list[DemandCell] = []
            demanded: list[DemandCell] = []
            for cell_id in candidate_ids[row][column]:
                cell = cells_by_id[cell_id]
                if (
                    polygon_bbox_intersection_area(cell.poly, tile_bbox)
                    <= GEOMETRY_TOLERANCE_MM
                ):
                    continue
                intersecting.append(cell)
                if problem.demand.level(cell.level_index).requires_extra is True:
                    demanded.append(cell)
            allowed_row.append(bool(intersecting))
            level_row.append(
                max((cell.level_index for cell in demanded), default=None)
            )
            source_row.append(tuple(sorted(cell.id for cell in demanded)))
        allowed.append(allowed_row)
        levels.append(level_row)
        sources.append(source_row)

    return _Grid(
        x_edges=x_edges,
        y_edges=y_edges,
        allowed=tuple(tuple(row) for row in allowed),
        levels=tuple(tuple(row) for row in levels),
        source_cell_ids=tuple(tuple(row) for row in sources),
        allowed_prefix=_prefix_sum(allowed),
    )


def _with_spatial_meta(zone: LayoutZone) -> LayoutZone:
    return replace(
        zone,
        meta={**zone.meta, "construction": "spatial_partition_bbox"},
    )


def _make_rectangle(
    problem: LayoutProblem,
    grid: _Grid,
    context: DetailingContext,
    *,
    key: int,
    row_start: int,
    row_end: int,
    column_start: int,
    column_end: int,
    level_index: int,
    source_cell_ids: tuple[int, ...],
    collect_coverage: bool = False,
) -> _Rectangle:
    zone = build_zone_from_bbox(
        problem,
        grid.bbox(row_start, row_end, column_start, column_end),
        level_index,
        f"spatial-partition-{key}",
        seed_cell_ids=source_cell_ids,
        collect_coverage=collect_coverage,
        context=context,
    )
    return _Rectangle(
        key=key,
        row_start=row_start,
        row_end=row_end,
        column_start=column_start,
        column_end=column_end,
        level_index=level_index,
        source_cell_ids=source_cell_ids,
        zone=_with_spatial_meta(zone),
    )


def _stock_bounded_rectangles(
    problem: LayoutProblem,
    grid: _Grid,
    context: DetailingContext,
    *,
    key: int,
    row_start: int,
    row_end: int,
    column_start: int,
    column_end: int,
    level_index: int,
    source_cell_ids: tuple[int, ...],
) -> tuple[_Rectangle, ...]:
    """Разделить только слишком длинное обязательное покрытие вдоль стержней.

    Границы частей совпадают с сеткой, а каждая часть получает полную анкеровку
    и независимую длину из каталога. Это не разрез уже готового стержня без
    нахлёста. Поперечная ширина и исходная карта КЭ не меняются.
    """

    def materialize(start: int, end: int, rectangle_key: int) -> _Rectangle:
        rows = (row_start, row_end) if axis is Axis.X else (start, end)
        columns = (start, end) if axis is Axis.X else (column_start, column_end)
        ids = source_cell_ids if (start, end) == (lower, upper) else tuple(sorted({
            cell_id
            for row in range(*rows)
            for column in range(*columns)
            for cell_id in grid.source_cell_ids[row][column]
        }))
        return _make_rectangle(
            problem, grid, context, key=rectangle_key,
            row_start=rows[0], row_end=rows[1],
            column_start=columns[0], column_end=columns[1],
            level_index=level_index, source_cell_ids=ids,
        )

    axis = problem.demand.direction.axis
    lower, upper = ((column_start, column_end) if axis is Axis.X else (row_start, row_end))
    try:
        return (materialize(lower, upper, key),)
    except CutLengthInfeasibleError:
        pass

    result: list[_Rectangle] = []
    start = lower
    while start < upper:
        # Один атом нельзя молча удалить или сделать короче ради каталога.
        try:
            best = materialize(start, start + 1, key + len(result))
        except CutLengthInfeasibleError as error:
            raise CutLengthInfeasibleError(
                "атом обязательной сетки не помещается в каталог вместе с полной "
                f"анкеровкой; ось {axis.value}, индекс {start}: {error}"
            ) from error
        # Допустимость длины монотонна по продольному габариту при том же уровне.
        # Выбираем наибольший допустимый префикс, а не избыточные мелкие куски.
        low, high = start + 2, upper
        while low <= high:
            end = (low + high) // 2
            try:
                proposed = materialize(start, end, key + len(result))
            except CutLengthInfeasibleError:
                high = end - 1
            else:
                best = proposed
                low = end + 1
        result.append(best)
        start = best.column_end if axis is Axis.X else best.row_end
    return tuple(result)


def _initial_rectangles(
    problem: LayoutProblem,
    grid: _Grid,
    context: DetailingContext,
    *,
    align_with_bar_axis: bool = False,
) -> list[_Rectangle]:
    merged: list[list[object]] = []
    active_by_profile: dict[tuple[int, int, int], int] = {}
    if not align_with_bar_axis or problem.demand.direction.axis is Axis.X:
        runs: list[tuple[int, int, int, int, int, tuple[int, ...]]] = []
        for row, levels in enumerate(grid.levels):
            column = 0
            while column < grid.column_count:
                level = levels[column]
                if level is None:
                    column += 1
                    continue
                end = column + 1
                source_ids = set(grid.source_cell_ids[row][column])
                while end < grid.column_count and levels[end] == level:
                    source_ids.update(grid.source_cell_ids[row][end])
                    end += 1
                runs.append(
                    (row, row + 1, column, end, level, tuple(sorted(source_ids)))
                )
                column = end

        for row_start, row_end, column_start, column_end, level, source_ids in runs:
            profile = (column_start, column_end, level)
            previous_index = active_by_profile.get(profile)
            if previous_index is not None and merged[previous_index][1] == row_start:
                merged[previous_index][1] = row_end
                merged[previous_index][5] = tuple(
                    sorted({*merged[previous_index][5], *source_ids})
                )
            else:
                active_by_profile[profile] = len(merged)
                merged.append(
                    [row_start, row_end, column_start, column_end, level, source_ids]
                )
    else:
        runs = []
        for column in range(grid.column_count):
            row = 0
            while row < grid.row_count:
                level = grid.levels[row][column]
                if level is None:
                    row += 1
                    continue
                end = row + 1
                source_ids = set(grid.source_cell_ids[row][column])
                while end < grid.row_count and grid.levels[end][column] == level:
                    source_ids.update(grid.source_cell_ids[end][column])
                    end += 1
                runs.append(
                    (row, end, column, column + 1, level, tuple(sorted(source_ids)))
                )
                row = end

        for row_start, row_end, column_start, column_end, level, source_ids in runs:
            profile = (row_start, row_end, level)
            previous_index = active_by_profile.get(profile)
            if previous_index is not None and merged[previous_index][3] == column_start:
                merged[previous_index][3] = column_end
                merged[previous_index][5] = tuple(
                    sorted({*merged[previous_index][5], *source_ids})
                )
            else:
                active_by_profile[profile] = len(merged)
                merged.append(
                    [row_start, row_end, column_start, column_end, level, source_ids]
                )

    rectangles: list[_Rectangle] = []
    for values in merged:
        rectangles.extend(_stock_bounded_rectangles(
            problem,
            grid,
            context,
            key=len(rectangles),
            row_start=int(values[0]),
            row_end=int(values[1]),
            column_start=int(values[2]),
            column_end=int(values[3]),
            level_index=int(values[4]),
            source_cell_ids=values[5],
        ))
    return rectangles


def _ranges_overlap(first_start: int, first_end: int, second_start: int, second_end: int) -> bool:
    return min(first_end, second_end) > max(first_start, second_start)


def _contains(outer: tuple[int, int, int, int], rectangle: _Rectangle) -> bool:
    row_start, row_end, column_start, column_end = outer
    return (
        row_start <= rectangle.row_start
        and rectangle.row_end <= row_end
        and column_start <= rectangle.column_start
        and rectangle.column_end <= column_end
    )


def _intersects(outer: tuple[int, int, int, int], rectangle: _Rectangle) -> bool:
    row_start, row_end, column_start, column_end = outer
    return _ranges_overlap(row_start, row_end, rectangle.row_start, rectangle.row_end) and _ranges_overlap(
        column_start,
        column_end,
        rectangle.column_start,
        rectangle.column_end,
    )


def _neighbor_pairs(rectangles: list[_Rectangle]) -> set[tuple[int, int]]:
    """Найти ближайших соседей по четырём ортогональным направлениям."""

    pairs: set[tuple[int, int]] = set()
    for first_index, first in enumerate(rectangles):
        nearest_right: tuple[int, int] | None = None
        nearest_up: tuple[int, int] | None = None
        for second_index, second in enumerate(rectangles):
            if first_index == second_index:
                continue
            if second.column_start >= first.column_end and _ranges_overlap(
                first.row_start,
                first.row_end,
                second.row_start,
                second.row_end,
            ):
                candidate = (second.column_start - first.column_end, second_index)
                if nearest_right is None or candidate < nearest_right:
                    nearest_right = candidate
            if second.row_start >= first.row_end and _ranges_overlap(
                first.column_start,
                first.column_end,
                second.column_start,
                second.column_end,
            ):
                candidate = (second.row_start - first.row_end, second_index)
                if nearest_up is None or candidate < nearest_up:
                    nearest_up = candidate
        for nearest in (nearest_right, nearest_up):
            if nearest is not None:
                pairs.add(tuple(sorted((first_index, nearest[1]))))
    return pairs


def _objective(zone: LayoutZone, request: AlgorithmRequest) -> float:
    return request.objective.mass * zone.mass_kg + request.objective.detail_penalty_kg


def _nondominated_trace(
    trajectory: list[dict[str, float | int]],
) -> tuple[dict[str, float | int], ...]:
    """Оставить недоминируемые точки одной жадной траектории, без claims оптимума."""

    best_mass = math.inf
    front: list[dict[str, float | int]] = []
    for point in sorted(trajectory, key=lambda item: int(item["zone_count"])):
        mass = float(point["total_mass_kg"])
        if mass + 1e-9 >= best_mass:
            continue
        front.append(point)
        best_mass = mass
    return tuple(front)


def _candidate(
    problem: LayoutProblem,
    request: AlgorithmRequest,
    grid: _Grid,
    context: DetailingContext,
    rectangles: list[_Rectangle],
    first_index: int,
    second_index: int,
    next_key: int,
) -> _MergeCandidate | None:
    first = rectangles[first_index]
    second = rectangles[second_index]
    hull = (
        min(first.row_start, second.row_start),
        max(first.row_end, second.row_end),
        min(first.column_start, second.column_start),
        max(first.column_end, second.column_end),
    )
    while True:
        crossing = [
            rectangle
            for rectangle in rectangles
            if _intersects(hull, rectangle) and not _contains(hull, rectangle)
        ]
        if not crossing:
            break
        expanded = (
            min(hull[0], *(rectangle.row_start for rectangle in crossing)),
            max(hull[1], *(rectangle.row_end for rectangle in crossing)),
            min(hull[2], *(rectangle.column_start for rectangle in crossing)),
            max(hull[3], *(rectangle.column_end for rectangle in crossing)),
        )
        if expanded == hull:
            return None
        hull = expanded
    if not grid.is_fully_allowed(*hull):
        return None

    absorbed = [rectangle for rectangle in rectangles if _intersects(hull, rectangle)]
    if len(absorbed) < 2 or any(not _contains(hull, rectangle) for rectangle in absorbed):
        return None
    level_index = max(rectangle.level_index for rectangle in absorbed)
    source_ids = tuple(
        sorted(
            {
                cell_id
                for rectangle in absorbed
                for cell_id in rectangle.source_cell_ids
            }
        )
    )
    try:
        merged = _make_rectangle(
            problem,
            grid,
            context,
            key=next_key,
            row_start=hull[0],
            row_end=hull[1],
            column_start=hull[2],
            column_end=hull[3],
            level_index=level_index,
            source_cell_ids=source_ids,
        )
    except CutLengthInfeasibleError:
        return None
    absorbed_cost = sum(_objective(rectangle.zone, request) for rectangle in absorbed)
    return _MergeCandidate(
        absorbed_keys=tuple(sorted(rectangle.key for rectangle in absorbed)),
        rectangle=merged,
        objective_delta=_objective(merged.zone, request) - absorbed_cost,
    )


class SpatialPartitionGreedyOptimizer:
    """Строить зоны из пространственных плиток и укрупнять только допустимые bbox."""

    name = "spatial-partition-greedy"

    def solve(
        self,
        problem: LayoutProblem,
        request: AlgorithmRequest | None = None,
    ) -> LayoutSolution:
        started = perf_counter()
        request = request or AlgorithmRequest()
        detail_limit = request.max_details
        min_improvement = float(request.params.get("min_improvement_kg", 0.0))
        if min_improvement < 0:
            raise ValueError("min_improvement_kg не может быть отрицательным")

        if not demanded_cells(problem):
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
                    "kind": "spatial_grid_partition_greedy",
                    "baseline": True,
                    "initial_rectangle_count": 0,
                    "merge_log": [],
                    "merge_trajectory": [],
                    "trajectory_pareto_front": [],
                },
            )

        grid = _build_grid(problem, request)
        context = prepare_detailing(problem)
        rectangles = _initial_rectangles(problem, grid, context)
        initial_rectangles = tuple(
            {"bbox": rectangle.zone.demand_bbox, "level_index": rectangle.level_index}
            for rectangle in rectangles
        )
        initial_count = len(rectangles)
        trajectory: list[dict[str, float | int]] = [
            {
                "zone_count": initial_count,
                "total_mass_kg": sum(rectangle.zone.mass_kg for rectangle in rectangles),
            }
        ]
        merge_log: list[dict[str, float | int | list[int]]] = []
        rejected_forbidden = 0
        rejected_partial_overlap = 0
        next_key = initial_count
        maximum_merges = int(request.params.get("maximum_merges", 5_000))
        if maximum_merges < 0:
            raise ValueError("maximum_merges не может быть отрицательным")
        maximum_merge_reduction = int(
            request.params.get("maximum_detail_reduction_per_merge", 4)
        )
        if maximum_merge_reduction < 1:
            raise ValueError(
                "maximum_detail_reduction_per_merge должен быть не меньше 1"
            )
        timed_out = False

        while len(rectangles) > 1 and len(merge_log) < maximum_merges:
            if request.time_limit_s is not None and perf_counter() - started >= request.time_limit_s:
                timed_out = True
                break
            candidates: list[_MergeCandidate] = []
            candidate_signatures: set[tuple[int, ...]] = set()
            for first_index, second_index in _neighbor_pairs(rectangles):
                first = rectangles[first_index]
                second = rectangles[second_index]
                hull = (
                    min(first.row_start, second.row_start),
                    max(first.row_end, second.row_end),
                    min(first.column_start, second.column_start),
                    max(first.column_end, second.column_end),
                )
                if not grid.is_fully_allowed(*hull):
                    rejected_forbidden += 1
                    continue
                candidate = _candidate(
                    problem,
                    request,
                    grid,
                    context,
                    rectangles,
                    first_index,
                    second_index,
                    next_key,
                )
                if candidate is None:
                    rejected_partial_overlap += 1
                    continue
                if candidate.absorbed_keys in candidate_signatures:
                    continue
                candidate_signatures.add(candidate.absorbed_keys)
                candidates.append(candidate)
            if not candidates:
                break

            local_candidates = [
                candidate
                for candidate in candidates
                if candidate.detail_reduction <= maximum_merge_reduction
            ]
            if not local_candidates:
                break
            if detail_limit is not None and len(rectangles) > detail_limit:
                remaining_reduction = len(rectangles) - detail_limit
                non_overshooting = [
                    candidate
                    for candidate in local_candidates
                    if candidate.detail_reduction <= remaining_reduction
                ]
                if non_overshooting:
                    local_candidates = non_overshooting

            best = min(
                local_candidates,
                key=lambda candidate: (
                    candidate.objective_delta / candidate.detail_reduction,
                    candidate.objective_delta,
                    -candidate.detail_reduction,
                    candidate.rectangle.key,
                    candidate.absorbed_keys,
                ),
            )
            must_reduce_count = detail_limit is not None and len(rectangles) > detail_limit
            if not must_reduce_count and best.objective_delta >= -min_improvement:
                break

            absorbed_keys = set(best.absorbed_keys)
            rectangles = [
                rectangle for rectangle in rectangles if rectangle.key not in absorbed_keys
            ]
            rectangles.append(best.rectangle)
            rectangles.sort(
                key=lambda rectangle: (
                    rectangle.row_start,
                    rectangle.column_start,
                    rectangle.row_end,
                    rectangle.column_end,
                    rectangle.key,
                )
            )
            merge_log.append(
                {
                    "absorbed_zone_keys": list(best.absorbed_keys),
                    "detail_reduction": best.detail_reduction,
                    "objective_delta": best.objective_delta,
                    "zone_count": len(rectangles),
                    "level_index": best.rectangle.level_index,
                }
            )
            trajectory.append(
                {
                    "zone_count": len(rectangles),
                    "total_mass_kg": sum(
                        rectangle.zone.mass_kg for rectangle in rectangles
                    ),
                }
            )
            next_key += 1

        final_rectangles = [
            _make_rectangle(
                problem,
                grid,
                context,
                key=index,
                row_start=rectangle.row_start,
                row_end=rectangle.row_end,
                column_start=rectangle.column_start,
                column_end=rectangle.column_end,
                level_index=rectangle.level_index,
                source_cell_ids=rectangle.source_cell_ids,
                collect_coverage=True,
            )
            for index, rectangle in enumerate(rectangles, 1)
        ]
        zones = tuple(rectangle.zone for rectangle in final_rectangles)
        evaluation = evaluate_layout(problem, zones, request)
        if timed_out:
            status = SolutionStatus.TIME_LIMIT
        else:
            status = SolutionStatus.FEASIBLE if evaluation.valid else SolutionStatus.ERROR

        forbidden_tiles = tuple(
            grid.bbox(row, row + 1, column, column + 1)
            for row in range(grid.row_count)
            for column in range(grid.column_count)
            if not grid.allowed[row][column]
        )
        return LayoutSolution(
            algorithm=self.name,
            status=status,
            zones=zones,
            metrics=evaluation.metrics,
            request=request,
            runtime_ms=(perf_counter() - started) * 1000.0,
            diagnostics=evaluation.diagnostics,
            meta={
                "kind": "spatial_grid_partition_greedy",
                "baseline": True,
                "grid_shape": [grid.row_count, grid.column_count],
                "grid_cell_size_mm": [
                    (grid.x_edges[-1] - grid.x_edges[0]) / grid.column_count,
                    (grid.y_edges[-1] - grid.y_edges[0]) / grid.row_count,
                ],
                "allowed_tile_count": sum(sum(row) for row in grid.allowed),
                "demanded_tile_count": sum(
                    level is not None for row in grid.levels for level in row
                ),
                "forbidden_tile_count": len(forbidden_tiles),
                "initial_rectangle_count": initial_count,
                "merge_log": merge_log,
                "merge_trajectory": trajectory,
                "trajectory_pareto_front": _nondominated_trace(trajectory),
                "maximum_detail_reduction_per_merge": maximum_merge_reduction,
                "rejected_merge_counts": {
                    "forbidden_tiles": rejected_forbidden,
                    "partial_overlap": rejected_partial_overlap,
                },
                "partition_debug": {
                    "x_edges": grid.x_edges,
                    "y_edges": grid.y_edges,
                    "forbidden_tiles": forbidden_tiles,
                    "initial_rectangles": initial_rectangles,
                },
            },
        )
