"""Человекочитаемые марки и выноски параметрических зон."""

from __future__ import annotations

from dataclasses import dataclass

from rebar.models import Direction, Layer
from rebar.optimization import LayoutSolution, LayoutZone


@dataclass(frozen=True)
class ZoneScheduleRow:
    """Одна строка ведомости зон без привязки к HTML или Revit API."""

    mark: str
    zone_id: str
    callout: str
    level_index: int
    diameter_mm: int
    step_mm: int
    bar_count: int
    width_mm: float
    required_length_mm: float
    anchored_length_mm: float
    installed_length_mm: float
    mass_kg: float


def _number(value: float | int) -> str:
    rounded = round(float(value), 3)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.3f}".rstrip("0").rstrip(".")


def _zone_sort_key(zone: LayoutZone) -> tuple[float, float, float, float, int, str]:
    xmin, ymin, xmax, ymax = zone.demand_bbox
    return (ymin, xmin, ymax, xmax, zone.level_index, zone.id)


def direction_mark_prefix(direction: Direction) -> str:
    """Вернуть короткий русский префикс слоя и оси: ``Н-X``/``В-Y``."""

    layer = "Н" if direction.layer is Layer.BOTTOM else "В"
    return f"{layer}-{direction.axis.value}"


def build_zone_schedule(
    direction: Direction,
    solution: LayoutSolution,
) -> tuple[ZoneScheduleRow, ...]:
    """Сформировать стабильные марки и выноски для всех зон решения."""

    prefix = direction_mark_prefix(direction)
    rows = []
    for index, zone in enumerate(sorted(solution.zones, key=_zone_sort_key), 1):
        mark = f"{prefix}-{index:03d}"
        callout = (
            f"⌀{zone.rebar.diameter}-{_number(zone.installed_length_mm)}, "
            f"шаг {zone.rebar.step} ({zone.bar_count} шт.)"
        )
        rows.append(
            ZoneScheduleRow(
                mark=mark,
                zone_id=zone.id,
                callout=callout,
                level_index=zone.level_index,
                diameter_mm=zone.rebar.diameter,
                step_mm=zone.rebar.step,
                bar_count=zone.bar_count,
                width_mm=zone.width_mm,
                required_length_mm=zone.required_length_mm,
                anchored_length_mm=zone.anchored_length_mm,
                installed_length_mm=zone.installed_length_mm,
                mass_kg=zone.mass_kg,
            )
        )
    return tuple(rows)
