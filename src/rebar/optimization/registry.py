"""Registry для выбора алгоритма по стабильному имени."""

from __future__ import annotations

from collections.abc import Callable

from .algorithms.base import LayoutOptimizer

OptimizerFactory = Callable[[], LayoutOptimizer]


class OptimizerRegistry:
    """Локальный registry без скрытого глобального состояния."""

    def __init__(self) -> None:
        self._factories: dict[str, OptimizerFactory] = {}

    def register(self, name: str, factory: OptimizerFactory) -> None:
        normalized = name.strip().casefold()
        if not normalized:
            raise ValueError("имя алгоритма не может быть пустым")
        if normalized in self._factories:
            raise ValueError(f"алгоритм уже зарегистрирован: {normalized}")
        self._factories[normalized] = factory

    def create(self, name: str) -> LayoutOptimizer:
        normalized = name.strip().casefold()
        try:
            factory = self._factories[normalized]
        except KeyError as error:
            available = ", ".join(self.names()) or "нет"
            raise KeyError(f"неизвестный алгоритм {name!r}; доступны: {available}") from error
        optimizer = factory()
        if optimizer.name.strip().casefold() != normalized:
            raise ValueError(f"factory {normalized!r} вернула алгоритм с именем {optimizer.name!r}")
        return optimizer

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


def built_in_optimizer_registry() -> OptimizerRegistry:
    """Создать новый registry со всеми встроенными алгоритмами."""

    from .algorithms import (
        BspOptimizer,
        GreedyStripOptimizer,
        PriorityGreedyOptimizer,
        StrongestBBoxOptimizer,
    )

    registry = OptimizerRegistry()
    registry.register(StrongestBBoxOptimizer.name, StrongestBBoxOptimizer)
    registry.register(BspOptimizer.name, BspOptimizer)
    registry.register(GreedyStripOptimizer.name, GreedyStripOptimizer)
    registry.register(PriorityGreedyOptimizer.name, PriorityGreedyOptimizer)
    return registry
