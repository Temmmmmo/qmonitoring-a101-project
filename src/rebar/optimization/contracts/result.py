"""Единый выходной контракт и параметры запуска алгоритмов."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from rebar.models import Rebar

from .problem import BBox


@dataclass(frozen=True)
class ObjectiveWeights:
    """Общие коэффициенты цели; штраф за деталь выражается в эквиваленте кг."""

    mass: float = 1.0
    detail_penalty_kg: float = 0.0

    def __post_init__(self) -> None:
        if self.mass < 0 or self.detail_penalty_kg < 0:
            raise ValueError("коэффициенты целевой функции не могут быть отрицательными")


@dataclass(frozen=True)
class AlgorithmRequest:
    """Параметры запуска, не привязанные к конкретной реализации."""

    objective: ObjectiveWeights = field(default_factory=ObjectiveWeights)
    max_details: int | None = None
    time_limit_s: float | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_details is not None and self.max_details < 1:
            raise ValueError("max_details должен быть не меньше 1")
        if self.time_limit_s is not None and self.time_limit_s <= 0:
            raise ValueError("time_limit_s должен быть положительным")


@dataclass(frozen=True)
class LayoutZone:
    """Одна параметрическая прямоугольная группа параллельных стержней."""

    id: str
    # Envelope осей установленных стержней: поперёк — от первого до последнего,
    # вдоль — полная фактически принятая длина.
    bbox: BBox
    # Минимальная область спроса, из которой построена группа, без анкеровки и раскроя.
    demand_bbox: BBox
    level_index: int
    rebar: Rebar
    width_mm: float
    required_length_mm: float
    anchored_length_mm: float
    installed_length_mm: float
    # Глобальная поперечная координата оси первого стержня (Y для Axis.X, X для Axis.Y).
    first_bar_coordinate_mm: float
    bar_count: int
    mass_kg: float
    covered_cell_ids: tuple[int, ...] = ()
    overcovered_cell_ids: tuple[int, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LayoutMetrics:
    """Единые метрики, по которым сравниваются разные алгоритмы."""

    detail_count: int
    total_mass_kg: float
    demanded_cell_count: int
    covered_demanded_cell_count: int
    under_reinforced_cell_count: int
    overcovered_cell_count: int
    overcovered_area_mm2: float
    objective_value: float
    physical_bar_count: int = 0
    total_bar_length_mm: float = 0.0


@dataclass(frozen=True)
class LayoutEvaluation:
    """Результат независимой проверки и пересчёта метрик решения."""

    valid: bool
    metrics: LayoutMetrics
    diagnostics: tuple[str, ...] = ()


class SolutionStatus(str, Enum):
    """Статус решения относительно переданной математической модели."""

    FEASIBLE = "feasible"
    OPTIMAL = "optimal"
    INFEASIBLE = "infeasible"
    TIME_LIMIT = "time_limit"
    ERROR = "error"


@dataclass(frozen=True)
class LayoutSolution:
    """Единый выход алгоритма, пригодный для сравнения, HTML и JSON."""

    algorithm: str
    status: SolutionStatus
    zones: tuple[LayoutZone, ...]
    metrics: LayoutMetrics
    request: AlgorithmRequest = field(default_factory=AlgorithmRequest)
    runtime_ms: float = 0.0
    diagnostics: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)
