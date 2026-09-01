"""Контракты явных таблиц ``As -> арматура`` вне ingest-модели."""

from __future__ import annotations

from dataclasses import dataclass

from rebar.models import Rebar


@dataclass(frozen=True)
class RebarBandMapping:
    """Инженерное назначение одной позиции цветовой шкалы."""

    label: str
    threshold_as: float
    background: Rebar
    additional: Rebar | None


@dataclass(frozen=True)
class RebarMapping:
    """Версионированная внешняя таблица для конкретной расчётной выдачи.

    ``expected_scale_bounds_as`` описывает числовую шкалу DXF, с которой таблицу
    разрешено связывать. Цвета ACI намеренно не хранятся: они берутся из текущего
    ``Mosaic`` и сопоставляются с назначениями только по порядку полос.
    """

    id: str
    source: str
    status: str
    expected_scale_bounds_as: tuple[float, ...]
    bands: tuple[RebarBandMapping, ...]
    a101_profile_id: str | None = None
    scale_tolerance_as: float = 0.05

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("идентификатор таблицы армирования не может быть пустым")
        if not self.source.strip():
            raise ValueError("источник таблицы армирования не может быть пустым")
        if self.a101_profile_id is not None and not self.a101_profile_id.strip():
            raise ValueError("идентификатор профиля А101 не может быть пустым")
        if len(self.expected_scale_bounds_as) != len(self.bands) + 1:
            raise ValueError("число границ шкалы должно быть на один больше числа полос")
        if self.scale_tolerance_as < 0:
            raise ValueError("допуск шкалы As не может быть отрицательным")
        if any(
            first >= second
            for first, second in zip(
                self.expected_scale_bounds_as,
                self.expected_scale_bounds_as[1:],
            )
        ):
            raise ValueError("ожидаемые границы шкалы As должны строго возрастать")
        if any(
            first.threshold_as >= second.threshold_as
            for first, second in zip(self.bands, self.bands[1:])
        ):
            raise ValueError("пороги назначений арматуры должны строго возрастать")
        if any(not band.label.strip() for band in self.bands):
            raise ValueError("подпись полосы армирования не может быть пустой")
