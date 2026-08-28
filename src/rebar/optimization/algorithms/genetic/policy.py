"""Воспроизводимая политика выбора генетических операторов.

UCB1 здесь является online hyper-heuristic: она не создаёт геометрию и не меняет
инженерные правила, а только распределяет вычислительный бюджет между фиксированными
операторами по наблюдаемому улучшению Парето-целей.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Protocol


@dataclass
class _OperatorStat:
    selections: int = 0
    reward_sum: float = 0.0
    positive_rewards: int = 0

    @property
    def mean_reward(self) -> float:
        return 0.0 if self.selections == 0 else self.reward_sum / self.selections


class OperatorPolicy(Protocol):
    """Выбрать оператор и принять ограниченную обратную связь о его результате."""

    name: str

    def choose(self, available: tuple[str, ...], rng: random.Random) -> str:
        """Вернуть имя одного доступного оператора."""

    def update(self, operator: str, reward: float) -> None:
        """Учесть reward в диапазоне ``[-1, 1]``."""

    def snapshot(self) -> dict[str, object]:
        """Вернуть JSON-совместимую телеметрию политики."""


class _TrackedPolicy:
    name = "tracked"

    def __init__(self, operators: tuple[str, ...]) -> None:
        if not operators:
            raise ValueError("политике нужен хотя бы один оператор")
        if len(set(operators)) != len(operators):
            raise ValueError("имена операторов должны быть уникальными")
        self._operators = operators
        self._stats = {operator: _OperatorStat() for operator in operators}

    def _validate_available(self, available: tuple[str, ...]) -> None:
        if not available:
            raise ValueError("нет доступных генетических операторов")
        unknown = tuple(operator for operator in available if operator not in self._stats)
        if unknown:
            raise ValueError(f"неизвестный генетический оператор: {unknown[0]}")

    def update(self, operator: str, reward: float) -> None:
        if operator not in self._stats:
            raise ValueError(f"неизвестный генетический оператор: {operator}")
        if not math.isfinite(reward):
            raise ValueError("reward оператора должен быть конечным числом")
        bounded = max(-1.0, min(1.0, reward))
        stat = self._stats[operator]
        stat.selections += 1
        stat.reward_sum += bounded
        if bounded > 0.0:
            stat.positive_rewards += 1

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self.name,
            "total_selections": sum(stat.selections for stat in self._stats.values()),
            "operators": [
                {
                    "name": operator,
                    "selections": stat.selections,
                    "reward_sum": stat.reward_sum,
                    "mean_reward": stat.mean_reward,
                    "positive_rewards": stat.positive_rewards,
                }
                for operator, stat in self._stats.items()
            ],
        }


class UniformOperatorPolicy(_TrackedPolicy):
    """Контрольная политика: равновероятно выбирать доступную мутацию."""

    name = "uniform"

    def choose(self, available: tuple[str, ...], rng: random.Random) -> str:
        self._validate_available(available)
        return available[rng.randrange(len(available))]


class Ucb1OperatorPolicy(_TrackedPolicy):
    """UCB1 с обязательной первой пробой каждого доступного оператора."""

    name = "ucb1"

    def __init__(self, operators: tuple[str, ...], exploration: float = math.sqrt(2.0)) -> None:
        super().__init__(operators)
        if not math.isfinite(exploration) or exploration < 0.0:
            raise ValueError("ucb_exploration должен быть конечным неотрицательным числом")
        self.exploration = exploration

    def choose(self, available: tuple[str, ...], rng: random.Random) -> str:
        self._validate_available(available)
        untried = tuple(
            operator for operator in available if self._stats[operator].selections == 0
        )
        if untried:
            return untried[rng.randrange(len(untried))]

        total = max(1, sum(stat.selections for stat in self._stats.values()))
        scores = {
            operator: self._stats[operator].mean_reward
            + self.exploration
            * math.sqrt(math.log(total) / self._stats[operator].selections)
            for operator in available
        }
        best = max(scores.values())
        tied = tuple(
            operator
            for operator in available
            if math.isclose(scores[operator], best, rel_tol=1e-12, abs_tol=1e-12)
        )
        return tied[rng.randrange(len(tied))]

    def snapshot(self) -> dict[str, object]:
        return {**super().snapshot(), "exploration": self.exploration}


def build_operator_policy(
    name: str,
    operators: tuple[str, ...],
    *,
    exploration: float = math.sqrt(2.0),
) -> OperatorPolicy:
    """Создать явно выбранную политику для A/B-сравнения."""

    normalized = name.strip().casefold()
    if normalized == "uniform":
        return UniformOperatorPolicy(operators)
    if normalized == "ucb1":
        return Ucb1OperatorPolicy(operators, exploration)
    raise ValueError("operator_policy должен быть 'uniform' или 'ucb1'")
