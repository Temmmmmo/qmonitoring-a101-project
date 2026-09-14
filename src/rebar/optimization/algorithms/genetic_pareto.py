"""Воспроизводимый многокритериальный генетический поиск раскладок.

Первая версия использует пространственный CandidateSet: один проход строит допустимые
прямоугольники из регулярного разбиения, а хромосома выбирает их подмножество. Repair
гарантирует покрытие всех исходных атомов перед независимой инженерной проверкой.
NSGA-II-подобный отбор оптимизирует массу и число зон без свёртки в один score.
"""

from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass, replace
from time import perf_counter

from rebar.models import Axis

from ..contracts import (
    AlgorithmRequest,
    BBox,
    ComplexityAxis,
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
    prepare_detailing,
    resolve_zone_phases,
)
from ..services.geometry import GEOMETRY_TOLERANCE_MM
from ..services.cutting import CutLengthInfeasibleError
from ..services.bar_schedule import zone_position_keys
from .agglomerative import AgglomerativeOptimizer
from .bsp import BspOptimizer
from .genetic import (
    MUTATION_OPERATORS,
    OperatorPolicy,
    build_operator_policy,
    mutate_genome,
)
from .genetic.operators import available_operators
from .genetic.candidates import layered_geometry_variants
from .genetic.local_search import improve_genome
from .genetic.pool_solver import polish_candidate_pool
from .genetic.recombination import expand_recombined_space
from .genetic.coverage import CoverageAtom as _AtomicLeaf, demand_fragments
from .genetic.host_filter import (
    CandidateGuard,
    check_materialized_zones,
    filter_candidate_space,
    missing_atoms,
    preserve_atom_boundaries,
)
from .greedy_priority import PriorityGreedyOptimizer
from .spatial_partition_greedy import (
    _Grid,
    _Rectangle,
    _build_grid,
    _candidate,
    _contains,
    _initial_rectangles,
    _make_rectangle,
    _neighbor_pairs,
)

_MASS_TOLERANCE_KG = 1e-6


@dataclass(frozen=True)
class _PoolCandidate:
    """Один допустимый прямоугольник и покрываемые им атомы разбиения."""

    rectangle: _Rectangle
    leaf_ids: frozenset[int]
    source_cell_ids: tuple[int, ...]
    origins: frozenset[str]

    @property
    def mass_kg(self) -> float:
        return self.rectangle.zone.mass_kg


@dataclass(frozen=True)
class _SearchSpace:
    grid: _Grid
    candidates: tuple[_PoolCandidate, ...]
    seed_genomes: tuple[frozenset[int], ...]
    baseline_seed_genomes: tuple[frozenset[int], ...]
    baseline_seed_algorithms: tuple[str, ...]
    baseline_seed_metrics: tuple[dict[str, float | int | str], ...]
    leaf_count: int
    initial_rectangle_count: int
    trajectory_count: int
    trajectory_state_count: int
    stopped_by_time_limit: bool
    leaves: tuple[_AtomicLeaf, ...] = ()


@dataclass(frozen=True)
class _Individual:
    genome: frozenset[int]
    mass_kg: float
    complexity: int

    @property
    def zone_count(self) -> int:
        return len(self.genome)


def _rectangle_signature(rectangle: _Rectangle) -> tuple[int, int, int, int, int]:
    return (
        rectangle.row_start,
        rectangle.row_end,
        rectangle.column_start,
        rectangle.column_end,
        rectangle.level_index,
    )


def _pool_sort_key(candidate: _PoolCandidate) -> tuple[int, int, int, int, int]:
    return _rectangle_signature(candidate.rectangle)


def _atomic_leaves(grid: _Grid) -> tuple[_AtomicLeaf, ...]:
    return tuple(
        _AtomicLeaf(
            row_start=row,
            row_end=row + 1,
            column_start=column,
            column_end=column + 1,
            level_index=level,
            source_cell_ids=grid.source_cell_ids[row][column],
            bbox=grid.bbox(row, row + 1, column, column + 1),
        )
        for row, levels in enumerate(grid.levels)
        for column, level in enumerate(levels)
        if level is not None
    )


def _leaf_ids_for_rectangle(
    rectangle: _Rectangle,
    leaves: tuple[_AtomicLeaf, ...],
) -> frozenset[int]:
    hull = (
        rectangle.row_start,
        rectangle.row_end,
        rectangle.column_start,
        rectangle.column_end,
    )
    return frozenset(
        leaf_id
        for leaf_id, leaf in enumerate(leaves)
        if leaf.level_index <= rectangle.level_index and _contains(hull, leaf)
        and _bbox_contains(rectangle.zone.demand_bbox, leaf.bbox)
    )


def _bbox_contains(outer: BBox, inner: BBox) -> bool:
    """Индексы соседства могут округляться наружу; покрытие — только реальная геометрия."""

    return (
        outer[0] <= inner[0] + GEOMETRY_TOLERANCE_MM
        and outer[1] <= inner[1] + GEOMETRY_TOLERANCE_MM
        and outer[2] >= inner[2] - GEOMETRY_TOLERANCE_MM
        and outer[3] >= inner[3] - GEOMETRY_TOLERANCE_MM
    )


_BASELINE_SEED_OPTIMIZERS = {
    "agglomerative": AgglomerativeOptimizer,
    "bsp": BspOptimizer,
    "greedy-priority": PriorityGreedyOptimizer,
}


def _covering_grid_range(
    edges: tuple[float, ...],
    lower: float,
    upper: float,
) -> tuple[int, int]:
    """Привязать bbox baseline к покрывающим его границам регулярной сетки."""

    start = max(
        0,
        min(
            len(edges) - 2,
            bisect.bisect_right(edges, lower + GEOMETRY_TOLERANCE_MM) - 1,
        ),
    )
    end = max(
        start + 1,
        min(
            len(edges) - 1,
            bisect.bisect_left(edges, upper - GEOMETRY_TOLERANCE_MM),
        ),
    )
    return start, end


