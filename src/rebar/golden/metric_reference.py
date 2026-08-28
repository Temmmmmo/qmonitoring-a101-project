"""Нормализованная plate-level метка для benchmark и калибровки предпочтений."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .dataset_models import EngineerMetricStatus, EngineerReferenceCase
from .models import GoldenCaseDefinition


@dataclass(frozen=True)
class PlateMetricReference:
    """Три сопоставимые числовые метрики инженерской выдачи.

    Такая метка слабее строгого golden: она не утверждает, что прямоугольники или
    отдельные направления нашей раскладки совпадают с решением конструктора.
    """

    id: str
    title: str
    expected_position_count: int
    expected_bar_count: int
    expected_mass_kg: float
    source_kind: str
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.title.strip():
            raise ValueError("метка должна иметь непустые id и title")
        if self.expected_position_count <= 0:
            raise ValueError("число позиций инженерской выдачи должно быть положительным")
        if self.expected_bar_count <= 0:
            raise ValueError("число физических стержней должно быть положительным")
        if not math.isfinite(self.expected_mass_kg) or self.expected_mass_kg <= 0:
            raise ValueError("масса инженерской выдачи должна быть положительной")
        if not self.source_kind.strip():
            raise ValueError("тип источника метки не может быть пустым")


def plate_metric_reference_from_golden(
    case: GoldenCaseDefinition,
) -> PlateMetricReference:
    """Свести строгий golden к общеплитным числовым метрикам."""

    return PlateMetricReference(
        id=case.id,
        title=case.title,
        expected_position_count=case.expected_position_count,
        expected_bar_count=case.expected_bar_count,
        expected_mass_kg=case.expected_mass_kg,
        source_kind="strict_golden",
        notes=case.notes,
    )


def plate_metric_reference_from_engineer(
    case: EngineerReferenceCase,
) -> PlateMetricReference:
    """Создать слабую числовую метку только из проверенной PDF-спецификации."""

    if case.metric_status is not EngineerMetricStatus.VERIFIED_PDF_SPEC:
        raise ValueError(
            f"инженерская выдача {case.id!r} не имеет проверенных числовых метрик"
        )
    position_count = case.expected_position_count
    bar_count = case.expected_bar_count
    mass_kg = case.expected_mass_kg
    if position_count is None or bar_count is None or mass_kg is None:
        raise ValueError(f"инженерская выдача {case.id!r} не содержит plate-level итогов")
    return PlateMetricReference(
        id=case.id,
        title=case.title,
        expected_position_count=position_count,
        expected_bar_count=bar_count,
        expected_mass_kg=mass_kg,
        source_kind="verified_pdf_spec",
        notes=case.notes,
    )
