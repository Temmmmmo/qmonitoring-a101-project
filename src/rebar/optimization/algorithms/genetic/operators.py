"""Предметные операции над геномом прямоугольного покрытия."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol

MUTATION_OPERATORS = (
    "split",
    "merge",
    "shift",
    "change-level",
    "baseline-patch",
)


class _RectangleLike(Protocol):
    row_start: int
    row_end: int
    column_start: int
    column_end: int
    level_index: int


class _CandidateLike(Protocol):
    rectangle: _RectangleLike
    leaf_ids: frozenset[int]

    @property
    def mass_kg(self) -> float: ...


class GeneticSearchSpace(Protocol):
    candidates: tuple[_CandidateLike, ...]
    baseline_seed_genomes: tuple[frozenset[int], ...]


@dataclass(frozen=True)
class MutationOutcome:
    """Результат одной именованной мутации до общего repair."""

    genome: frozenset[int]
    operator: str


def _bounds(candidate: _CandidateLike) -> tuple[int, int, int, int]:
    rectangle = candidate.rectangle
    return (
        rectangle.row_start,
        rectangle.row_end,
        rectangle.column_start,
        rectangle.column_end,
    )


def _available_operators(
    space: GeneticSearchSpace,
    selected: set[int],
) -> tuple[str, ...]:
    candidates = space.candidates
    available: list[str] = []
    if any(
        other.leaf_ids < candidates[index].leaf_ids
        for index in selected
        for other_index, other in enumerate(candidates)
        if other_index not in selected
    ):
        available.append("split")
    if any(
        sum(
            candidates[index].leaf_ids <= candidate.leaf_ids
            for index in selected
        )
        >= 2
        for candidate_index, candidate in enumerate(candidates)
        if candidate_index not in selected
    ):
        available.append("merge")
    if any(
        candidate_index not in selected
        and candidate.rectangle.level_index
        == candidates[selected_index].rectangle.level_index
        and candidate.leaf_ids & candidates[selected_index].leaf_ids
        and _bounds(candidate) != _bounds(candidates[selected_index])
        for selected_index in selected
        for candidate_index, candidate in enumerate(candidates)
    ):
        available.append("shift")
    if any(
        candidate_index not in selected
        and _bounds(candidate) == _bounds(candidates[selected_index])
        and candidate.rectangle.level_index
        != candidates[selected_index].rectangle.level_index
        for selected_index in selected
        for candidate_index, candidate in enumerate(candidates)
    ):
        available.append("change-level")
    if space.baseline_seed_genomes:
        available.append("baseline-patch")
    return tuple(available)


def _split(
    space: GeneticSearchSpace,
    selected: set[int],
    rng: random.Random,
) -> None:
    splittable = tuple(
        index
        for index in sorted(selected)
        if any(
            candidate_index not in selected
            and candidate.leaf_ids < space.candidates[index].leaf_ids
            for candidate_index, candidate in enumerate(space.candidates)
        )
    )
    removed = splittable[rng.randrange(len(splittable))]
    selected.remove(removed)
    uncovered = set(space.candidates[removed].leaf_ids)
    children = sorted(
        (
            index
            for index, candidate in enumerate(space.candidates)
            if index not in selected
            and candidate.leaf_ids < space.candidates[removed].leaf_ids
        ),
        key=lambda index: (
            space.candidates[index].mass_kg
            / max(1, len(space.candidates[index].leaf_ids & uncovered)),
            -len(space.candidates[index].leaf_ids),
            index,
        ),
    )
    while uncovered and children:
        useful = tuple(
            index
            for index in children
            if space.candidates[index].leaf_ids & uncovered
        )
        if not useful:
            break
        window = useful[: min(4, len(useful))]
        added = window[rng.randrange(len(window))]
        selected.add(added)
        uncovered.difference_update(space.candidates[added].leaf_ids)
        children.remove(added)


def _merge(
    space: GeneticSearchSpace,
    selected: set[int],
    rng: random.Random,
) -> None:
    ranked: list[tuple[float, int, int, tuple[int, ...]]] = []
    for candidate_index, candidate in enumerate(space.candidates):
        if candidate_index in selected:
            continue
        removable = tuple(
            index
            for index in sorted(selected)
            if space.candidates[index].leaf_ids <= candidate.leaf_ids
        )
        reduction = len(removable) - 1
        if reduction <= 0:
            continue
        mass_delta = candidate.mass_kg - sum(
            space.candidates[index].mass_kg for index in removable
        )
        ranked.append((mass_delta / reduction, -reduction, candidate_index, removable))
    ranked.sort()
    chosen = ranked[rng.randrange(min(6, len(ranked)))]
    selected.difference_update(chosen[3])
    selected.add(chosen[2])


def _shift(
    space: GeneticSearchSpace,
    selected: set[int],
    rng: random.Random,
) -> None:
    moves: list[tuple[float, float, int, int]] = []
    for removed in sorted(selected):
        source = space.candidates[removed]
        for candidate_index, candidate in enumerate(space.candidates):
            if candidate_index in selected:
                continue
            if candidate.rectangle.level_index != source.rectangle.level_index:
                continue
            overlap = len(candidate.leaf_ids & source.leaf_ids)
            union = len(candidate.leaf_ids | source.leaf_ids)
            if overlap == 0 or _bounds(candidate) == _bounds(source):
                continue
            jaccard = overlap / union
            moves.append(
                (-jaccard, abs(candidate.mass_kg - source.mass_kg), removed, candidate_index)
            )
    moves.sort()
    _jaccard, _mass_delta, removed, added = moves[rng.randrange(min(8, len(moves)))]
    selected.remove(removed)
    selected.add(added)


def _change_level(
    space: GeneticSearchSpace,
    selected: set[int],
    rng: random.Random,
) -> None:
    replacements = tuple(
        (selected_index, candidate_index)
        for selected_index in sorted(selected)
        for candidate_index, candidate in enumerate(space.candidates)
        if candidate_index not in selected
        and _bounds(candidate) == _bounds(space.candidates[selected_index])
        and candidate.rectangle.level_index
        != space.candidates[selected_index].rectangle.level_index
    )
    removed, added = replacements[rng.randrange(len(replacements))]
    selected.remove(removed)
    selected.add(added)


def _baseline_patch(
    space: GeneticSearchSpace,
    selected: set[int],
    rng: random.Random,
) -> None:
    baseline = space.baseline_seed_genomes[
        rng.randrange(len(space.baseline_seed_genomes))
    ]
    patch_size = min(len(baseline), 1 + rng.randrange(3))
    patch = set(rng.sample(tuple(sorted(baseline)), patch_size))
    covered = frozenset(
        leaf_id
        for candidate_index in patch
        for leaf_id in space.candidates[candidate_index].leaf_ids
    )
    selected.difference_update(
        index
        for index in tuple(selected)
        if space.candidates[index].leaf_ids <= covered
    )
    selected.update(patch)


def mutate_genome(
    space: GeneticSearchSpace,
    genome: frozenset[int],
    operator: str,
    rng: random.Random,
) -> MutationOutcome:
    """Применить выбранный оператор; инженерную допустимость восстановит общий repair."""

    selected = set(genome)
    available = _available_operators(space, selected)
    if not available:
        return MutationOutcome(genome, operator)
    if operator not in available:
        operator = available[rng.randrange(len(available))]
    if operator == "split":
        _split(space, selected, rng)
    elif operator == "merge":
        _merge(space, selected, rng)
    elif operator == "shift":
        _shift(space, selected, rng)
    elif operator == "change-level":
        _change_level(space, selected, rng)
    elif operator == "baseline-patch":
        _baseline_patch(space, selected, rng)
    else:
        raise ValueError(f"неизвестный генетический оператор: {operator}")
    return MutationOutcome(frozenset(selected), operator)


def available_operators(
    space: GeneticSearchSpace,
    genome: frozenset[int],
) -> tuple[str, ...]:
    """Вернуть операции, которые могут изменить данный геном до repair."""

    return _available_operators(space, set(genome))
