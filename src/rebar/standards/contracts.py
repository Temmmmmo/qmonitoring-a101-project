"""Контракты версионированных инженерных стандартов армирования."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rebar.models import Rebar


class A101PositionStatus(str, Enum):
    """Статус строки, буквально перенесённый из таблицы А101."""

    ALLOWED = "allowed"
    PROHIBITED = "prohibited"


@dataclass(frozen=True)
class A101RebarPosition:
    """Одна строка дополнительного армирования в выбранном профиле."""

    additional: Rebar
    status: A101PositionStatus = A101PositionStatus.ALLOWED
    layer_count: int = 1

    def __post_init__(self) -> None:
        if self.layer_count < 1:
            raise ValueError("число сеток дополнительного армирования должно быть положительным")


@dataclass(frozen=True)
class A101RebarProfile:
    """Строковый блок таблицы для конкретного типа и толщины конструкции."""

    id: str
    table_id: str
    pdf_page: int
    construction_type: str
    thickness_label: str
    background: Rebar
    positions: tuple[A101RebarPosition, ...]
    source: str

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.table_id.strip():
            raise ValueError("профиль А101 должен иметь идентификатор и номер таблицы")
        if self.pdf_page < 1:
            raise ValueError("номер страницы профиля А101 должен быть положительным")
        if not self.construction_type.strip() or not self.thickness_label.strip():
            raise ValueError("тип и толщина конструкции не могут быть пустыми")
        if not self.positions:
            raise ValueError("профиль А101 должен содержать хотя бы одну позицию")
        signatures = [
            (
                position.additional.diameter,
                position.additional.step,
                position.layer_count,
            )
            for position in self.positions
        ]
        if len(signatures) != len(set(signatures)):
            raise ValueError(f"профиль А101 {self.id!r} содержит повторяющиеся позиции")


@dataclass(frozen=True)
class A101StepScheme:
    """Схема взаимного шага фонового и дополнительного армирования 2.4.7."""

    construction_type: str
    background_step_mm: int
    additional_steps_mm: tuple[int, ...]
    note_required_for_step_mm: int | None = None


class A101PositionOutcome(str, Enum):
    """Результат проверки фактически использованной позиции."""

    ALLOWED = "allowed"
    PROHIBITED = "prohibited"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class A101ZonePositionCheck:
    """Проверка одной зоны против явно выбранного профиля."""

    zone_id: str
    rebar: Rebar
    outcome: A101PositionOutcome


@dataclass(frozen=True)
class A101PositionValidation:
    """Сводка проверки всех зон одного решения."""

    profile_id: str | None
    table_id: str | None
    profile_title: str | None
    checks: tuple[A101ZonePositionCheck, ...]

    @property
    def allowed_count(self) -> int:
        return sum(check.outcome is A101PositionOutcome.ALLOWED for check in self.checks)

    @property
    def prohibited_count(self) -> int:
        return sum(
            check.outcome is A101PositionOutcome.PROHIBITED for check in self.checks
        )

    @property
    def unknown_count(self) -> int:
        return sum(check.outcome is A101PositionOutcome.UNKNOWN for check in self.checks)
