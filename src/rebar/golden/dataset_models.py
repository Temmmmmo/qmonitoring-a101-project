"""Контракты каталога инженерских выдач для eval и обучения предпочтений.

Эти типы описывают доступные наблюдения инженера, а не выход оптимизатора. Они
намеренно отделены от публичного ``rebar.models`` и от единственного строгого
``GoldenCaseDefinition``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rebar.models import Direction

from .models import GoldenSheetMetrics


class EngineerMetricStatus(str, Enum):
    """Насколько пригодна PDF-выдача как числовая метка."""

    VERIFIED_PDF_SPEC = "verified_pdf_spec"
    PDF_TEXT_UNREADABLE = "pdf_text_unreadable"


@dataclass(frozen=True)
class EngineerInputSetDefinition:
    """Один четырёхнаправленный вход, связанный с инженерской выдачей."""

    id: str
    title: str
    relative_directory: tuple[str, ...]
    dxf_by_direction: tuple[tuple[Direction, str], ...]
    direction_mapping_verified: bool = False

    @property
    def direction_file_names(self) -> dict[Direction, str]:
        """Вернуть имена четырёх DXF по направлениям."""

        return dict(self.dxf_by_direction)


@dataclass(frozen=True)
class EngineerReferenceSheet:
    """Проверенный лист PDF с одной или несколькими спецификациями направлений."""

    pdf_page: int
    directions: tuple[Direction, ...]
    expected_position_count: int
    expected_bar_count: int
    expected_mass_kg: float


@dataclass(frozen=True)
class EngineerReferenceCase:
    """Одна независимая объединённая инженерская выдача."""

    id: str
    title: str
    dataset_dir_name: str
    engineer_pdf_parts: tuple[str, ...]
    input_sets: tuple[EngineerInputSetDefinition, ...]
    metric_status: EngineerMetricStatus
    sheets: tuple[EngineerReferenceSheet, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def expected_position_count(self) -> int | None:
        if not self.sheets:
            return None
        return sum(sheet.expected_position_count for sheet in self.sheets)

    @property
    def expected_bar_count(self) -> int | None:
        if not self.sheets:
            return None
        return sum(sheet.expected_bar_count for sheet in self.sheets)

    @property
    def expected_mass_kg(self) -> float | None:
        if not self.sheets:
            return None
        return round(sum(sheet.expected_mass_kg for sheet in self.sheets), 2)


@dataclass(frozen=True)
class EngineerReferenceFiles:
    """Локальные файлы одной инженерской выдачи."""

    engineer_pdf: Path
    dxf_by_input_set: dict[str, dict[Direction, Path]]


@dataclass(frozen=True)
class EngineerReferenceMetrics:
    """Повторно извлечённые метрики релевантных листов PDF."""

    sheets: tuple[tuple[EngineerReferenceSheet, GoldenSheetMetrics], ...]

    @property
    def position_count(self) -> int:
        return sum(metrics.position_count for _, metrics in self.sheets)

    @property
    def bar_count(self) -> int:
        return sum(metrics.bar_count for _, metrics in self.sheets)

    @property
    def total_mass_kg(self) -> float:
        return round(sum(metrics.total_mass_kg for _, metrics in self.sheets), 2)