def _baseline_seed_rectangles(
    problem: LayoutProblem,
    request: AlgorithmRequest,
    grid: _Grid,
    context: DetailingContext,
    leaves: tuple[_AtomicLeaf, ...],
    algorithm_names: tuple[str, ...],
    *,
    deadline: float | None,
) -> tuple[
    tuple[str, tuple[tuple[_Rectangle, frozenset[int]], ...], dict[str, float | int | str]],
    ...,
]:
    """Материализовать допустимые frozen-baseline как геометрические seed GA."""

    result = []
    next_key = 2_000_000
    baseline_request = AlgorithmRequest(
        objective=request.objective,
        max_details=request.max_details,
        time_limit_s=None,
        params={},
    )
    for algorithm_name in algorithm_names:
        if deadline is not None and perf_counter() >= deadline:
            break
        optimizer_type = _BASELINE_SEED_OPTIMIZERS[algorithm_name]
        try:
            solution = optimizer_type().solve(problem, baseline_request)
        except CutLengthInfeasibleError:
            # Frozen-baseline может начать с общего bbox длиннее прутка. Это
            # необязательный seed: полное пространственное покрытие уже построено.
            continue
        if (
            solution.status not in {SolutionStatus.FEASIBLE, SolutionStatus.OPTIMAL}
            or solution.metrics.under_reinforced_cell_count != 0
        ):
            continue

        seed_rectangles: list[tuple[_Rectangle, frozenset[int]]] = []
        for zone in solution.zones:
            column_start, column_end = _covering_grid_range(
                grid.x_edges,
                zone.demand_bbox[0],
                zone.demand_bbox[2],
            )
            row_start, row_end = _covering_grid_range(
                grid.y_edges,
                zone.demand_bbox[1],
                zone.demand_bbox[3],
            )
            hull = (row_start, row_end, column_start, column_end)
            leaf_ids = frozenset(
                leaf_id
                for leaf_id, leaf in enumerate(leaves)
                if leaf.level_index <= zone.level_index and _contains(hull, leaf)
                and _bbox_contains(zone.demand_bbox, leaf.bbox)
            )
            # Частичная ячейка не засчитывается repair как полная. При этом все зоны
            # проверенного baseline, даже без целых атомов, сохраняются в его seed.
            source_ids = tuple(zone.meta.get("seed_cell_ids", zone.covered_cell_ids))
            # Индексы сетки нужны операторам соседства, но физическую геометрию и
            # стоимость seed сохраняем в точности как у проверенного baseline.
            rectangle = _Rectangle(
                key=next_key,
                row_start=row_start,
                row_end=row_end,
                column_start=column_start,
                column_end=column_end,
                level_index=zone.level_index,
                source_cell_ids=source_ids,
                zone=zone,
            )
            seed_rectangles.append((rectangle, leaf_ids))
            next_key += 1
        result.append(
            (
                algorithm_name,
                tuple(seed_rectangles),
                {
                    "algorithm": algorithm_name,
                    "zone_count": solution.metrics.detail_count,
                    "physical_bar_count": solution.metrics.physical_bar_count,
                    "mass_kg": solution.metrics.total_mass_kg,
                },
            )
        )
    return tuple(result)


def _layer_bridge_candidates(
    problem: LayoutProblem,
    grid: _Grid,
    context: DetailingContext,
    initial: list[_Rectangle],
    *,
    neighbor_span: int,
) -> tuple[tuple[_Rectangle, frozenset[int]], ...]:
    """Построить длинные зоны одного уровня под локальными сильными накладками."""

    result: dict[
        tuple[int, int, int, int, int],
        tuple[_Rectangle, frozenset[int]],
    ] = {}
    axis = problem.demand.direction.axis
    next_key = 1_000_000
    for first_index, first in enumerate(initial):
        aligned: list[tuple[int, int]] = []
        for second_index, second in enumerate(initial):
            if first_index == second_index or first.level_index != second.level_index:
                continue
            if axis is Axis.X:
                transverse_overlap = min(first.row_end, second.row_end) > max(
                    first.row_start,
                    second.row_start,
                )
                if not transverse_overlap or second.column_start < first.column_end:
                    continue
                gap = second.column_start - first.column_end
            else:
                transverse_overlap = min(first.column_end, second.column_end) > max(
                    first.column_start,
                    second.column_start,
                )
                if not transverse_overlap or second.row_start < first.row_end:
                    continue
                gap = second.row_start - first.row_end
            aligned.append((gap, second_index))

        for _gap, second_index in sorted(aligned)[:neighbor_span]:
            second = initial[second_index]
            hull = (
                min(first.row_start, second.row_start),
                max(first.row_end, second.row_end),
                min(first.column_start, second.column_start),
                max(first.column_end, second.column_end),
            )
            if not grid.is_fully_allowed(*hull):
                continue
            covered_leaves = frozenset(
                leaf_id
                for leaf_id, rectangle in enumerate(initial)
                if rectangle.level_index <= first.level_index
                and _contains(hull, rectangle)
            )
            if len(covered_leaves) < 2:
                continue
            source_ids = tuple(
                sorted(
                    {
                        cell_id
                        for leaf_id in covered_leaves
                        for cell_id in initial[leaf_id].source_cell_ids
                    }
                )
            )
            try:
                rectangle = _make_rectangle(
                    problem,
                    grid,
                    context,
                    key=next_key,
                    row_start=hull[0],
                    row_end=hull[1],
                    column_start=hull[2],
                    column_end=hull[3],
                    level_index=first.level_index,
                    source_cell_ids=source_ids,
                )
            except CutLengthInfeasibleError:
                continue
            result.setdefault(
                _rectangle_signature(rectangle),
                (rectangle, covered_leaves),
            )
            next_key += 1
    return tuple(result.values())


