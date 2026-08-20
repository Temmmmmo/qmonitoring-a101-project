"""Заменяемые правила анкеровочного выпуска для параметрических групп."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from rebar.models import Rebar


class AnchoragePolicy(Protocol):
    """Политика минимального выпуска с одного конца стержня."""

    name: str

    def extension_each_end_mm(self, rebar: Rebar) -> float:
        """Вернуть минимальный выпуск с одного конца в миллиметрах."""

        ...


@dataclass(frozen=True)
class FixedDiameterAnchoragePolicy:
    """Проектное правило ``multiplier × diameter``; для текущего ТЗ это 40d."""

    multiplier: float = 40.0
    name: str = "fixed-diameter"

    def __post_init__(self) -> None:
        if self.multiplier < 0:
            raise ValueError("множитель анкеровки не может быть отрицательным")

    def extension_each_end_mm(self, rebar: Rebar) -> float:
        if rebar.diameter <= 0:
            raise ValueError("диаметр арматуры должен быть положительным")
        return self.multiplier * rebar.diameter

