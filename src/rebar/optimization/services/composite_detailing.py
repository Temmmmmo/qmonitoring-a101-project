"""Детализация явно выбранной составной зоны и независимая проверка её ведомости.

Это НЕ оптимизатор и НЕ проверка покрытия/host/3D. Фон служит привязкой схемы и не
попадает в массу дополнительной арматуры. Неизвестные фазы не заменяются нулём.
"""

from __future__ import annotations

import math

from rebar.models import ReinforcementRecipe

from ..contracts import BBox, DemandMap, LayoutConstraints
from ..contracts.placement import (
    CompositeLayoutZone, CompositeZoneEvaluation, PatternedRebarSet, RecipePlacement,
)
from .anchorage import FixedDiameterAnchoragePolicy
from .axis_patterns import pattern_runs
from .bar_geometry import longitudinal_interval, transverse_interval
from .cutting import select_installed_length_mm
from .detailing import rebar_mass_kg, typical_transverse_cell_size_mm
from .geometry import GEOMETRY_TOLERANCE_MM


def validate_recipe_placement(recipe: ReinforcementRecipe, placement: RecipePlacement) -> None:
    if len(placement.additions) != len(recipe.additions):
        raise ValueError("размещение должно содержать ВСЕ добавки в порядке исходной схемы")
    for name, spec, axes in zip(
        ("фон", *(f"добавка {i + 1}" for i in range(len(recipe.additions)))),
        (recipe.background, *recipe.additions), (placement.background, *placement.additions),
    ):
        if axes.origin_mm is None:
            raise ValueError(f"{name}: не согласована поперечная привязка осей (origin_mm)")
        if axes.pattern.mean_spacing_mm > spec.step + GEOMETRY_TOLERANCE_MM:
            raise ValueError(f"{name}: плотность фактических осей меньше условной спецификации")


def _validate_request(
    demand: DemandMap, level_index: int, bbox: BBox, placement: RecipePlacement, constraints: LayoutConstraints,
) -> ReinforcementRecipe:
    if len(bbox) != 4 or not all(math.isfinite(x) for x in bbox) or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError("demand_bbox должен быть конечным невырожденным прямоугольником")
    recipe = demand.level(level_index).recipe
    if recipe is None or not recipe.additions:
        raise ValueError("уровень должен содержать явную recipe с дополнительными наборами")
    validate_recipe_placement(recipe, placement)
    if not math.isfinite(constraints.anchorage_diameters):
        raise ValueError("коэффициент анкеровки должен быть конечным")
    if any(not math.isfinite(x) for x in constraints.allowed_cut_lengths_mm):
        raise ValueError("каталог длин должен быть конечным")
    lower, upper = transverse_interval(demand.direction.axis, bbox)
    minimum = constraints.min_width_cells * typical_transverse_cell_size_mm(demand)
    if upper - lower + GEOMETRY_TOLERANCE_MM < minimum:
        raise ValueError("окно составной зоны меньше минимальной ширины в КЭ")
    return recipe


def build_composite_zone(
    demand: DemandMap,
    demand_bbox: BBox,
    level_index: int,
    zone_id: str,
    placement: RecipePlacement,
    *,
    constraints: LayoutConstraints | None = None,
) -> CompositeLayoutZone:
    """Сохранить логическую зону, построить каждый набор по его реальным осям.

    demand_bbox задаёт выбранное окно, не границу AreaReinforcement. Оно не меняется
    молча для подгонки шага. Полноту покрытия КЭ составной схемой этот сервис не доказывает.
    """
    constraints = constraints or LayoutConstraints()
    if not zone_id.strip():
        raise ValueError("нужен непустой идентификатор зоны")
    recipe = _validate_request(demand, level_index, demand_bbox, placement, constraints)
    window = transverse_interval(demand.direction.axis, demand_bbox)
    start, end = longitudinal_interval(demand.direction.axis, demand_bbox)
    required = end - start
    policy = FixedDiameterAnchoragePolicy(constraints.anchorage_diameters)
    components = []
    for index, (spec, axes) in enumerate(zip(recipe.additions, placement.additions)):
        runs = pattern_runs(axes, window)
        count = sum(run.bar_count for run in runs)
        if not count:
            raise ValueError(f"добавка {index + 1}: в выбранном окне нет ни одной оси")
        anchored = required + 2 * policy.extension_each_end_mm(spec)
        installed = select_installed_length_mm(anchored, constraints.allowed_cut_lengths_mm)
        extension = (installed - required) / 2
        components.append(PatternedRebarSet(
            index, spec, axes, window, (start - extension, end + extension), required,
            anchored, installed, count, rebar_mass_kg(spec.diameter, installed, count),
        ))
    return CompositeLayoutZone(zone_id, demand.direction, level_index, demand_bbox,
                               recipe, placement, tuple(components))