def _build_search_space(
    problem: LayoutProblem,
    request: AlgorithmRequest,
    *,
    candidate_window: int,
    candidate_trajectories: int,
    layer_bridge_span: int,
    maximum_merge_reduction: int,
    maximum_pool_merges: int,
    baseline_seed_algorithms: tuple[str, ...],
    random_seed: int,
    deadline: float | None,
    candidate_expansion: str = "none",
    maximum_layer_variants: int = 256,
    coverage_atoms: str = "demand_fragments",
) -> _SearchSpace:
    """Собрать CandidateSet по нескольким воспроизводимым merge-траекториям."""

    grid = _build_grid(problem, request)
    context = prepare_detailing(problem)
    initial = _initial_rectangles(
        problem,
        grid,
        context,
        align_with_bar_axis=True,
    )
    if request.max_details is not None and request.max_details < len(initial):
        initial = _initial_rectangles(problem, grid, context)
    leaves = _atomic_leaves(grid)
    baseline_seeds = _baseline_seed_rectangles(
        problem, request, grid, context, leaves, baseline_seed_algorithms, deadline=deadline,
    )
    if coverage_atoms == "demand_fragments":
        leaves = demand_fragments(problem, grid, tuple(
            rectangle.zone.demand_bbox
            for _name, rectangles, _metrics in baseline_seeds
            for rectangle, _ids in rectangles
        ))
        baseline_seeds = tuple(
            (name, tuple((rectangle, _leaf_ids_for_rectangle(rectangle, leaves))
                         for rectangle, _ids in rectangles), metrics)
            for name, rectangles, metrics in baseline_seeds
        )

    pool_rectangles: dict[tuple[int, int, int, int, int], _Rectangle] = {}
    pool_leaves: dict[tuple[int, int, int, int, int], set[int]] = {}
    pool_origins: dict[tuple[int, int, int, int, int], set[str]] = {}
    trajectory_states: list[tuple[tuple[int, int, int, int, int], ...]] = []

    def add_to_pool(
        rectangle: _Rectangle,
        leaf_ids: frozenset[int],
        origin: str,
    ) -> None:
        signature = _rectangle_signature(rectangle)
        pool_rectangles.setdefault(signature, rectangle)
        pool_leaves.setdefault(signature, set()).update(leaf_ids)
        pool_origins.setdefault(signature, set()).add(origin)

    for rectangle in initial:
        add_to_pool(
            rectangle,
            _leaf_ids_for_rectangle(rectangle, leaves),
            "spatial-atom",
        )
    for rectangle, _initial_leaf_ids in _layer_bridge_candidates(
        problem,
        grid,
        context,
        initial,
        neighbor_span=layer_bridge_span,
    ):
        add_to_pool(
            rectangle,
            _leaf_ids_for_rectangle(rectangle, leaves),
            "layer-bridge",
        )

    baseline_metrics = [metrics for _name, _rectangles, metrics in baseline_seeds]

    pool_rng = random.Random(random_seed ^ 0xA101)
    total_merge_count = 0
    completed_trajectories = 0
    stopped_by_time_limit = False

    for trajectory_index in range(candidate_trajectories):
        if total_merge_count >= maximum_pool_merges:
            break
        if deadline is not None and perf_counter() >= deadline:
            stopped_by_time_limit = True
            break

        rectangles = list(initial)
        leaves_by_key: dict[int, frozenset[int]] = {
            rectangle.key: _leaf_ids_for_rectangle(rectangle, leaves)
            for rectangle in initial
        }
        next_key = len(rectangles)
        trajectory_states.append(
            tuple(_rectangle_signature(item) for item in rectangles)
        )
        completed_trajectories += 1

        while len(rectangles) > 1 and total_merge_count < maximum_pool_merges:
            if deadline is not None and perf_counter() >= deadline:
                stopped_by_time_limit = True
                break

            merge_candidates = []
            seen_absorbed: set[tuple[int, ...]] = set()
            for first_index, second_index in _neighbor_pairs(rectangles):
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
                if (
                    candidate is None
                    or candidate.detail_reduction > maximum_merge_reduction
                    or candidate.absorbed_keys in seen_absorbed
                ):
                    continue
                seen_absorbed.add(candidate.absorbed_keys)
                merge_candidates.append(candidate)
            if not merge_candidates:
                break

            strategy = trajectory_index % 3
            if strategy == 0:
                merge_candidates.sort(
                    key=lambda item: (
                        item.objective_delta / item.detail_reduction,
                        item.objective_delta,
                        -item.detail_reduction,
                        _rectangle_signature(item.rectangle),
                        item.absorbed_keys,
                    )
                )
            elif strategy == 1:
                merge_candidates.sort(
                    key=lambda item: (
                        item.objective_delta,
                        item.objective_delta / item.detail_reduction,
                        -item.detail_reduction,
                        _rectangle_signature(item.rectangle),
                        item.absorbed_keys,
                    )
                )
            else:
                merge_candidates.sort(
                    key=lambda item: (
                        -item.detail_reduction,
                        item.objective_delta / item.detail_reduction,
                        item.objective_delta,
                        _rectangle_signature(item.rectangle),
                        item.absorbed_keys,
                    )
                )

            alternatives = merge_candidates[:candidate_window]
            for alternative in alternatives:
                alternative_leaves = frozenset(
                    leaf_id
                    for key in alternative.absorbed_keys
                    for leaf_id in leaves_by_key[key]
                )
                add_to_pool(
                    alternative.rectangle,
                    alternative_leaves,
                    "merge-alternative",
                )

            chosen = (
                alternatives[0]
                if trajectory_index == 0
                else alternatives[
                    pool_rng.randrange(min(6, len(alternatives)))
                ]
            )
            absorbed = set(chosen.absorbed_keys)
            chosen_leaves = frozenset(
                leaf_id
                for key in chosen.absorbed_keys
                for leaf_id in leaves_by_key[key]
            )
            rectangles = [item for item in rectangles if item.key not in absorbed]
            rectangles.append(chosen.rectangle)
            rectangles.sort(
                key=lambda item: (
                    item.row_start,
                    item.column_start,
                    item.row_end,
                    item.column_end,
                    item.key,
                )
            )
            for key in absorbed:
                leaves_by_key.pop(key)
            leaves_by_key[chosen.rectangle.key] = chosen_leaves
            add_to_pool(chosen.rectangle, chosen_leaves, "merge-trajectory")
            trajectory_states.append(
                tuple(_rectangle_signature(item) for item in rectangles)
            )
            next_key += 1
            total_merge_count += 1

        if stopped_by_time_limit:
            break

    # Baseline может намеренно использовать более сильный уровень на bbox. Добавляем
    # минимально достаточный вариант той же геометрии, чтобы change-level мог убрать
    # локальный перерасход, не изобретая непроверенную форму.
    for signature, rectangle in tuple(pool_rectangles.items()):
        leaf_ids = frozenset(pool_leaves[signature])
        if not leaf_ids:
            continue
        minimum_level = max(leaves[leaf_id].level_index for leaf_id in leaf_ids)
        if minimum_level == rectangle.level_index:
            continue
        try:
            variant = _make_rectangle(
                problem,
                grid,
                context,
                key=3_000_000 + len(pool_rectangles),
                row_start=rectangle.row_start,
                row_end=rectangle.row_end,
                column_start=rectangle.column_start,
                column_end=rectangle.column_end,
                level_index=minimum_level,
                source_cell_ids=tuple(
                    sorted(
                        {
                            cell_id
                            for leaf_id in leaf_ids
                            for cell_id in leaves[leaf_id].source_cell_ids
                        }
                    )
                ),
            )
        except CutLengthInfeasibleError:
            continue
        add_to_pool(variant, leaf_ids, "minimal-level-variant")

    ordered_signatures = sorted(pool_rectangles)
    index_by_signature = {
        signature: index for index, signature in enumerate(ordered_signatures)
    }
    pool: list[_PoolCandidate] = []
    for signature in ordered_signatures:
        leaf_ids = frozenset(pool_leaves[signature])
        source_ids = tuple(
            sorted(
                {
                    cell_id
                    for leaf_id in leaf_ids
                    for cell_id in leaves[leaf_id].source_cell_ids
                }
            )
        )
        pool.append(
            _PoolCandidate(
                rectangle=pool_rectangles[signature],
                leaf_ids=leaf_ids,
                source_cell_ids=source_ids,
                origins=frozenset(pool_origins[signature]),
            )
        )

    baseline_genomes_list: list[frozenset[int]] = []
    for algorithm_name, rectangles, _metrics in baseline_seeds:
        candidate_indexes = []
        for rectangle, leaf_ids in rectangles:
            candidate_indexes.append(len(pool))
            pool.append(
                _PoolCandidate(
                    rectangle=rectangle,
                    leaf_ids=leaf_ids,
                    source_cell_ids=rectangle.source_cell_ids,
                    origins=frozenset((f"baseline:{algorithm_name}",)),
                )
            )
        baseline_genomes_list.append(frozenset(candidate_indexes))

    if candidate_expansion == "layered":
        for rectangle in layered_geometry_variants(
            problem, grid, context, tuple(candidate.rectangle for candidate in pool),
            maximum_variants=maximum_layer_variants,
        ):
            pool.append(_PoolCandidate(
                rectangle=rectangle,
                leaf_ids=_leaf_ids_for_rectangle(rectangle, leaves),
                source_cell_ids=rectangle.source_cell_ids,
                origins=frozenset(("layered-envelope",)),
            ))

    trajectory_genomes = tuple(
        dict.fromkeys(
            frozenset(index_by_signature[signature] for signature in state)
            for state in trajectory_states
        )
    )
    initial_genome = frozenset(
        index_by_signature[_rectangle_signature(rectangle)]
        for rectangle in initial
    )
    initial_candidate_indexes = frozenset(
        index_by_signature[_rectangle_signature(rectangle)] for rectangle in initial
    )
    replacement_genomes = tuple(
        frozenset(
            {
                *(
                    initial_genome
                    - {
                        index
                        for index in initial_candidate_indexes
                        if pool[index].leaf_ids <= candidate.leaf_ids
                    }
                ),
                candidate_index,
            }
        )
        for candidate_index, candidate in enumerate(pool)
        if len(candidate.leaf_ids) > 1
    )
    baseline_genomes = tuple(dict.fromkeys(baseline_genomes_list))
    seed_genomes = tuple(
        dict.fromkeys(
            (*baseline_genomes, *trajectory_genomes, *replacement_genomes)
        )
    )
    return _SearchSpace(
        grid=grid,
        candidates=tuple(pool),
        seed_genomes=seed_genomes,
        baseline_seed_genomes=baseline_genomes,
        baseline_seed_algorithms=tuple(name for name, _rectangles, _metrics in baseline_seeds),
        baseline_seed_metrics=tuple(baseline_metrics),
        leaf_count=len(leaves),
        initial_rectangle_count=len(initial),
        trajectory_count=completed_trajectories,
        trajectory_state_count=len(trajectory_states),
        stopped_by_time_limit=stopped_by_time_limit,
        leaves=leaves,
    )


