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


class UnsupportedReinforcementRecipeError(ValueError):
    """Составную схему нельзя безопасно представить старым одиночным набором."""


@dataclass(frozen=True)
class ReinforcementRecipe:
    """Фон и ВСЕ локальные добавки; шаги здесь условные, не координаты осей.

    Слагаемые сохраняют порядок и не дедуплицируются, даже если спецификации равны.
    """

    background: Rebar
    additions: tuple[Rebar, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.additions, tuple):
            raise ValueError("additions должен быть неизменяемым tuple")
        for spec in (self.background, *self.additions):
            if (not isinstance(spec.step, int) or isinstance(spec.step, bool) or spec.step <= 0
                    or not isinstance(spec.diameter, int) or isinstance(spec.diameter, bool)
                    or spec.diameter <= 0):
                raise ValueError("схема требует положительные целые шаги и диаметры")


@dataclass
class Band:
    """Одна полоса цветовой шкалы (легенды).

    background — фоновое армирование. additional — совместимое поле ОДНОЙ добавки;
    None может означать как отсутствие добавок, так и составную recipe. Наличие
    спроса проверять по reinforcement_recipe.additions, а не по одному additional.
    """
    index: int                       # позиция в шкале (0 = низ/фон)
    aci: int | None                  # ACI-цвет полосы (None если неизвестен из источника)
    label: str                       # исходная подпись, напр. "s300d18+s150d20"
    threshold_as: float              # порог As, см²/м (из .shk)
    background: Rebar                # фоновое армирование (левая часть подписи)
    additional: Rebar | None         # дополнительное (правая часть после '+'), или None
    # Для нескольких добавок additional=None, а ВСЕ наборы находятся в recipe.
    recipe: ReinforcementRecipe | None = None

    def __post_init__(self) -> None:
        if self.recipe is None:
            if len(self.label.split("+")) > 2:
                raise UnsupportedReinforcementRecipeError("составная подпись требует явного Band.recipe")
            return
        single = self.recipe.additions[0] if len(self.recipe.additions) == 1 else None
        if self.background != self.recipe.background or self.additional != single:
            raise ValueError("поля Band не согласованы с recipe")

    @property
    def reinforcement_recipe(self) -> ReinforcementRecipe:
        """Совместимое представление старого конструктора Band с одной добавкой."""
        return self.recipe or ReinforcementRecipe(
            self.background, () if self.additional is None else (self.additional,),
        )


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
        return self.band is not None and bool(self.band.reinforcement_recipe.additions)


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