def evaluate_composite_zone(
    demand: DemandMap,
    zone: CompositeLayoutZone,
    *,
    constraints: LayoutConstraints | None = None,
) -> CompositeZoneEvaluation:
    """Пересчитать оси/анкеровку/раскрой/массу, не доверяя сохранённым счётчикам."""
    constraints = constraints or LayoutConstraints()
    diagnostics: list[str] = []
    try:
        recipe = _validate_request(demand, zone.level_index, zone.demand_bbox, zone.placement, constraints)
    except (ValueError, KeyError) as error:
        return CompositeZoneEvaluation(False, 0, 0.0, 0.0, (f"ERROR: {error}",))
    if zone.direction != demand.direction or zone.recipe != recipe or not zone.id.strip():
        diagnostics.append("ERROR: направление, схема или идентификатор зоны не согласованы с запросом")
    if len(zone.components) != len(recipe.additions):
        diagnostics.append("ERROR: потеряны либо добавлены компоненты схемы")
    window = transverse_interval(demand.direction.axis, zone.demand_bbox)
    start, end = longitudinal_interval(demand.direction.axis, zone.demand_bbox)
    required = end - start
    policy = FixedDiameterAnchoragePolicy(constraints.anchorage_diameters)
    masses, lengths, count, run_count = [], [], 0, 0
    for index, (spec, axes) in enumerate(zip(recipe.additions, zone.placement.additions)):
        try:
            runs = pattern_runs(axes, window)
            quantity = sum(run.bar_count for run in runs)
            if not quantity:
                raise ValueError("нет осей в выбранном окне")
            anchored = required + 2 * policy.extension_each_end_mm(spec)
            installed = select_installed_length_mm(anchored, constraints.allowed_cut_lengths_mm)
            mass = rebar_mass_kg(spec.diameter, installed, quantity)
        except ValueError as error:
            diagnostics.append(f"ERROR: добавка {index + 1}: {error}")
            continue
        masses.append(mass)
        lengths.append(installed * quantity)
        count += quantity
        run_count += len(runs)
        if index >= len(zone.components):
            continue
        component = zone.components[index]
        if (component.component_index != index or component.rebar != spec
                or component.placement != axes or component.axis_window_mm != window):
            diagnostics.append(f"ERROR: добавка {index + 1}: спецификация/фаза/окно не соответствует схеме")
        extension = (installed - required) / 2
        expected = (required, anchored, installed, start - extension, end + extension, mass)
        actual = (component.required_length_mm, component.anchored_length_mm, component.installed_length_mm,
                  *component.longitudinal_interval_mm, component.mass_kg)
        if (len(actual) != len(expected)
                or any(not math.isfinite(a) or not math.isclose(a, b, rel_tol=0, abs_tol=1e-6)
                       for a, b in zip(actual, expected))):
            diagnostics.append(f"ERROR: добавка {index + 1}: длины/анкеровка/bbox/масса не воспроизводятся")
        if not isinstance(component.bar_count, int) or isinstance(component.bar_count, bool) or component.bar_count != quantity:
            diagnostics.append(f"ERROR: добавка {index + 1}: количество не соответствует фактическим осям")
    diagnostics.append("NOT_CHECKED: покрытие КЭ, минимальная ширина по фактическим наборам, "
                       "нормативные сочетания, host/проёмы/cover и 3D-укладка")
    return CompositeZoneEvaluation(
        not any(d.startswith("ERROR:") for d in diagnostics), count, math.fsum(masses),
        math.fsum(lengths), tuple(diagnostics), component_count=len(recipe.additions), uniform_run_count=run_count,
    )