def _coverage_counts(
    candidates: tuple[_PoolCandidate, ...],
    selected: set[int],
    leaf_count: int,
) -> list[int]:
    counts = [0] * leaf_count
    for candidate_index in selected:
        for leaf_id in candidates[candidate_index].leaf_ids:
            counts[leaf_id] += 1
    return counts


def _prune_redundant(
    candidates: tuple[_PoolCandidate, ...],
    selected: set[int],
    leaf_count: int,
) -> None:
    counts = _coverage_counts(candidates, selected, leaf_count)
    removal_order = sorted(
        selected,
        key=lambda index: (
            candidates[index].mass_kg,
            -len(candidates[index].leaf_ids),
            index,
        ),
        reverse=True,
    )
    for candidate_index in removal_order:
        leaf_ids = candidates[candidate_index].leaf_ids
        if not leaf_ids or any(counts[leaf_id] <= 1 for leaf_id in leaf_ids):
            continue
        selected.remove(candidate_index)
        for leaf_id in leaf_ids:
            counts[leaf_id] -= 1


def _fill_uncovered(
    candidates: tuple[_PoolCandidate, ...],
    selected: set[int],
    leaf_count: int,
    rng: random.Random,
) -> None:
    counts = _coverage_counts(candidates, selected, leaf_count)
    uncovered = {leaf_id for leaf_id, count in enumerate(counts) if count == 0}
    while uncovered:
        ranked: list[tuple[float, float, int, int]] = []
        for candidate_index, candidate in enumerate(candidates):
            if candidate_index in selected:
                continue
            gain = len(candidate.leaf_ids & uncovered)
            if gain == 0:
                continue
            ranked.append(
                (
                    candidate.mass_kg / gain,
                    -gain,
                    len(candidate.leaf_ids),
                    candidate_index,
                )
            )
        if not ranked:
            raise RuntimeError("CandidateSet не может восстановить покрытие атомов")
        ranked.sort()
        window = ranked[: min(4, len(ranked))]
        chosen = window[rng.randrange(len(window))][3]
        selected.add(chosen)
        for leaf_id in candidates[chosen].leaf_ids:
            counts[leaf_id] += 1
        uncovered.difference_update(candidates[chosen].leaf_ids)


def _compress_to_limit(
    candidates: tuple[_PoolCandidate, ...],
    selected: set[int],
    limit: int,
) -> None:
    while len(selected) > limit:
        replacements: list[tuple[int, float, int, tuple[int, ...]]] = []
        for candidate_index, candidate in enumerate(candidates):
            if candidate_index in selected:
                continue
            removable = tuple(
                index
                for index in selected
                if candidates[index].leaf_ids <= candidate.leaf_ids
            )
            reduction = len(removable) - 1
            if reduction <= 0:
                continue
            mass_delta = candidate.mass_kg - sum(
                candidates[index].mass_kg for index in removable
            )
            replacements.append(
                (-reduction, mass_delta, candidate_index, removable)
            )
        if not replacements:
            break
        _negative_reduction, _mass_delta, candidate_index, removable = min(replacements)
        selected.difference_update(removable)
        selected.add(candidate_index)


def _repair(
    space: _SearchSpace,
    genome: frozenset[int],
    *,
    detail_limit: int,
    rng: random.Random,
) -> frozenset[int]:
    selected = {
        index for index in genome if 0 <= index < len(space.candidates)
    }
    _fill_uncovered(space.candidates, selected, space.leaf_count, rng)
    _prune_redundant(space.candidates, selected, space.leaf_count)
    _compress_to_limit(space.candidates, selected, detail_limit)
    _prune_redundant(space.candidates, selected, space.leaf_count)
    return frozenset(selected)


def _individual(
    space: _SearchSpace,
    genome: frozenset[int],
    complexity_axis: ComplexityAxis,
) -> _Individual:
    if complexity_axis is ComplexityAxis.ZONE_COUNT:
        complexity = len(genome)
    elif complexity_axis is ComplexityAxis.PHYSICAL_BAR_COUNT:
        complexity = sum(
            space.candidates[index].rectangle.zone.bar_count for index in genome
        )
    elif complexity_axis is ComplexityAxis.POSITION_COUNT:
        complexity = len(zone_position_keys(space.candidates[index].rectangle.zone for index in genome))
    else:
        raise ValueError(f"неподдерживаемая ось сложности GA: {complexity_axis}")
    return _Individual(
        genome=genome,
        mass_kg=sum(space.candidates[index].mass_kg for index in genome),
        complexity=complexity,
    )


def _dominates(first: _Individual, second: _Individual) -> bool:
    no_worse = (
        first.mass_kg <= second.mass_kg + _MASS_TOLERANCE_KG
        and first.complexity <= second.complexity
    )
    strictly_better = (
        first.mass_kg < second.mass_kg - _MASS_TOLERANCE_KG
        or first.complexity < second.complexity
    )
    return no_worse and strictly_better


def _non_dominated_fronts(
    population: tuple[_Individual, ...],
) -> tuple[tuple[_Individual, ...], ...]:
    domination_counts: dict[_Individual, int] = {item: 0 for item in population}
    dominated: dict[_Individual, list[_Individual]] = {item: [] for item in population}
    first_front: list[_Individual] = []
    for candidate in population:
        for other in population:
            if candidate is other:
                continue
            if _dominates(candidate, other):
                dominated[candidate].append(other)
            elif _dominates(other, candidate):
                domination_counts[candidate] += 1
        if domination_counts[candidate] == 0:
            first_front.append(candidate)

    fronts: list[tuple[_Individual, ...]] = []
    current = tuple(first_front)
    while current:
        fronts.append(current)
        following: list[_Individual] = []
        for candidate in current:
            for other in dominated[candidate]:
                domination_counts[other] -= 1
                if domination_counts[other] == 0:
                    following.append(other)
        current = tuple(following)
    return tuple(fronts)


def _crowding(front: tuple[_Individual, ...]) -> dict[_Individual, float]:
    distance = {candidate: 0.0 for candidate in front}
    if len(front) <= 2:
        return {candidate: math.inf for candidate in front}
    for value in (
        lambda item: item.mass_kg,
        lambda item: float(item.complexity),
    ):
        ordered = sorted(front, key=value)
        distance[ordered[0]] = math.inf
        distance[ordered[-1]] = math.inf
        lower = value(ordered[0])
        upper = value(ordered[-1])
        span = upper - lower
        if span <= 0:
            continue
        for index in range(1, len(ordered) - 1):
            if math.isinf(distance[ordered[index]]):
                continue
            distance[ordered[index]] += (
                value(ordered[index + 1]) - value(ordered[index - 1])
            ) / span
    return distance


