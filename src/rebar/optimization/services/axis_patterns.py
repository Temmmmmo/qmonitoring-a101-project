"""Ограниченное разворачивание периодических схем, без изменения номинального шага."""

from __future__ import annotations

import math

from rebar.models import ReinforcementRecipe

from ..contracts.placement import AxisPlacement, PeriodicAxisPattern, RecipePlacement, UniformBarRun
from .geometry import GEOMETRY_TOLERANCE_MM

MAX_PATTERN_BARS = 100_000


def pattern_runs(
    placement: AxisPlacement,
    axis_window_mm: tuple[float, float],
    *,
    maximum_bars: int = MAX_PATTERN_BARS,
) -> tuple[UniformBarRun, ...]:
    """Выбрать оси внутри замкнутого окна; ограничение ресурсов не обрезает результат."""
    if placement.origin_mm is None:
        raise ValueError("не согласована поперечная привязка осей (origin_mm)")
    if not isinstance(maximum_bars, int) or isinstance(maximum_bars, bool) or maximum_bars < 1:
        raise ValueError("maximum_bars должен быть положительным целым")
    lower, upper = axis_window_mm
    if not all(math.isfinite(x) for x in (lower, upper)) or lower > upper:
        raise ValueError("невалидный интервал выбора осей")
    pattern, origin = placement.pattern, placement.origin_mm
    runs = []
    count = 0
    for offset in pattern.offsets_mm:
        first_q = (lower - origin - offset - GEOMETRY_TOLERANCE_MM) / pattern.period_mm
        last_q = (upper - origin - offset + GEOMETRY_TOLERANCE_MM) / pattern.period_mm
        if not math.isfinite(first_q) or not math.isfinite(last_q):
            raise ValueError("интервал осей превышает числовой диапазон")
        first_k, last_k = math.ceil(first_q), math.floor(last_q)
        if first_k > last_k:
            continue
        quantity = last_k - first_k + 1
        count += quantity
        if count > maximum_bars:
            raise ValueError(f"схема превышает лимит {maximum_bars} осей; результат не обрезан")
        runs.append(UniformBarRun(origin + offset + first_k * pattern.period_mm, pattern.period_mm, quantity))
    return tuple(sorted(runs, key=lambda run: run.first_axis_mm))


def pattern_coordinates(
    placement: AxisPlacement,
    axis_window_mm: tuple[float, float],
    *,
    maximum_bars: int = MAX_PATTERN_BARS,
) -> tuple[float, ...]:
    runs = pattern_runs(placement, axis_window_mm, maximum_bars=maximum_bars)
    return tuple(sorted(run.first_axis_mm + i * run.actual_step_mm for run in runs for i in range(run.bar_count)))


def a101_247_slab_recipe_placement(
    recipe: ReinforcementRecipe,
    *,
    background_origin_mm: float | None = None,
) -> RecipePlacement:
    """ЯВНО выбранная схема 2.4.7 для @300-фона и условного @150.

    Не вызывается парсером/GA автоматически. Для добавок @300 фаза неизвестна,
    особенно Ø25 в тройной комбинации. Для @100 требуется отдельная схема укладки
    вплотную: эту неопределённость нельзя подменить равномерной сеткой.
    """
    if recipe.background.step != 300:
        raise ValueError("эта схема 2.4.7 требует фон @300")
    additions = []
    for index, spec in enumerate(recipe.additions):
        if spec.step == 150:
            # Подтверждена первая добавка между фоновыми осями. Следующий набор
            # нельзя автоматически посадить на те же оси, даже если он тоже @150.
            origin = background_origin_mm if index == 0 else None
            additions.append(AxisPlacement(PeriodicAxisPattern(300.0, (100.0, 200.0)), origin))
        elif spec.step == 300:
            additions.append(AxisPlacement(PeriodicAxisPattern.uniform(300.0), None))
        else:
            raise ValueError(f"для условного шага {spec.step} нужна отдельная согласованная схема осей")
    return RecipePlacement(
        AxisPlacement(PeriodicAxisPattern.uniform(300.0), background_origin_mm), tuple(additions),
        "A101-2.4.7/slab-300-150; Revit specialist clarification 2026-09-07; partial",
    )
