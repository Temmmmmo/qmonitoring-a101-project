"""Ограниченное детерминированное улучшение генома внутри существующего пула."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING

from ...contracts import ComplexityAxis

if TYPE_CHECKING:
    from ..genetic_pareto import _SearchSpace


@dataclass(frozen=True)
class LocalSearchResult:
    genome: frozenset[int]
    moves: int
    candidate_checks: int
    stopped_by_time_limit: bool


def improve_genome(
    space: _SearchSpace,
    genome: frozenset[int],
    *,
    complexity_axis: ComplexityAxis,
    maximum_passes: int,
    deadline: float | None = None,
) -> LocalSearchResult:
    """До maximum_passes замен: одна зона вместо покрываемых ею выбранных зон.

    Каждый шаг не увеличивает массу и выбранную ось сложности, улучшая хотя бы одну.
    Покрытие атомов сохраняется по включению множеств. Это только proposal: результат
    обязан пройти обычную детализацию/hard-валидатор; исходный кандидат сохраняется.
    """

    if maximum_passes < 0:
        raise ValueError("maximum_passes должен быть неотрицательным")
    if not isinstance(complexity_axis, ComplexityAxis):
        raise ValueError("неподдерживаемая ось local search")
    if any(index < 0 or index >= len(space.candidates) for index in genome):
        raise ValueError("индекс генома отсутствует в CandidateSet")
    selected = set(genome)
    moves = checks = 0
    stopped = False
    for _pass in range(maximum_passes):
        choices = []
        selected_indexes = tuple(sorted(selected))
        for candidate_index, candidate in enumerate(space.candidates):
            if deadline is not None and perf_counter() >= deadline:
                stopped = True
                break
            if candidate_index in selected or not candidate.leaf_ids:
                continue
            checks += 1
            removable = tuple(
                index for index in selected_indexes
                if space.candidates[index].leaf_ids
                and space.candidates[index].leaf_ids <= candidate.leaf_ids
            )
            if not removable:
                continue
            mass_delta = candidate.mass_kg - sum(space.candidates[index].mass_kg for index in removable)
            complexity_delta = 1 - len(removable)
            if complexity_axis is ComplexityAxis.PHYSICAL_BAR_COUNT:
                complexity_delta = candidate.rectangle.zone.bar_count - sum(
                    space.candidates[index].rectangle.zone.bar_count for index in removable
                )
            if mass_delta > 1e-6 or complexity_delta > 0:
                continue
            if mass_delta >= -1e-6 and complexity_delta == 0:
                continue
            choices.append((mass_delta, complexity_delta, candidate_index, removable))
        if stopped or not choices:
            break
        _mass_delta, _complexity_delta, added, removed = min(choices)
        selected.difference_update(removed)
        selected.add(added)
        moves += 1
    return LocalSearchResult(frozenset(selected), moves, checks, stopped)
