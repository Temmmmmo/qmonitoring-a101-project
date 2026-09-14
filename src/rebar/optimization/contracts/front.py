"""Контракты множества кандидатов и Парето-фронта раскладки."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rebar.models import Direction

from .plate import PlateSolution
from .result import LayoutSolution


class ComplexityAxis(str, Enum):
    """Явно выбранная ось сложности без скрытой взвешенной суммы."""

    ZONE_COUNT = "zone_count"
    PHYSICAL_BAR_COUNT = "physical_bar_count"
    POSITION_COUNT = "position_count"


@dataclass(frozen=True)
class ConstructabilityMetrics:
    """Прозрачные proxy-метрики комплектации и монтажа одного варианта."""

    zone_count: int
    physical_bar_count: int
    unique_diameter_count: int
    unique_step_count: int
    unique_installed_length_count: int
    unique_layout_signature_count: int
    warning_count: int
    error_count: int
    position_count: int = 0
    # Ключи нужны для объединения номенклатуры всей плиты, а не суммы счётчиков.
    bar_position_keys: tuple[tuple[str, str, int, float], ...] = ()
    # Presence of a declared class is not verification against a Revit bar type.
    position_class_declared: bool = False

    def value(self, axis: ComplexityAxis) -> int:
        """Вернуть выбранную ось сложности для Парето-сравнения."""

        if axis is ComplexityAxis.ZONE_COUNT:
            return self.zone_count
        if axis is ComplexityAxis.PHYSICAL_BAR_COUNT:
            return self.physical_bar_count
        if axis is ComplexityAxis.POSITION_COUNT:
            return self.position_count
        raise ValueError(f"неподдерживаемая ось сложности: {axis}")


@dataclass(frozen=True)
class CandidateRejection:
    """Кандидат, исключённый до Парето-сравнения hard-валидатором."""

    id: str
    algorithm: str
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class DirectionCandidate:
    """Проверенный вариант раскладки одного направления."""

    id: str
    direction: Direction
    solution: LayoutSolution
    constructability: ConstructabilityMetrics
    equivalent_candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DirectionParetoFront:
    """Недоминируемые однонаправленные варианты по массе и выбранной оси."""

    direction: Direction
    complexity_axis: ComplexityAxis
    candidates: tuple[DirectionCandidate, ...]
    source_candidate_count: int
    dominated_candidate_count: int
    equivalent_candidate_count: int
    rejections: tuple[CandidateRejection, ...] = ()
    # Для неаддитивного position_count локально доминируемый вариант может быть
    # полезен всей плите из-за совпадения типоразмеров с другими направлениями.
    combination_candidates: tuple[DirectionCandidate, ...] = ()


@dataclass(frozen=True)
class PlateCandidate:
    """Проверенный вариант плиты и его происхождение по четырём направлениям."""

    id: str
    solution: PlateSolution
    constructability: ConstructabilityMetrics
    direction_candidate_ids: tuple[str, ...]
    equivalent_candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlateParetoFront:
    """Общеплитный Парето-фронт, а не набор однонаправленных smoke-тестов."""

    complexity_axis: ComplexityAxis
    candidates: tuple[PlateCandidate, ...]
    direction_fronts: tuple[DirectionParetoFront, ...]
    combination_count: int
    dominated_candidate_count: int
    equivalent_candidate_count: int
