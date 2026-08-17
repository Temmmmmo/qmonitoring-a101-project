"""Протокол взаимозаменяемого алгоритма оптимизации."""

from __future__ import annotations

from typing import Protocol

from ..contracts import AlgorithmRequest, LayoutProblem, LayoutSolution


class LayoutOptimizer(Protocol):
    """Стратегия, которая решает один и тот же ``LayoutProblem``."""

    name: str

    def solve(
        self,
        problem: LayoutProblem,
        request: AlgorithmRequest | None = None,
    ) -> LayoutSolution:
        """Вернуть решение в общем формате, не выполняя рендер или экспорт."""

        ...
