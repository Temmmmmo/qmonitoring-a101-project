"""Позиции прямых стержней: типоразмер, а не зона или регулярный Revit-ряд.

Округление длины до 1e-6 мм убирает только вычислительный шум. Оно не меняет
геометрию и не является укрупнением/округлением производственных длин. Пустой класс
означает неизвестный единый класс входа, не молчаливое назначение A500.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import math

from ..contracts.placement import CompositeLayoutZone
from ..contracts.result import LayoutZone
from .detailing import rebar_mass_kg

PositionKey = tuple[str, str, int, float]


def straight_bar_key(diameter_mm: int, length_mm: float, steel_class: str = "") -> PositionKey:
    if (isinstance(diameter_mm, bool) or not isinstance(diameter_mm, int)
            or diameter_mm <= 0 or isinstance(length_mm, bool)
            or not math.isfinite(length_mm) or length_mm <= 0):
        raise ValueError("позиция требует положительные диаметр и конечную длину")
    if not isinstance(steel_class, str):
        raise ValueError("класс стали должен быть строкой; пустая строка = не задан")
    return ("straight", steel_class.strip(), diameter_mm, round(length_mm, 6))


def zone_position_keys(zones: Iterable[LayoutZone], steel_class: str = "") -> frozenset[PositionKey]:
    return frozenset(
        straight_bar_key(zone.rebar.diameter, zone.installed_length_mm, steel_class)
        for zone in zones
    )


@dataclass(frozen=True)
class BarScheduleGroup:
    source_id: str
    diameter_mm: int
    installed_length_mm: float
    physical_bar_count: int
    steel_class: str = ""


@dataclass(frozen=True)
class BarSchedulePosition:
    mark: str
    shape: str
    steel_class: str
    diameter_mm: int
    length_mm: float
    physical_bar_count: int
    total_mass_kg: float
    source_ids: tuple[str, ...]


def build_bar_schedule(groups: Iterable[BarScheduleGroup]) -> tuple[BarSchedulePosition, ...]:
    """Группировать, не менять оси/длины; массу независимо считать по каждому набору."""
    grouped: dict[PositionKey, list[BarScheduleGroup]] = {}
    source_ids: set[str] = set()
    for group in groups:
        if not group.source_id or group.source_id in source_ids:
            raise ValueError("source_id набора должен быть непустым и уникальным")
        source_ids.add(group.source_id)
        if (isinstance(group.physical_bar_count, bool)
                or not isinstance(group.physical_bar_count, int) or group.physical_bar_count < 1):
            raise ValueError("количество физических стержней должно быть положительным целым")
        key = straight_bar_key(group.diameter_mm, group.installed_length_mm, group.steel_class)
        grouped.setdefault(key, []).append(group)
    return tuple(
        BarSchedulePosition(
            mark=f"П{index + 1}", shape=key[0], steel_class=key[1],
            diameter_mm=key[2], length_mm=key[3],
            physical_bar_count=sum(group.physical_bar_count for group in members),
            total_mass_kg=math.fsum(
                rebar_mass_kg(group.diameter_mm, group.installed_length_mm, group.physical_bar_count)
                for group in members
            ),
            source_ids=tuple(sorted(group.source_id for group in members)),
        )
        for index, (key, members) in enumerate(sorted(grouped.items()))
    )


def layout_schedule_groups(zones: Iterable[LayoutZone], *, prefix: str = "",
                           steel_class: str = "") -> tuple[BarScheduleGroup, ...]:
    return tuple(BarScheduleGroup(
        f"{prefix}{zone.id}", zone.rebar.diameter, zone.installed_length_mm,
        zone.bar_count, steel_class,
    ) for zone in zones)


def composite_schedule_groups(zones: Iterable[CompositeLayoutZone], *,
                              steel_class: str = "") -> tuple[BarScheduleGroup, ...]:
    # Компонент учитывается один раз, независимо от числа технических UniformBarRun.
    return tuple(BarScheduleGroup(
        f"{zone.direction}:{zone.id}:{component.component_index}",
        component.rebar.diameter, component.installed_length_mm,
        component.bar_count, steel_class,
    ) for zone in zones for component in zone.components)