def _unique_individuals(
    individuals: list[_Individual] | tuple[_Individual, ...],
) -> tuple[_Individual, ...]:
    by_genome: dict[frozenset[int], _Individual] = {}
    for individual in individuals:
        by_genome.setdefault(individual.genome, individual)
    return tuple(by_genome.values())


def _select_population(
    candidates: list[_Individual] | tuple[_Individual, ...],
    population_size: int,
) -> tuple[_Individual, ...]:
    unique = _unique_individuals(candidates)
    selected: list[_Individual] = []
    for front in _non_dominated_fronts(unique):
        remaining = population_size - len(selected)
        if remaining <= 0:
            break
        if len(front) <= remaining:
            selected.extend(front)
            continue
        crowding = _crowding(front)
        selected.extend(
            sorted(
                front,
                key=lambda item: (
                    -crowding[item],
                    item.complexity,
                    item.zone_count,
                    item.mass_kg,
                    tuple(sorted(item.genome)),
                ),
            )[:remaining]
        )
    return tuple(selected)


def _rank_and_crowding(
    population: tuple[_Individual, ...],
) -> tuple[dict[_Individual, int], dict[_Individual, float]]:
    rank: dict[_Individual, int] = {}
    crowding: dict[_Individual, float] = {}
    for front_rank, front in enumerate(_non_dominated_fronts(population)):
        crowding.update(_crowding(front))
        for candidate in front:
            rank[candidate] = front_rank
    return rank, crowding


def _tournament(
    population: tuple[_Individual, ...],
    rank: dict[_Individual, int],
    crowding: dict[_Individual, float],
    rng: random.Random,
) -> _Individual:
    first = population[rng.randrange(len(population))]
    second = population[rng.randrange(len(population))]
    return min(
        (first, second),
        key=lambda item: (
            rank[item],
            -crowding[item],
            item.complexity,
            item.zone_count,
            item.mass_kg,
            tuple(sorted(item.genome)),
        ),
    )


def _crossover(
    first: frozenset[int],
    second: frozenset[int],
    rng: random.Random,
) -> frozenset[int]:
    union = first | second
    return frozenset(
        index
        for index in union
        if (index in first and index in second) or rng.random() < 0.5
    )


def _operator_reward(
    parent: _Individual,
    child: _Individual,
    *,
    novel: bool,
) -> float:
    """Оценить локальное улучшение двух Парето-целей для online AOS."""

    if child.genome == parent.genome:
        return -0.25
    mass_gain = (parent.mass_kg - child.mass_kg) / max(parent.mass_kg, 1.0)
    complexity_gain = (parent.complexity - child.complexity) / max(
        parent.complexity,
        1,
    )
    dominance = 0.5 if _dominates(child, parent) else 0.0
    if _dominates(parent, child):
        dominance = -0.5
    novelty_bonus = 0.05 if novel else -0.05
    return max(
        -1.0,
        min(1.0, dominance + mass_gain + complexity_gain + novelty_bonus),
    )


def _mutate_with_policy(
    space: _SearchSpace,
    genome: frozenset[int],
    policy: OperatorPolicy,
    rng: random.Random,
) -> tuple[frozenset[int], str | None]:
    available = available_operators(space, genome)
    if not available:
        return genome, None
    operator = policy.choose(available, rng)
    outcome = mutate_genome(space, genome, operator, rng)
    return outcome.genome, outcome.operator


def _sample_seed_genomes(
    space: _SearchSpace,
    detail_limit: int,
    population_size: int,
    complexity_axis: ComplexityAxis,
) -> list[frozenset[int]]:
    eligible = tuple(sorted(
        {
            genome for genome in space.seed_genomes if len(genome) <= detail_limit
        },
        key=lambda genome: (len(genome), tuple(sorted(genome))),
    ))
    if not eligible:
        eligible = space.seed_genomes[-1:]
    protected_baselines = tuple(
        genome
        for genome in space.baseline_seed_genomes
        if genome in eligible
    )[:population_size]
    remaining_size = population_size - len(protected_baselines)
    other_eligible = tuple(
        genome for genome in eligible if genome not in protected_baselines
    )
    selected = _select_population(
        [_individual(space, genome, complexity_axis) for genome in other_eligible],
        remaining_size,
    )
    return [*protected_baselines, *(individual.genome for individual in selected)]


def _evolve(
    space: _SearchSpace,
    *,
    detail_limit: int,
    population_size: int,
    generations: int,
    crossover_rate: float,
    mutation_rate: float,
    complexity_axis: ComplexityAxis,
    operator_policy: OperatorPolicy,
    rng: random.Random,
    deadline: float | None,
) -> tuple[
    tuple[_Individual, ...],
    tuple[dict[str, object], ...],
    bool,
    dict[str, object],
]:
    seed_genomes = _sample_seed_genomes(
        space,
        detail_limit,
        population_size,
        complexity_axis,
    )
    population = [
        _individual(
            space,
            (
                genome
                if genome in space.baseline_seed_genomes
                else _repair(space, genome, detail_limit=detail_limit, rng=rng)
            ),
            complexity_axis,
        )
        for genome in seed_genomes
    ]
    for _index in range(population_size):
        greedy_cover = _repair(
            space,
            frozenset(),
            detail_limit=detail_limit,
            rng=rng,
        )
        population.append(_individual(space, greedy_cover, complexity_axis))
    attempts = 0
    while len(_unique_individuals(population)) < population_size and attempts < population_size * 20:
        parent = population[rng.randrange(len(population))]
        mutated, operator = _mutate_with_policy(
            space,
            parent.genome,
            operator_policy,
            rng,
        )
        repaired = _repair(space, mutated, detail_limit=detail_limit, rng=rng)
        child = _individual(space, repaired, complexity_axis)
        if operator is not None:
            operator_policy.update(
                operator,
                _operator_reward(
                    parent,
                    child,
                    novel=all(child.genome != item.genome for item in population),
                ),
            )
        population.append(child)
        attempts += 1
    population_tuple = _select_population(population, population_size)
    protected_baselines = tuple(
        _individual(space, genome, complexity_axis)
        for genome in space.baseline_seed_genomes
        if len(genome) <= detail_limit
    )
    archive_front = _non_dominated_fronts(_unique_individuals(population))[0]
    history: list[dict[str, object]] = []
    stopped_by_time_limit = False

    for generation in range(generations):
        if deadline is not None and perf_counter() >= deadline:
            stopped_by_time_limit = True
            break
        rank, crowding = _rank_and_crowding(population_tuple)
        offspring: list[_Individual] = []
        while len(offspring) < population_size:
            first = _tournament(population_tuple, rank, crowding, rng)
            second = _tournament(population_tuple, rank, crowding, rng)
            genome = (
                _crossover(first.genome, second.genome, rng)
                if rng.random() < crossover_rate
                else first.genome
            )
            operator: str | None = None
            if rng.random() < mutation_rate:
                genome, operator = _mutate_with_policy(
                    space,
                    genome,
                    operator_policy,
                    rng,
                )
            repaired = _repair(space, genome, detail_limit=detail_limit, rng=rng)
            child = _individual(space, repaired, complexity_axis)
            if operator is not None:
                operator_policy.update(
                    operator,
                    _operator_reward(
                        first,
                        child,
                        novel=all(
                            child.genome != item.genome
                            for item in (*population_tuple, *offspring)
                        ),
                    ),
                )
            offspring.append(child)
        population_tuple = _select_population(
            [*population_tuple, *offspring],
            population_size,
        )
        first_front = _non_dominated_fronts(population_tuple)[0]
        archive_front = _non_dominated_fronts(
            _unique_individuals([*archive_front, *offspring])
        )[0]
        history.append(
            {
                "generation": generation + 1,
                "population_size": len(population_tuple),
                "front_size": len(first_front),
                "archive_front_size": len(archive_front),
                "operator_policy": operator_policy.snapshot(),
            }
        )

    return (
        _unique_individuals([*archive_front, *protected_baselines]),
        tuple(history),
        stopped_by_time_limit,
        operator_policy.snapshot(),
    )


