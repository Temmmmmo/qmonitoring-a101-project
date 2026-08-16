"""Контракт данных ядра. Все размеры — в МИЛЛИМЕТРАХ.

Это публичный интерфейс между стадиями пайплайна (A ingest → B segmentation → ...).
НЕ менять поля/сигнатуры без согласования — на них завязан весь пайплайн.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

Point = tuple[float, float]  # (x, y) в мм


class Layer(str, Enum):
    """Слой армирования плиты."""
    BOTTOM = "bottom"  # Нижнее
    TOP = "top"        # Верхнее


class Axis(str, Enum):
    """Направление (ориентация) стержней."""
    X = "X"  # горизонтальная ось
    Y = "Y"  # вертикальная ось


@dataclass(frozen=True)
class Direction:
    """Одно из 4 направлений раскладки. Обычно определяется из имени файла."""
    layer: Layer
    axis: Axis

    def __str__(self) -> str:
        return f"{self.layer.value}-{self.axis.value}"


@dataclass(frozen=True)
class Rebar:
    """Спецификация стержней: шаг и диаметр (мм)."""
    step: int      # шаг, мм (напр. 300, 150, 100)
    diameter: int  # диаметр, мм (напр. 18, 20, 25)


@dataclass
class Band:
    """Одна полоса цветовой шкалы (легенды).

    background — фоновое армирование (есть всегда), additional — добавка, которую
    мы раскладываем (None для самой нижней полосы, где добавки нет).
    """
    index: int                       # позиция в шкале (0 = низ/фон)
    aci: int | None                  # ACI-цвет полосы (None если неизвестен из источника)
    label: str                       # исходная подпись, напр. "s300d18+s150d20"
    threshold_as: float              # порог As, см²/м (из .shk)
    background: Rebar                # фоновое армирование (левая часть подписи)
    additional: Rebar | None         # дополнительное (правая часть после '+'), или None


@dataclass
class Cell:
    """Один конечный элемент (КЭ) мозаики = один 3DFACE."""
    poly: list[Point]                # вершины квада, мм (обычно 4)
    centroid: Point                  # центр, мм
    aci: int                         # ACI-цвет элемента
    band: Band | None = None         # сопоставленная полоса шкалы (заполняется при ingest)

    @property
    def needs_extra(self) -> bool:
        """Требуется ли дополнительное армирование в этом КЭ."""
        return self.band is not None and self.band.additional is not None


@dataclass
class Mosaic:
    """Нормализованная модель одной мозаики (одно направление).

    Результат стадии A (ingest). Вход для стадии B (segmentation).
    """
    direction: Direction
    cells: list[Cell]
    legend: list[Band]
    bbox: tuple[float, float, float, float]  # (xmin, ymin, xmax, ymax), мм
    source_path: str = ""                    # откуда прочитано (для отладки)
    meta: dict = field(default_factory=dict) # прочее (единицы, версия и т.п.)
