"""Безопасное связывание ручной таблицы с результатом DXF-ingest."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from itertools import pairwise
from typing import Any

from rebar.models import Band, Cell, Mosaic

from .contracts import RebarMapping


class RebarMappingError(ValueError):
    """Таблицу нельзя однозначно применить к переданной мозаике."""


@dataclass(frozen=True)
class _ScaleInterval:
    index: int
    aci: int
    lower_as: float
    upper_as: float


def _read_scale_intervals(mosaic: Mosaic) -> tuple[_ScaleInterval, ...]:
    raw_intervals = mosaic.meta.get("scale_intervals")
    if not isinstance(raw_intervals, list) or not raw_intervals:
        raise RebarMappingError("в Mosaic.meta отсутствуют интервалы цветовой шкалы As")

    intervals: list[_ScaleInterval] = []
    for raw in raw_intervals:
        if not isinstance(raw, dict):
            raise RebarMappingError("интервал шкалы As должен быть словарём")
        try:
            interval = _ScaleInterval(
                index=int(raw["index"]),
                aci=int(raw["aci"]),
                lower_as=float(raw["lower_as"]),
                upper_as=float(raw["upper_as"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RebarMappingError(f"некорректный интервал шкалы As: {raw!r}") from error
        if not all(math.isfinite(value) for value in (interval.lower_as, interval.upper_as)):
            raise RebarMappingError("границы интервала As должны быть конечными числами")
        if interval.lower_as >= interval.upper_as:
            raise RebarMappingError("нижняя граница интервала As должна быть меньше верхней")
        if not 1 <= interval.aci <= 255:
            raise RebarMappingError(f"некорректный ACI цвет шкалы: {interval.aci}")
        intervals.append(interval)

    intervals.sort(key=lambda interval: interval.index)
    indexes = [interval.index for interval in intervals]
    if indexes != list(range(len(intervals))):
        raise RebarMappingError("индексы шкалы As должны идти подряд от нуля")
    aci_values = [interval.aci for interval in intervals]
    if len(set(aci_values)) != len(aci_values):
        raise RebarMappingError("ACI цвета уровней шкалы должны быть уникальными")
    for previous, current in pairwise(intervals):
        if not math.isclose(previous.upper_as, current.lower_as, abs_tol=1e-9):
            raise RebarMappingError("соседние интервалы шкалы As должны иметь общую границу")
    return tuple(intervals)


def _actual_scale_bounds(intervals: tuple[_ScaleInterval, ...]) -> tuple[float, ...]:
    return (intervals[0].lower_as, *(interval.upper_as for interval in intervals))


def _validate_compatibility(
    intervals: tuple[_ScaleInterval, ...],
    mapping: RebarMapping,
) -> None:
    if len(intervals) != len(mapping.bands):
        raise RebarMappingError(
            f"таблица {mapping.id!r} содержит {len(mapping.bands)} полос, "
            f"а входная шкала — {len(intervals)}"
        )

    actual_bounds = _actual_scale_bounds(intervals)
    for index, (actual, expected) in enumerate(
        zip(actual_bounds, mapping.expected_scale_bounds_as)
    ):
        if not math.isclose(
            actual,
            expected,
            rel_tol=0.0,
            abs_tol=mapping.scale_tolerance_as,
        ):
            raise RebarMappingError(
                f"таблица {mapping.id!r} несовместима с границей шкалы {index}: "
                f"получено {actual:g}, ожидалось {expected:g}"
            )


def _mapping_meta(mapping: RebarMapping, intervals: tuple[_ScaleInterval, ...]) -> dict[str, Any]:
    return {
        "id": mapping.id,
        "source": mapping.source,
        "status": mapping.status,
        "scale_bounds_as": list(_actual_scale_bounds(intervals)),
    }


def apply_rebar_mapping(mosaic: Mosaic, mapping: RebarMapping) -> Mosaic:
    """Вернуть копию ``Mosaic`` с заполненными существующими ``Band``.

    Функция не подменяет легенду из `.shk`: ручную таблицу разрешено применять только
    к результату ingest без назначений арматуры. ACI берутся из текущего DXF и
    связываются с правилами по стабильному порядку шкалы.
    """

    if mosaic.legend or any(cell.band is not None for cell in mosaic.cells):
        raise RebarMappingError(
            "в Mosaic уже есть легенда арматуры; ручная таблица не должна её перезаписывать"
        )

    intervals = _read_scale_intervals(mosaic)
    _validate_compatibility(intervals, mapping)

    bands = [
        Band(
            index=interval.index,
            aci=interval.aci,
            label=rule.label,
            threshold_as=rule.threshold_as,
            background=rule.background,
            additional=rule.additional,
        )
        for interval, rule in zip(intervals, mapping.bands)
    ]
    band_by_aci = {band.aci: band for band in bands}
    unknown_aci = sorted({cell.aci for cell in mosaic.cells if cell.aci not in band_by_aci})
    if unknown_aci:
        raise RebarMappingError(f"цвета КЭ отсутствуют в шкале: {unknown_aci}")

    mapped_cells = [
        Cell(
            poly=list(cell.poly),
            centroid=cell.centroid,
            aci=cell.aci,
            band=band_by_aci[cell.aci],
        )
        for cell in mosaic.cells
    ]
    meta = dict(mosaic.meta)
    meta["rebar_mapping"] = _mapping_meta(mapping, intervals)
    return replace(mosaic, cells=mapped_cells, legend=bands, meta=meta)
