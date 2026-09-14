"""Объединение неаддитивной номенклатуры без ошибочного pruning по сумме счётчиков."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import TypeVar

from ..contracts.front import DirectionCandidate
from .bar_schedule import PositionKey

Candidate = TypeVar("Candidate")


def combine_keyed_candidates(
    groups: tuple[tuple[Candidate, ...], ...], *,
    keys_of: Callable[[Candidate], frozenset[PositionKey]],
    mass_of: Callable[[Candidate], float], maximum_states: int = 100_000,
) -> tuple[tuple[Candidate, ...], ...]:
    """Общий DP для старого и составного контракта; разные множества не отсекаются."""
    if isinstance(maximum_states, bool) or not isinstance(maximum_states, int) or maximum_states < 1:
        raise ValueError("maximum_states должен быть положительным целым")
    states: dict[frozenset[PositionKey], tuple[float, tuple[Candidate, ...]]] = {frozenset(): (0.0, ())}
    for group in groups:
        next_states = {}
        for keys, (mass, partial) in states.items():
            for candidate in group:
                candidate_mass = mass_of(candidate)
                if not math.isfinite(candidate_mass) or candidate_mass < 0:
                    raise ValueError("масса кандидата должна быть конечной неотрицательной")
                union = keys | keys_of(candidate)
                combined_mass = math.fsum((mass, candidate_mass))
                previous = next_states.get(union)
                if previous is None or combined_mass < previous[0]:
                    next_states[union] = (combined_mass, (*partial, candidate))
                if len(next_states) > maximum_states:
                    raise ValueError(f"Общеплитная номенклатура: превышен лимит {maximum_states} состояний; "
                                     "неполный фронт не выдан за полный.")
        states = next_states
    return tuple(value[1] for _, value in sorted(states.items(), key=lambda item: (
        len(item[0]), item[1][0], tuple(sorted(item[0])),
    )))


def combine_position_candidates(
    groups: tuple[tuple[DirectionCandidate, ...], ...], *, maximum_states: int = 100_000,
) -> tuple[tuple[DirectionCandidate, ...], ...]:
    """Хранить минимальную массу для каждого множества типоразмеров.

    Разные множества одинакового размера НЕ эквивалентны: следующее направление
    может повторить одно из них. Числовой Pareto применяется только после всей плиты.
    Лимит защищает память; его достижение даёт ошибку, не усечённый «полный фронт».
    """
    def keys(candidate):
        result = frozenset(candidate.constructability.bar_position_keys)
        if len(result) != candidate.constructability.position_count:
            raise ValueError("счётчик позиций не соответствует ключам номенклатуры")
        return result

    return combine_keyed_candidates(groups, keys_of=keys,
                                    mass_of=lambda candidate: candidate.solution.metrics.total_mass_kg,
                                    maximum_states=maximum_states)