def _materialize_genome(
    problem: LayoutProblem,
    space: _SearchSpace,
    genome: frozenset[int],
    context: DetailingContext,
) -> tuple[tuple[LayoutZone, ...], str | None]:
    """Общая детализация выбранного подмножества для GA и exact-oracle."""

    if genome in space.baseline_seed_genomes:
        return tuple(
            space.candidates[index].rectangle.zone for index in sorted(genome)
        ), None
    raw_zones = tuple(
        build_zone_from_bbox(
            problem,
            space.candidates[candidate_index].rectangle.zone.demand_bbox,
            space.candidates[candidate_index].rectangle.level_index,
            f"genetic-{index + 1}",
            seed_cell_ids=space.candidates[candidate_index].source_cell_ids,
            collect_coverage=False,
            context=context,
        )
        for index, candidate_index in enumerate(sorted(genome))
    )
    try:
        return tuple(resolve_zone_phases(problem, raw_zones, context=context)), None
    except ValueError as error:
        # Индивидуальные зоны всё равно проходят независимый hard-валидатор.
        checked_zones = tuple(build_zone_from_bbox(
            problem, zone.demand_bbox, zone.level_index, zone.id,
            seed_cell_ids=zone.meta["seed_cell_ids"], context=context,
            first_bar_coordinate_mm=zone.first_bar_coordinate_mm,
        ) for zone in raw_zones)
        return checked_zones, (
            "WARNING: repair поперечных фаз не нашёл совместный вариант: "
            f"{error}"
        )


