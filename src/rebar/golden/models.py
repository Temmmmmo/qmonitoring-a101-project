"""Доменные модели эталонных инженерных раскладок.

Они намеренно живут отдельно от ``rebar.models`` и контрактов оптимизации: эталон
описывает наблюдаемую инженерную выдачу, а не меняет публичную модель ``Mosaic``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rebar.models import Direction


@dataclass(frozen=True)
class GoldenBarPosition:
    """Одна строка спецификации стержней на инженерном листе."""

    position: str
    diameter_mm: int
    length_mm: int
    quantity: int
    unit_mass_kg: float
    total_mass_kg: float


@dataclass(frozen=True)
class GoldenSheetMetrics:
    """Метрики, извлечённые из спецификации одного направления."""

    positions: tuple[GoldenBarPosition, ...]

    @property
    def position_count(self) -> int:
        return len(self.positions)

    @property
    def bar_count(self) -> int:
        return sum(position.quantity for position in self.positions)

    @property
    def total_mass_kg(self) -> float:
        return round(sum(position.total_mass_kg for position in self.positions), 2)


@dataclass(frozen=True)
class GoldenSheetExpectation:
    """Ожидаемая страница и контрольные числа одного направления."""

    direction: Direction
    title: str
    engineer_axis_label: str
    pdf_page: int
    input_png_name: str
    expected_position_count: int
    expected_bar_count: int
    expected_mass_kg: float


@dataclass(frozen=True)
class GoldenCaseDefinition:
    """Переносимое описание эталона без абсолютных путей и бинарных данных."""

    id: str
    title: str
    dataset_dir_name: str
    input_task_dir_name: str
    engineer_pdf_name: str
    sheets: tuple[GoldenSheetExpectation, ...]
    notes: tuple[str, ...] = ()

    @property
    def expected_position_count(self) -> int:
        return sum(sheet.expected_position_count for sheet in self.sheets)

    @property
    def expected_bar_count(self) -> int:
        return sum(sheet.expected_bar_count for sheet in self.sheets)

    @property
    def expected_mass_kg(self) -> float:
        return round(sum(sheet.expected_mass_kg for sheet in self.sheets), 2)


@dataclass(frozen=True)
class GoldenCaseFiles:
    """Разрешённые локальные пути к материалам одного эталона."""

    engineer_pdf: Path
    input_png_by_direction: dict[Direction, Path]

