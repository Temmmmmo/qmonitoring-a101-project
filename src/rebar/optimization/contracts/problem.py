"""Единый входной контракт для всех алгоритмов раскладки."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rebar.models import Direction, Point, Rebar

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class DemandLevel:
    """Один упорядоченный уровень требуемого армирования."""

    index: int
    aci: int | None
    lower_as: float | None
    upper_as: float | None
    label: str | None
    additional: Rebar | None
    requires_extra: bool | None


@dataclass(frozen=True)
class DemandCell:
    """КЭ, сведённый к уровню спроса, понятному любому оптимизатору."""

    id: int
    poly: tuple[Point, ...]
    centroid: Point
    aci: int
    level_index: int


@dataclass(frozen=True)
class DemandMap:
    """Нормализованное поле требований для одного направления армирования."""

    direction: Direction
    levels: tuple[DemandLevel, ...]
    cells: tuple[DemandCell, ...]
    bbox: BBox
    source_path: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def level(self, index: int) -> DemandLevel:
        """Вернуть уровень по стабильному индексу шкалы."""

        try:
            level = self.levels[index]
        except IndexError as error:
            raise KeyError(f"уровень спроса {index} отсутствует") from error
        if level.index != index:
            raise KeyError(f"уровень спроса {index} отсутствует")
        return level


@dataclass(frozen=True)
class LayoutConstraints:
    """Общие инженерные ограничения, одинаковые для всех алгоритмов."""

    min_width_cells: int = 2
    anchorage_diameters: float = 40.0
    allow_overcoverage: bool = True
    allow_overlaps: bool = False
    enforce_zone_gap: bool = True

    def __post_init__(self) -> None:
        if self.min_width_cells < 1:
            raise ValueError("min_width_cells должен быть не меньше 1")
        if self.anchorage_diameters < 0:
            raise ValueError("anchorage_diameters не может быть отрицательным")


@dataclass(frozen=True)
class LayoutProblem:
    """Единый вход BSP, Greedy, CP-SAT/MILP и будущих алгоритмов."""

    demand: DemandMap
    constraints: LayoutConstraints = field(default_factory=LayoutConstraints)
    case_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