class GeneticParetoOptimizer:
    """Искать приближённый фронт по массе и явно выбранной оси сложности."""

    name = "genetic-pareto"

    def __init__(self, *, candidate_guard: CandidateGuard | None = None):
        """Optional read-only predicate; not a request parameter or global policy.

        The default path stays unchanged. A caller may impose exact host/STO
        restrictions without altering the original demand, parser or registry.
        """
        if candidate_guard is not None and not callable(candidate_guard):
            raise TypeError("candidate_guard must be callable or None")
        self._candidate_guard = candidate_guard

    def solve_many(
        self,
        problem: LayoutProblem,
        request: AlgorithmRequest | None = None,
    ) -> tuple[LayoutSolution, ...]:
        started = perf_counter()
        request = request or AlgorithmRequest()
        params = request.params
        population_size = int(params.get("population_size", 16))
        generations = int(params.get("generations", 30))
        random_seed = int(params.get("random_seed", 42))
        crossover_rate = float(params.get("crossover_rate", 0.85))
        mutation_rate = float(params.get("mutation_rate", 0.35))
        candidate_window = int(params.get("candidate_window", 6))
        candidate_trajectories = int(params.get("candidate_trajectories", 3))
        layer_bridge_span = int(params.get("layer_bridge_span", 6))
        maximum_merge_reduction = int(
            params.get("maximum_detail_reduction_per_merge", 12)
        )
        maximum_pool_merges = int(params.get("maximum_pool_merges", 5_000))
        candidate_expansion = str(params.get("candidate_expansion", "none"))
        maximum_layer_variants = int(params.get("maximum_layer_variants", 256))
        local_search_passes = int(params.get("local_search_passes", 4))
        coverage_atoms = str(params.get("coverage_atoms", "demand_fragments"))
        pool_polish = str(params.get("pool_polish", "none"))
        pool_polish_solves = int(params.get("pool_polish_solves", 6))
        pool_polish_time_s = float(params.get("pool_polish_time_s", 10.0))
        recombination_variants = int(params.get("recombination_variants", 0))
        operator_policy_name = str(params.get("operator_policy", "ucb1"))
        ucb_exploration = float(params.get("ucb_exploration", math.sqrt(2.0)))
        raw_baseline_seed_algorithms = params.get(
            "baseline_seed_algorithms",
            tuple(_BASELINE_SEED_OPTIMIZERS),
        )
        if isinstance(raw_baseline_seed_algorithms, str):
            baseline_seed_algorithms = tuple(
                item.strip()
                for item in raw_baseline_seed_algorithms.split(",")
                if item.strip()
            )
        else:
            baseline_seed_algorithms = tuple(
                str(item).strip()
                for item in raw_baseline_seed_algorithms
                if str(item).strip()
            )
        baseline_seed_algorithms = tuple(dict.fromkeys(baseline_seed_algorithms))
        detail_limit = request.max_details or int(params.get("maximum_zones", 32))
        raw_complexity_axis = params.get(
            "complexity_axis",
            ComplexityAxis.ZONE_COUNT.value,
        )
        complexity_axis = (
            raw_complexity_axis
            if isinstance(raw_complexity_axis, ComplexityAxis)
            else ComplexityAxis(str(raw_complexity_axis))
        )

        if population_size < 4:
            raise ValueError("population_size должен быть не меньше 4")
        if generations < 1:
            raise ValueError("generations должен быть не меньше 1")
        if not 0.0 <= crossover_rate <= 1.0:
            raise ValueError("crossover_rate должен быть от 0 до 1")
        if not 0.0 <= mutation_rate <= 1.0:
            raise ValueError("mutation_rate должен быть от 0 до 1")
        if candidate_window < 1:
            raise ValueError("candidate_window должен быть не меньше 1")
        if candidate_trajectories < 1:
            raise ValueError("candidate_trajectories должен быть не меньше 1")
        if layer_bridge_span < 0:
            raise ValueError("layer_bridge_span не может быть отрицательным")
        if maximum_merge_reduction < 1:
            raise ValueError(
                "maximum_detail_reduction_per_merge должен быть не меньше 1"
            )
        if maximum_pool_merges < 1:
            raise ValueError("maximum_pool_merges должен быть не меньше 1")
        if candidate_expansion not in {"none", "layered"}:
            raise ValueError("candidate_expansion должен быть 'none' или 'layered'")
        if maximum_layer_variants < 1:
            raise ValueError("maximum_layer_variants должен быть не меньше 1")
        if local_search_passes < 0:
            raise ValueError("local_search_passes должен быть неотрицательным")
        if coverage_atoms not in {"whole_tiles", "demand_fragments"}:
            raise ValueError("coverage_atoms должен быть 'whole_tiles' или 'demand_fragments'")
        if pool_polish not in {"none", "milp"}:
            raise ValueError("pool_polish должен быть 'none' или 'milp'")
        if pool_polish_solves < 3 or not math.isfinite(pool_polish_time_s) or pool_polish_time_s <= 0:
            raise ValueError("pool_polish требует >= 3 solves и конечный положительный time_s")
        if recombination_variants < 0 or (recombination_variants and coverage_atoms != "demand_fragments"):
            raise ValueError("recombination_variants >= 0 требует coverage_atoms='demand_fragments'")
        unknown_baselines = tuple(
            name
            for name in baseline_seed_algorithms
            if name not in _BASELINE_SEED_OPTIMIZERS
        )
        if unknown_baselines:
            raise ValueError(
                "неизвестный baseline для seed генетического поиска: "
                f"{unknown_baselines[0]}"
            )
        if detail_limit < 1:
            raise ValueError("maximum_zones должен быть не меньше 1")
        operator_policy = build_operator_policy(
            operator_policy_name,
            MUTATION_OPERATORS,
            exploration=ucb_exploration,
        )

        if not demanded_cells(problem):
            evaluation = evaluate_layout(problem, (), request)
            return (
                LayoutSolution(
                    algorithm=self.name,
                    status=SolutionStatus.OPTIMAL,
                    zones=(),
                    metrics=evaluation.metrics,
                    request=request,
                    runtime_ms=(perf_counter() - started) * 1000.0,
                    diagnostics=evaluation.diagnostics,
                    meta={
                        "kind": "genetic_spatial_candidate_set",
                        "random_seed": random_seed,
                        "pareto_population": True,
                        "operator_learning": operator_policy.snapshot(),
                    },
                ),
            )

        deadline = (
            started + request.time_limit_s
            if request.time_limit_s is not None
            else None
        )
        rng = random.Random(random_seed)
        space = _build_search_space(
            problem,
            request,
            candidate_window=candidate_window,
            candidate_trajectories=candidate_trajectories,
            layer_bridge_span=layer_bridge_span,
            maximum_merge_reduction=maximum_merge_reduction,
            maximum_pool_merges=maximum_pool_merges,
            baseline_seed_algorithms=baseline_seed_algorithms,
            random_seed=random_seed,
            deadline=deadline,
            candidate_expansion=candidate_expansion,
            maximum_layer_variants=maximum_layer_variants,
            coverage_atoms=coverage_atoms,
        )
        guard_stages: list[dict[str, object]] = []
        guard_coverage: dict[str, object] = {}
        if self._candidate_guard is not None:
            space, stage = filter_candidate_space(problem, space, self._candidate_guard, stage="initial_candidate_pool")
            guard_stages.append(stage)
        if recombination_variants:
            before_recombination = space
            space = expand_recombined_space(
                problem, space, maximum_variants=recombination_variants, deadline=deadline,
            )
            if self._candidate_guard is not None:
                space = preserve_atom_boundaries(problem, before_recombination, space)
                space, stage = filter_candidate_space(problem, space, self._candidate_guard,
                                                     stage="after_geometric_recombination")
                guard_stages.append(stage)
        if self._candidate_guard is not None:
            guard_coverage = missing_atoms(space)
            if guard_coverage["missing_atom_count"]:
                evaluation = evaluate_layout(problem, (), request)
                return (LayoutSolution(
                    algorithm=self.name, status=SolutionStatus.INFEASIBLE, zones=(),
                    metrics=evaluation.metrics, request=request, runtime_ms=(perf_counter()-started)*1000,
                    diagnostics=(*evaluation.diagnostics,
                        "ERROR: после candidate_guard конечный пул не покрывает "
                        f"{guard_coverage['missing_atom_count']} исходных атомов спроса; "
                        "генетический поиск не запущен, исходные КЭ не удалены. "
                        "Это невозможность покрытия данным пулом, не всеми возможными раскладками."),
                    meta={"kind": "genetic_spatial_candidate_set", "pareto_population": True,
                        "random_seed": random_seed, "population_size": population_size,
                        "generations_requested": generations, "generations_completed": 0,
                        "candidate_pool_size": len(space.candidates), "coverage_atoms": coverage_atoms,
                        "coverage_atom_count": space.leaf_count, "operator_learning": operator_policy.snapshot(),
                        "candidate_guard": {"enabled": True, "stages": guard_stages,
                            "coverage": guard_coverage, "evolution_started": False,
                            "source_demand_removed": False, "source_demand_values_changed": False,
                            "global_infeasibility_claimed": False,
                            "final_materialization": {"checked_zone_count": 0, "passed": False,
                                "reason": "no_full_coverage_candidate_pool"}}},
                ),)
        population, history, evolution_timed_out, operator_learning = _evolve(
            space,
            detail_limit=detail_limit,
            population_size=population_size,
            generations=generations,
            crossover_rate=crossover_rate,
            mutation_rate=mutation_rate,
            complexity_axis=complexity_axis,
            operator_policy=operator_policy,
            rng=rng,
            deadline=deadline,
        )
        local_search_log: dict[frozenset[int], dict[str, object]] = {}
        local_totals = {"candidate_checks": 0, "moves": 0, "improved_genomes": 0}
        if local_search_passes:
            improved = []
            for parent in population:
                result = improve_genome(
                    space, parent.genome, complexity_axis=complexity_axis,
                    maximum_passes=local_search_passes, deadline=deadline,
                )
                local_totals["candidate_checks"] += result.candidate_checks
                local_totals["moves"] += result.moves
                evolution_timed_out = evolution_timed_out or result.stopped_by_time_limit
                if result.genome != parent.genome:
                    improved.append(_individual(space, result.genome, complexity_axis))
                    local_totals["improved_genomes"] += 1
                    local_search_log.setdefault(result.genome, {
                        "parent_genome": tuple(sorted(parent.genome)),
                        "moves": result.moves, "candidate_checks": result.candidate_checks,
                    })
            # Не отсекать родителей до hard-validation улучшенных предложений.
            population = _unique_individuals([*population, *improved])
        pool_polish_telemetry: dict[str, object] = {}
        if pool_polish == "milp":
            polished = polish_candidate_pool(
                space, complexity_axis=complexity_axis, maximum_zones=detail_limit,
                maximum_solves=pool_polish_solves, time_limit_s=pool_polish_time_s, deadline=deadline,
            )
            pool_polish_telemetry = polished.telemetry
            evolution_timed_out = evolution_timed_out or polished.stopped_by_time_limit
            # Родители и предложения объединяются без отсечения до hard-validation.
            population = _unique_individuals((*population, *tuple(
                _individual(space, genome, complexity_axis) for genome in polished.genomes
            )))
        approximate_front = tuple(sorted(population, key=lambda item: (
            item.complexity, item.zone_count, -item.mass_kg, tuple(sorted(item.genome)),
        )))
        context = prepare_detailing(problem)
        solutions: list[LayoutSolution] = []
        archive_validation = {"checked": 0, "rejected": 0, "maximum_mass_error_kg": 0.0}
        guard_archive = {"checked_solutions": 0, "rejected_solutions": 0, "rejected_candidates": []}
        for candidate_number, individual in enumerate(approximate_front, 1):
            exact_baseline_seed = individual.genome in space.baseline_seed_genomes
            zones, phase_diagnostic = _materialize_genome(
                problem, space, individual.genome, context,
            )
            final_guard = (check_materialized_zones(problem, zones, self._candidate_guard)
                           if self._candidate_guard is not None else None)
            guard_passed = final_guard is None or final_guard["passed"]
            if final_guard is not None:
                guard_archive["checked_solutions"] += 1
                if not guard_passed:
                    guard_archive["rejected_solutions"] += 1
                    guard_archive["rejected_candidates"].append({
                        "candidate_number": candidate_number,
                        "genome_candidate_indexes": sorted(individual.genome),
                        "final_check": final_guard,
                    })
            evaluation = evaluate_layout(problem, zones, request)
            archive_validation["checked"] += 1
            archive_validation["rejected"] += int(not evaluation.valid or not guard_passed)
            archive_validation["maximum_mass_error_kg"] = max(
                archive_validation["maximum_mass_error_kg"],
                abs(evaluation.metrics.total_mass_kg - individual.mass_kg),
            )
            timed_out = space.stopped_by_time_limit or evolution_timed_out
            diagnostics = evaluation.diagnostics
            if phase_diagnostic is not None:
                diagnostics = (*diagnostics, phase_diagnostic)
            if not guard_passed:
                diagnostics = (*diagnostics,
                    "ERROR: candidate_guard отклонил итоговую геометрию после материализации/фаз: "
                    + ", ".join(item["zone_id"] for item in final_guard["rejected_zones"]))
            if timed_out:
                diagnostics = (
                    *diagnostics,
                    "WARNING: генетический поиск остановлен по лимиту времени; "
                    "показан найденный допустимый фронт",
                )
            solutions.append(
                LayoutSolution(
                    algorithm=self.name,
                    status=(
                        SolutionStatus.FEASIBLE
                        if evaluation.valid and guard_passed
                        else SolutionStatus.ERROR
                    ),
                    zones=zones,
                    metrics=evaluation.metrics,
                    request=request,
                    runtime_ms=(perf_counter() - started) * 1000.0,
                    diagnostics=diagnostics,
                    meta={
                        "kind": "genetic_spatial_candidate_set",
                        "pareto_population": True,
                        "candidate_number": candidate_number,
                        "random_seed": random_seed,
                        "population_size": population_size,
                        "generations_requested": generations,
                        "generations_completed": len(history),
                        "candidate_pool_size": len(space.candidates),
                        "candidate_expansion": candidate_expansion,
                        "maximum_layer_variants": maximum_layer_variants,
                        "local_search_passes": local_search_passes,
                        "coverage_atoms": coverage_atoms,
                        "coverage_atom_count": space.leaf_count,
                        "pool_polish": pool_polish_telemetry,
                        "recombination_variants": recombination_variants,
                        "local_search_totals": local_totals,
                        "local_search": local_search_log.get(individual.genome),
                        "archive_validation": archive_validation,
                        "candidate_pool_origin_counts": {
                            origin: sum(
                                origin in candidate.origins
                                for candidate in space.candidates
                            )
                            for origin in sorted(
                                {
                                    origin
                                    for candidate in space.candidates
                                    for origin in candidate.origins
                                }
                            )
                        },
                        "layer_bridge_span": layer_bridge_span,
                        "initial_rectangle_count": space.initial_rectangle_count,
                        "baseline_seed_algorithms_requested": baseline_seed_algorithms,
                        "baseline_seed_algorithms": space.baseline_seed_algorithms,
                        "baseline_seed_count": len(space.baseline_seed_genomes),
                        "baseline_seed_metrics": space.baseline_seed_metrics,
                        "exact_baseline_seed": exact_baseline_seed,
                        "trajectory_count": space.trajectory_count,
                        "trajectory_state_count": space.trajectory_state_count,
                        "genome_candidate_indexes": sorted(individual.genome),
                        "search_history": history,
                        "approximate_mass_kg": individual.mass_kg,
                        "approximate_zone_count": individual.zone_count,
                        "approximate_complexity": individual.complexity,
                        "complexity_axis": complexity_axis.value,
                        "mutation_operators": MUTATION_OPERATORS,
                        "operator_learning": operator_learning,
                        **({"candidate_guard": {"enabled": True, "stages": guard_stages,
                            "coverage": guard_coverage, "evolution_started": True,
                            "source_demand_removed": False, "source_demand_values_changed": False,
                            "global_infeasibility_claimed": False,
                            "archive_materialization": guard_archive,
                            "final_materialization": final_guard}}
                           if self._candidate_guard is not None else {}),
                    },
                )
            )
        valid_solutions = tuple(
            solution
            for solution in solutions
            if solution.status in {SolutionStatus.FEASIBLE, SolutionStatus.OPTIMAL}
            and solution.metrics.under_reinforced_cell_count == 0
        )
        if not valid_solutions:
            return tuple(solutions)

        def actual_complexity(solution: LayoutSolution) -> int:
            if complexity_axis is ComplexityAxis.ZONE_COUNT:
                return solution.metrics.detail_count
            if complexity_axis is ComplexityAxis.POSITION_COUNT:
                return len(zone_position_keys(solution.zones))
            return solution.metrics.physical_bar_count

        def solution_dominates(
            first: LayoutSolution,
            second: LayoutSolution,
        ) -> bool:
            if complexity_axis is ComplexityAxis.POSITION_COUNT:
                # Сохраняем альтернативную номенклатуру для объединения всей плиты.
                first_keys, second_keys = zone_position_keys(first.zones), zone_position_keys(second.zones)
                return (first_keys <= second_keys
                        and first.metrics.total_mass_kg <= second.metrics.total_mass_kg + _MASS_TOLERANCE_KG
                        and (first_keys < second_keys or first.metrics.total_mass_kg
                             < second.metrics.total_mass_kg - _MASS_TOLERANCE_KG))
            first_complexity = actual_complexity(first)
            second_complexity = actual_complexity(second)
            return (
                first.metrics.total_mass_kg
                <= second.metrics.total_mass_kg + _MASS_TOLERANCE_KG
                and first_complexity <= second_complexity
                and (
                    first.metrics.total_mass_kg
                    < second.metrics.total_mass_kg - _MASS_TOLERANCE_KG
                    or first_complexity < second_complexity
                )
            )

        unique_by_point: dict[tuple, LayoutSolution] = {}
        for solution in valid_solutions:
            point = (
                tuple(sorted(zone_position_keys(solution.zones)))
                if complexity_axis is ComplexityAxis.POSITION_COUNT else actual_complexity(solution),
                round(solution.metrics.total_mass_kg, 6),
            )
            unique_by_point.setdefault(point, solution)
        unique = tuple(unique_by_point.values())
        actual_front = tuple(
            solution
            for solution in unique
            if not any(
                solution_dominates(other, solution)
                for other in unique
                if other is not solution
            )
        )
        hard_rejection_count = len(solutions) - len(valid_solutions)
        return tuple(
            replace(
                solution,
                meta={
                    **solution.meta,
                    "internal_hard_rejection_count": hard_rejection_count,
                },
            )
            for solution in sorted(
                actual_front,
                key=lambda item: (
                    actual_complexity(item),
                    -item.metrics.total_mass_kg,
                    item.metrics.detail_count,
                ),
            )
        )

    def solve(
        self,
        problem: LayoutProblem,
        request: AlgorithmRequest | None = None,
    ) -> LayoutSolution:
        request = request or AlgorithmRequest()
        candidates = self.solve_many(problem, request)
        return min(
            candidates,
            key=lambda solution: (
                solution.metrics.objective_value,
                solution.metrics.detail_count,
                solution.metrics.total_mass_kg,
            ),
        )
