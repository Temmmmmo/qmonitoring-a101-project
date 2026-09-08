"""Параметрические фактические оси отдельно от условного шага спецификации.

Контракт составной зоны не заменяет LayoutZone и не расширяет разрешённые overlap
старого GA. Один дополнительный набор может состоять из нескольких регулярных рядов.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from rebar.models import Direction, Rebar, ReinforcementRecipe

from .problem import BBox


@dataclass(frozen=True)
class PeriodicAxisPattern:
    """Оси = origin + k × period + offset; k — любое целое число.

    Например, period=300, offsets=(100, 200) задаёт последовательность 100/200,
    а не равномерный шаг 150. Origin хранится отдельно и никогда не подразумевает 0.
    """

    period_mm: float
    offsets_mm: tuple[float, ...]

    def __post_init__(self) -> None:
        if not math.isfinite(self.period_mm) or self.period_mm <= 1e-6:
            raise ValueError("период осей должен быть конечным и больше 1e-6 мм")
        if not isinstance(self.offsets_mm, tuple) or not 1 <= len(self.offsets_mm) <= 64:
            raise ValueError("нужен tuple из 1..64 смещений внутри периода")
        if any(not math.isfinite(x) or not 0 <= x < self.period_mm for x in self.offsets_mm):
            raise ValueError("смещения должны быть конечными и находиться в [0, period)")
        gaps = [b - a for a, b in zip(self.offsets_mm, self.offsets_mm[1:])]
        gaps.append(self.period_mm + self.offsets_mm[0] - self.offsets_mm[-1])
        if min(gaps) <= 1e-6:
            raise ValueError("смещения должны возрастать без совпадающих осей")

    @classmethod
    def uniform(cls, actual_step_mm: float) -> PeriodicAxisPattern:
        return cls(actual_step_mm, (0.0,))

    @property
    def mean_spacing_mm(self) -> float:
        return self.period_mm / len(self.offsets_mm)


@dataclass(frozen=True)
class AxisPlacement:
    pattern: PeriodicAxisPattern
    # Глобальная поперечная координата начала периода; None = не согласовано.
    origin_mm: float | None = None
    # Расстояние ОСИ от соответствующей верхней/нижней грани, не защитный слой.
    axis_depth_from_face_mm: float | None = None

    def __post_init__(self) -> None:
        if self.origin_mm is not None and not math.isfinite(self.origin_mm):
            raise ValueError("origin_mm должен быть конечным либо None")
        if self.axis_depth_from_face_mm is not None and (
            not math.isfinite(self.axis_depth_from_face_mm) or self.axis_depth_from_face_mm <= 0
        ):
            raise ValueError("привязка оси к грани должна быть конечной положительной либо None")


@dataclass(frozen=True)
class RecipePlacement:
    background: AxisPlacement
    # Порядок совпадает с ReinforcementRecipe.additions, без дедупликации.
    additions: tuple[AxisPlacement, ...]
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.additions, tuple) or not self.source.strip():
            raise ValueError("нужны tuple размещений добавок и явный источник схемы")


@dataclass(frozen=True)
class UniformBarRun:
    """Транспортный регулярный поднабор; не отдельная логическая зона."""

    first_axis_mm: float
    actual_step_mm: float
    bar_count: int

    def __post_init__(self) -> None:
        if (not math.isfinite(self.first_axis_mm) or not math.isfinite(self.actual_step_mm)
                or self.actual_step_mm <= 1e-6 or not isinstance(self.bar_count, int)
                or isinstance(self.bar_count, bool) or self.bar_count < 1):
            raise ValueError("невалидный регулярный ряд осей")


@dataclass(frozen=True)
class PatternedRebarSet:
    component_index: int
    rebar: Rebar  # Условный шаг из исходной шкалы остаётся здесь.
    placement: AxisPlacement
    # Интервал ВЫБОРА ОСЕЙ, не контур AreaReinforcement и не граница Revit-host.
    axis_window_mm: tuple[float, float]
    longitudinal_interval_mm: tuple[float, float]
    required_length_mm: float
    anchored_length_mm: float
    installed_length_mm: float
    bar_count: int
    mass_kg: float


@dataclass(frozen=True)
class CompositeLayoutZone:
    id: str
    direction: Direction
    level_index: int
    demand_bbox: BBox
    recipe: ReinforcementRecipe
    placement: RecipePlacement
    components: tuple[PatternedRebarSet, ...]


@dataclass(frozen=True)
class CompositeZoneEvaluation:
    # Только воспроизводимость геометрии/ведомости, НЕ инженерная допустимость.
    geometry_valid: bool
    physical_bar_count: int
    total_mass_kg: float
    total_bar_length_mm: float
    diagnostics: tuple[str, ...]
    zone_count: int = 1
    component_count: int = 0
    uniform_run_count: int = 0
