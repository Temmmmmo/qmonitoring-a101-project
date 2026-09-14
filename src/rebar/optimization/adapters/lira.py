"""Числовой спрос -> общий DemandMap по ЯВНО выбранной шкале, без Mosaic/ACI."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
import math

from shapely.geometry import Polygon

from rebar.lira.models import AS_DIRECTIONS, LiraPlate
from rebar.models import Band, Direction

from ..contracts.problem import DemandCell, DemandLevel, DemandMap
from ..services.reinforcement_area import recipe_area_cm2_m


def build_lira_demand_map(plate: LiraPlate, direction: Direction, bands: Sequence[Band], *,
                          mapping_source: str, export_axes_are_global_xy: bool = False) -> DemandMap:
    """Сохраняет реальные ID/As; назначает первый достаточный рецепт по порядку шкалы.

    Проверяется И порог шкалы, И фактическая средняя площадь её состава. Цветов
    в Excel нет: aci=0 у КЭ означает «не применимо», не цвет исходной мозаики.
    Разрешение на 3D-укладку из этой проверки не следует.
    """
    if direction not in AS_DIRECTIONS:
        raise ValueError("неизвестное направление AS1–AS4")
    if export_axes_are_global_xy is not True:
        raise ValueError("Подтвердите соответствие экспортных осей X/Y глобальным осям плиты")
    if any(e.local_axis_angle_deg not in (None, 0.0) for e in plate.elements):
        raise ValueError("Поворот локальных осей КЭ требует преобразования; простой X/Y-профиль неприменим")
    if not isinstance(mapping_source, str) or not mapping_source.strip():
        raise ValueError("требуется явный источник шкалы подбора")
    if not bands or [b.index for b in bands] != list(range(len(bands))):
        raise ValueError("шкала должна иметь последовательные индексы от нуля")
    thresholds = [b.threshold_as for b in bands]
    if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0
            for v in thresholds) or any(b <= a for a, b in zip(thresholds, thresholds[1:]))):
        raise ValueError("пороги шкалы должны быть конечными положительными и строго возрастать")
    recipes = [b.reinforcement_recipe for b in bands]
    if len({r.background for r in recipes}) != 1 or recipes[0].additions:
        raise ValueError("требуется единый явно заданный фон и первая полоса без добавки")
    capacities = [recipe_area_cm2_m(r) for r in recipes]
    if any(b < a for a, b in zip(capacities, capacities[1:])):
        raise ValueError("фактическая площадь рецептов не должна убывать по шкале")
    levels = tuple(DemandLevel(
        index=b.index, aci=None, lower_as=0.0 if b.index == 0 else thresholds[b.index - 1],
        upper_as=b.threshold_as, label=b.label, additional=b.additional,
        requires_extra=bool(recipe.additions), recipe=recipe,
    ) for b, recipe in zip(bands, recipes))
    cells, origins = [], []
    for element in plate.elements:
        required = element.required_as(direction)
        level = next((i for i, (threshold, capacity) in enumerate(zip(thresholds, capacities))
                      if required <= threshold and required <= capacity + 1e-10), None)
        if level is None:
            raise ValueError(f"КЭ {element.id}, {direction}: для As={required:g} см²/м нет достаточного рецепта")
        points = element.polygon_xy_mm
        centroid = Polygon(points).centroid
        cells.append(DemandCell(element.id, points, (centroid.x, centroid.y), 0, level))
        origins.append({
            "cell_id": element.id, "required_as_cm2_m": required,
            "group_id": element.group_id, "geometry_row": element.geometry_row,
            "reinforcement_rows": element.reinforcement_rows, "level_index": level,
            "provided_average_as_cm2_m": capacities[level],
        })
    lo, hi = plate.bbox_xyz_mm
    return DemandMap(direction, levels, tuple(cells), (lo[0], lo[1], hi[0], hi[1]),
        source_path=next(s.filename for s in plate.sources if s.role == "reinforcement"), meta={
            "source_kind": "lira_numeric_excel", "input_profile": plate.profile_id,
            "source_files": [asdict(s) for s in plate.sources], "units": "mm",
            "input_coordinate_unit": plate.input_coordinate_unit, "as_unit": "cm2/m",
            "aci_semantics": "not_applicable", "demand_mapping": "explicit_numeric_recipe_scale",
            "mapping_source": mapping_source, "export_axes_global_xy": "caller_confirmed",
            "z_mm": lo[2], "calculation_groups": [asdict(g) for g in plate.groups],
            "numeric_source_cells": origins, "averaging": "not_applied",
            "placement_eligible": False,
        })


def build_lira_plate_demands(plate: LiraPlate, scales: Mapping[Direction, Sequence[Band]], *,
                             mapping_sources: Mapping[Direction, str],
                             export_axes_are_global_xy: bool = False) -> tuple[DemandMap, ...]:
    if set(scales) != set(AS_DIRECTIONS) or set(mapping_sources) != set(AS_DIRECTIONS):
        raise ValueError("требуются явные шкалы и источники всех четырёх направлений")
    return tuple(build_lira_demand_map(
        plate, direction, scales[direction], mapping_source=mapping_sources[direction],
        export_axes_are_global_xy=export_axes_are_global_xy,
    ) for direction in AS_DIRECTIONS)
