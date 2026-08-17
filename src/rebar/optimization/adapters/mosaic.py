"""Адаптер результата ingest ``Mosaic`` в задачу оптимизации."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rebar.models import Mosaic

from ..contracts import DemandCell, DemandLevel, DemandMap, LayoutConstraints, LayoutProblem


class MissingRebarSpecificationError(ValueError):
    """В мозаике есть спрос, но источник не задаёт диаметр/шаг добавки."""


def _scale_intervals(mosaic: Mosaic) -> dict[int, dict[str, Any]]:
    intervals = mosaic.meta.get("scale_intervals") or []
    return {
        int(interval["index"]): interval
        for interval in intervals
        if isinstance(interval, dict) and "index" in interval
    }


def _levels_from_legend(mosaic: Mosaic) -> tuple[DemandLevel, ...]:
    intervals = _scale_intervals(mosaic)
    ordered = sorted(mosaic.legend, key=lambda band: band.index)
    if [band.index for band in ordered] != list(range(len(ordered))):
        raise ValueError("индексы легенды должны идти подряд от нуля")

    return tuple(
        DemandLevel(
            index=band.index,
            aci=band.aci,
            lower_as=intervals.get(band.index, {}).get("lower_as"),
            upper_as=intervals.get(band.index, {}).get("upper_as", band.threshold_as),
            label=band.label,
            additional=band.additional,
            requires_extra=band.additional is not None,
        )
        for band in ordered
    )


def _levels_from_intervals(mosaic: Mosaic) -> tuple[DemandLevel, ...]:
    intervals = sorted(_scale_intervals(mosaic).values(), key=lambda item: int(item["index"]))
    if not intervals:
        raise ValueError("в мозаике нет ни legend, ни scale_intervals")
    indexes = [int(interval["index"]) for interval in intervals]
    if indexes != list(range(len(indexes))):
        raise ValueError("индексы интервалов As должны идти подряд от нуля")

    return tuple(
        DemandLevel(
            index=int(interval["index"]),
            aci=int(interval["aci"]),
            lower_as=float(interval["lower_as"]),
            upper_as=float(interval["upper_as"]),
            label=None,
            additional=None,
            requires_extra=None,
        )
        for interval in intervals
    )


def build_demand_map(mosaic: Mosaic) -> DemandMap:
    """Преобразовать ``Mosaic`` в независимое от ingest поле требований."""

    levels = _levels_from_legend(mosaic) if mosaic.legend else _levels_from_intervals(mosaic)
    index_by_aci = {level.aci: level.index for level in levels if level.aci is not None}
    if len(index_by_aci) != len(levels):
        raise ValueError("ACI уровней спроса должны быть известны и уникальны")

    cells: list[DemandCell] = []
    unknown_aci: set[int] = set()
    for cell_id, cell in enumerate(mosaic.cells):
        level_index = cell.band.index if cell.band is not None else index_by_aci.get(cell.aci)
        if level_index is None:
            unknown_aci.add(cell.aci)
            continue
        cells.append(
            DemandCell(
                id=cell_id,
                poly=tuple(cell.poly),
                centroid=cell.centroid,
                aci=cell.aci,
                level_index=level_index,
            )
        )

    if unknown_aci:
        raise ValueError(f"цвета КЭ отсутствуют в шкале: {sorted(unknown_aci)}")

    meta = dict(mosaic.meta)
    meta["demand_mapping"] = "legend" if mosaic.legend else "as_intervals_only"
    return DemandMap(
        direction=mosaic.direction,
        levels=levels,
        cells=tuple(cells),
        bbox=mosaic.bbox,
        source_path=mosaic.source_path,
        meta=meta,
    )


def build_layout_problem(
    mosaic: Mosaic,
    constraints: LayoutConstraints | None = None,
) -> LayoutProblem:
    """Построить детализируемую задачу и явно отклонить неизвестные спецификации."""

    demand = build_demand_map(mosaic)
    unknown = [level.index for level in demand.levels if level.requires_extra is None]
    if unknown:
        raise MissingRebarSpecificationError(
            "для уровней As нет назначенных диаметров/шагов; требуется .shk или таблица "
            f"подбора (уровни: {unknown})"
        )

    return LayoutProblem(
        demand=demand,
        constraints=constraints or LayoutConstraints(),
        case_id=Path(mosaic.source_path).stem if mosaic.source_path else "",
    )
