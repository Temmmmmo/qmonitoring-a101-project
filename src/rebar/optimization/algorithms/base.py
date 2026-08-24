"""Протокол взаимозаменяемого алгоритма оптимизации."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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


@runtime_checkable
class LayoutCandidateGenerator(Protocol):
    """Алгоритм, способный вернуть серию решений одного ``LayoutProblem``.

    Обычный ``LayoutOptimizer`` по-прежнему возвращает одно решение. Эвристики,
    которые естественно строят популяцию или архив вариантов, дополнительно
    реализуют этот протокол; application-слой передаёт все варианты общему
    hard-валидатору и Парето-фильтру.
    """

    name: str

    def solve_many(
        self,
        problem: LayoutProblem,
        request: AlgorithmRequest | None = None,
    ) -> tuple[LayoutSolution, ...]:
        """Вернуть воспроизводимый конечный набор кандидатов."""

        ...
