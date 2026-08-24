"""Воспроизводимый многокритериальный генетический поиск раскладок.

Первая версия использует пространственный CandidateSet: один проход строит допустимые
прямоугольники из регулярного разбиения, а хромосома выбирает их подмножество. Repair
гарантирует покрытие всех исходных атомов перед независимой инженерной проверкой.
NSGA-II-подобный отбор оптимизирует массу и число зон без свёртки в один score.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from time import perf_counter

from ..contracts import AlgorithmRequest, LayoutProblem, LayoutSolution, SolutionStatus
from ..services import demanded_cells, evaluate_layout, prepare_detailing, resolve_zone_phases
from .spatial_partition_greedy import (
    _Grid,
    _Rectangle,
    _build_grid,
    _candidate,
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

    @property
    def mass_kg(self) -> float:
        return self.rectangle.zone.mass_kg


@dataclass(frozen=True)
class _SearchSpace:
    grid: _Grid
    candidates: tuple[_PoolCandidate, ...]
    seed_genomes: tuple[frozenset[int], ...]
    leaf_count: int
    initial_rectangle_count: int
    trajectory_count: int
    stopped_by_time_limit: bool


@dataclass(frozen=True)
class _Individual:
    genome: frozenset[int]
    mass_kg: float

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


def _build_search_space(
    problem: LayoutProblem,
    request: AlgorithmRequest,
    *,
    candidate_window: int,
    maximum_merge_reduction: int,
    maximum_pool_merges: int,
    deadline: float | None,
) -> _SearchSpace:
    """Собрать прямоугольный CandidateSet за один пространственный проход."""

    grid = _build_grid(problem, request)
    context = prepare_detailing(problem)
    initial = _initial_rectangles(problem, grid, context)
    initial_by_leaf = {leaf_id: rectangle for leaf_id, rectangle in enumerate(initial)}

    rectangles = list(initial)
    leaves_by_key: dict[int, frozenset[int]] = {
        rectangle.key: frozenset((leaf_id,))
        for leaf_id, rectangle in enumerate(initial)
    }
    pool_rectangles: dict[tuple[int, int, int, int, int], _Rectangle] = {}
    pool_leaves: dict[tuple[int, int, int, int, int], set[int]] = {}
    trajectory: list[tuple[tuple[int, int, int, int, int], ...]] = []

    def add_to_pool(rectangle: _Rectangle, leaf_ids: frozenset[int]) -> None:
        signature = _rectangle_signature(rectangle)
        pool_rectangles.setdefault(signature, rectangle)
        pool_leaves.setdefault(signature, set()).update(leaf_ids)

    def record_state() -> None:
        trajectory.append(tuple(_rectangle_signature(item) for item in rectangles))

    for leaf_id, rectangle in enumerate(initial):
        add_to_pool(rectangle, frozenset((leaf_id,)))
    record_state()

    next_key = len(rectangles)
    merge_count = 0
    stopped_by_time_limit = False
    while len(rectangles) > 1 and merge_count < maximum_pool_merges:
        if deadline is not None and perf_counter() >= deadline:
            stopped_by_time_limit = True
            break

        merge_candidates = []
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
            if candidate is None or candidate.detail_reduction > maximum_merge_reduction:
                continue
            merge_candidates.append(candidate)
        if not merge_candidates:
            break

        merge_candidates.sort(
            key=lambda item: (
                item.objective_delta / item.detail_reduction,
                item.objective_delta,
                -item.detail_reduction,
                _rectangle_signature(item.rectangle),
                item.absorbed_keys,
            )
        )
        for alternative in merge_candidates[:candidate_window]:
            alternative_leaves = frozenset(
                leaf_id
                for key in alternative.absorbed_keys
                for leaf_id in leaves_by_key[key]
            )
            add_to_pool(alternative.rectangle, alternative_leaves)

        best = merge_candidates[0]
        absorbed = set(best.absorbed_keys)
        best_leaves = frozenset(
            leaf_id for key in best.absorbed_keys for leaf_id in leaves_by_key[key]
        )
        rectangles = [item for item in rectangles if item.key not in absorbed]
        rectangles.append(best.rectangle)
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
        leaves_by_key[best.rectangle.key] = best_leaves
        add_to_pool(best.rectangle, best_leaves)
        record_state()
        next_key += 1
        merge_count += 1

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
                    for cell_id in initial_by_leaf[leaf_id].source_cell_ids
                }
            )
        )
        pool.append(
            _PoolCandidate(
                rectangle=pool_rectangles[signature],
                leaf_ids=leaf_ids,
                source_cell_ids=source_ids,
            )
        )

    seed_genomes = tuple(
        dict.fromkeys(
            frozenset(index_by_signature[signature] for signature in state)
            for state in trajectory
        )
    )
    return _SearchSpace(
        grid=grid,
        candidates=tuple(pool),
        seed_genomes=seed_genomes,
        leaf_count=len(initial),
        initial_rectangle_count=len(initial),
        trajectory_count=len(trajectory),
        stopped_by_time_limit=stopped_by_time_limit,
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


def _individual(space: _SearchSpace, genome: frozenset[int]) -> _Individual:
    return _Individual(
        genome=genome,
        mass_kg=sum(space.candidates[index].mass_kg for index in genome),
    )


def _dominates(first: _Individual, second: _Individual) -> bool:
    no_worse = (
        first.mass_kg <= second.mass_kg + _MASS_TOLERANCE_KG
        and first.zone_count <= second.zone_count
    )
    strictly_better = (
        first.mass_kg < second.mass_kg - _MASS_TOLERANCE_KG
        or first.zone_count < second.zone_count
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
        lambda item: float(item.zone_count),
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


def _mutate(
    space: _SearchSpace,
    genome: frozenset[int],
    rng: random.Random,
) -> frozenset[int]:
    selected = set(genome)
    operation = rng.randrange(3)
    if operation == 0 and selected:
        selected.remove(rng.choice(tuple(sorted(selected))))
    else:
        available = tuple(index for index in range(len(space.candidates)) if index not in selected)
        if available:
            added = rng.choice(available)
            selected.add(added)
            if operation == 2:
                covered = space.candidates[added].leaf_ids
                removable = [
                    index
                    for index in selected
                    if index != added and space.candidates[index].leaf_ids <= covered
                ]
                if removable:
                    selected.remove(rng.choice(removable))
    return frozenset(selected)


def _sample_seed_genomes(
    space: _SearchSpace,
    detail_limit: int,
    population_size: int,
) -> list[frozenset[int]]:
    eligible = [
        genome for genome in space.seed_genomes if len(genome) <= detail_limit
    ]
    if not eligible:
        eligible = list(space.seed_genomes[-1:])
    by_count = {len(genome): genome for genome in eligible}
    ordered = [by_count[count] for count in sorted(by_count)]
    if len(ordered) <= population_size:
        return ordered
    indexes = {
        round(index * (len(ordered) - 1) / (population_size - 1))
        for index in range(population_size)
    }
    return [ordered[index] for index in sorted(indexes)]


def _evolve(
    space: _SearchSpace,
    *,
    detail_limit: int,
    population_size: int,
    generations: int,
    crossover_rate: float,
    mutation_rate: float,
    rng: random.Random,
    deadline: float | None,
) -> tuple[tuple[_Individual, ...], tuple[dict[str, int], ...], bool]:
    seed_genomes = _sample_seed_genomes(space, detail_limit, population_size)
    population = [
        _individual(
            space,
            _repair(space, genome, detail_limit=detail_limit, rng=rng),
        )
        for genome in seed_genomes
    ]
    attempts = 0
    while len(_unique_individuals(population)) < population_size and attempts < population_size * 20:
        base = population[rng.randrange(len(population))].genome
        mutated = _mutate(space, base, rng)
        repaired = _repair(space, mutated, detail_limit=detail_limit, rng=rng)
        population.append(_individual(space, repaired))
        attempts += 1
    population_tuple = _select_population(population, population_size)
    history: list[dict[str, int]] = []
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
            if rng.random() < mutation_rate:
                genome = _mutate(space, genome, rng)
            repaired = _repair(space, genome, detail_limit=detail_limit, rng=rng)
            offspring.append(_individual(space, repaired))
        population_tuple = _select_population(
            [*population_tuple, *offspring],
            population_size,
        )
        first_front = _non_dominated_fronts(population_tuple)[0]
        history.append(
            {
                "generation": generation + 1,
                "population_size": len(population_tuple),
                "front_size": len(first_front),
            }
        )

    return population_tuple, tuple(history), stopped_by_time_limit


class GeneticParetoOptimizer:
    """Искать приближённый Парето-фронт по массе и числу прямоугольных зон."""

    name = "genetic-pareto"

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
        maximum_merge_reduction = int(
            params.get("maximum_detail_reduction_per_merge", 4)
        )
        maximum_pool_merges = int(params.get("maximum_pool_merges", 5_000))
        detail_limit = request.max_details or int(params.get("maximum_zones", 32))

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
        if maximum_merge_reduction < 1:
            raise ValueError(
                "maximum_detail_reduction_per_merge должен быть не меньше 1"
            )
        if maximum_pool_merges < 1:
            raise ValueError("maximum_pool_merges должен быть не меньше 1")
        if detail_limit < 1:
            raise ValueError("maximum_zones должен быть не меньше 1")

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
            maximum_merge_reduction=maximum_merge_reduction,
            maximum_pool_merges=maximum_pool_merges,
            deadline=deadline,
        )
        population, history, evolution_timed_out = _evolve(
            space,
            detail_limit=detail_limit,
            population_size=population_size,
            generations=generations,
            crossover_rate=crossover_rate,
            mutation_rate=mutation_rate,
            rng=rng,
            deadline=deadline,
        )
        approximate_front = _non_dominated_fronts(population)[0]
        approximate_front = tuple(
            sorted(
                approximate_front,
                key=lambda item: (
                    item.zone_count,
                    -item.mass_kg,
                    tuple(sorted(item.genome)),
                ),
            )
        )
        context = prepare_detailing(problem)
        solutions: list[LayoutSolution] = []
        for candidate_number, individual in enumerate(approximate_front, 1):
            rectangles = [
                _make_rectangle(
                    problem,
                    space.grid,
                    context,
                    key=index + 1,
                    row_start=space.candidates[candidate_index].rectangle.row_start,
                    row_end=space.candidates[candidate_index].rectangle.row_end,
                    column_start=space.candidates[candidate_index].rectangle.column_start,
                    column_end=space.candidates[candidate_index].rectangle.column_end,
                    level_index=space.candidates[candidate_index].rectangle.level_index,
                    source_cell_ids=space.candidates[candidate_index].source_cell_ids,
                    collect_coverage=True,
                )
                for index, candidate_index in enumerate(sorted(individual.genome))
            ]
            phase_diagnostic: str | None = None
            raw_zones = tuple(rectangle.zone for rectangle in rectangles)
            try:
                zones = tuple(
                    resolve_zone_phases(
                        problem,
                        raw_zones,
                        context=context,
                    )
                )
            except ValueError as error:
                # Индивидуально построенные зоны остаются геометрически допустимыми;
                # общий валидатор ниже независимо проверит их оси и коллизии.
                zones = raw_zones
                phase_diagnostic = (
                    "WARNING: repair поперечных фаз не нашёл совместный вариант: "
                    f"{error}"
                )
            evaluation = evaluate_layout(problem, zones, request)
            timed_out = space.stopped_by_time_limit or evolution_timed_out
            diagnostics = evaluation.diagnostics
            if phase_diagnostic is not None:
                diagnostics = (*diagnostics, phase_diagnostic)
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
                        if evaluation.valid
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
                        "initial_rectangle_count": space.initial_rectangle_count,
                        "trajectory_count": space.trajectory_count,
                        "genome_candidate_indexes": sorted(individual.genome),
                        "search_history": history,
                        "approximate_mass_kg": individual.mass_kg,
                        "approximate_zone_count": individual.zone_count,
                    },
                )
            )
        return tuple(solutions)

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
