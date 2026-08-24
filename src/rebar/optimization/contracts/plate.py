"""Контракты задачи и результата для полного комплекта направлений плиты."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from rebar.models import Axis, Direction, Layer

from .problem import LayoutProblem
from .result import LayoutSolution, SolutionStatus

PLATE_DIRECTIONS = (
    Direction(Layer.BOTTOM, Axis.X),
    Direction(Layer.BOTTOM, Axis.Y),
    Direction(Layer.TOP, Axis.X),
    Direction(Layer.TOP, Axis.Y),
)

_DirectionItem = TypeVar("_DirectionItem")


def _canonical_direction_items(
    items: tuple[_DirectionItem, ...],
    direction_of: Callable[[_DirectionItem], Direction],
    *,
    item_name: str,
) -> tuple[_DirectionItem, ...]:
    """Проверить полноту четырёх направлений и вернуть стабильный порядок."""

    by_direction: dict[Direction, _DirectionItem] = {}
    duplicates: list[Direction] = []
    for item in items:
        direction = direction_of(item)
        if direction in by_direction:
            duplicates.append(direction)
        else:
            by_direction[direction] = item

    if duplicates:
        labels = ", ".join(sorted({str(direction) for direction in duplicates}))
        raise ValueError(f"{item_name}: направления повторяются: {labels}")

    missing = [direction for direction in PLATE_DIRECTIONS if direction not in by_direction]
    unexpected = [direction for direction in by_direction if direction not in PLATE_DIRECTIONS]
    if missing or unexpected:
        fragments = []
        if missing:
            fragments.append(f"отсутствуют: {', '.join(map(str, missing))}")
        if unexpected:
            fragments.append(f"неизвестны: {', '.join(map(str, unexpected))}")
        raise ValueError(
            f"{item_name}: нужен комплект из четырёх направлений; {'; '.join(fragments)}"
        )

    return tuple(by_direction[direction] for direction in PLATE_DIRECTIONS)


@dataclass(frozen=True)
class PlateProblem:
    """Четыре независимых однонаправленных задачи одной плиты."""

    direction_problems: tuple[LayoutProblem, ...]
    case_id: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        canonical = _canonical_direction_items(
            self.direction_problems,
            lambda problem: problem.demand.direction,
            item_name="PlateProblem",
        )
        object.__setattr__(self, "direction_problems", canonical)

    def problem(self, direction: Direction) -> LayoutProblem:
        """Вернуть задачу указанного направления."""

        for problem in self.direction_problems:
            if problem.demand.direction == direction:
                return problem
        raise KeyError(f"направление {direction} отсутствует")


@dataclass(frozen=True)
class PlateMetrics:
    """Суммарные метрики четырёх направлений без смешения их семантики."""

    direction_count: int
    zone_count: int
    physical_bar_count: int
    total_mass_kg: float
    total_bar_length_mm: float
    demanded_cell_count: int
    covered_demanded_cell_count: int
    under_reinforced_cell_count: int
    overcovered_cell_count: int
    overcovered_area_mm2: float
    objective_value: float


@dataclass(frozen=True)
class PlateDirectionSolution:
    """Однонаправленное решение с явно сохранённым направлением."""

    direction: Direction
    solution: LayoutSolution


@dataclass(frozen=True)
class PlateSolution:
    """Один общеплитный вариант, содержащий решение каждого направления."""

    direction_solutions: tuple[PlateDirectionSolution, ...]
    status: SolutionStatus
    metrics: PlateMetrics
    runtime_ms: float = 0.0
    diagnostics: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        canonical = _canonical_direction_items(
            self.direction_solutions,
            lambda item: item.direction,
            item_name="PlateSolution",
        )
        object.__setattr__(self, "direction_solutions", canonical)

    @property
    def valid(self) -> bool:
        """Можно ли использовать вариант как проверенное общеплитное решение."""

        return (
            self.status in {SolutionStatus.FEASIBLE, SolutionStatus.OPTIMAL}
            and self.metrics.direction_count == len(PLATE_DIRECTIONS)
            and self.metrics.under_reinforced_cell_count == 0
        )

    def solution(self, direction: Direction) -> LayoutSolution:
        """Вернуть решение указанного направления."""

        for item in self.direction_solutions:
            if item.direction == direction:
                return item.solution
        raise KeyError(f"направление {direction} отсутствует")
