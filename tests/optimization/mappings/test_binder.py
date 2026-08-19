"""Тесты связывания внешней таблицы с ``Mosaic`` без `.shk`."""

from __future__ import annotations

from copy import deepcopy

import pytest

from rebar import Axis, Cell, Direction, Layer, Mosaic
from rebar.optimization import (
    PLATE_ZERO_D12,
    RebarMappingError,
    apply_rebar_mapping,
    build_layout_problem,
)

_ACI_ORDER = (181, 51, 200, 31, 13, 1)
_BOUNDS = (1.9, 3.8, 7.5, 11.0, 17.0, 24.0, 35.0)


def _mosaic() -> Mosaic:
    intervals = [
        {
            "index": index,
            "aci": aci,
            "lower_as": _BOUNDS[index],
            "upper_as": _BOUNDS[index + 1],
        }
        for index, aci in enumerate(_ACI_ORDER)
    ]
    return Mosaic(
        direction=Direction(Layer.BOTTOM, Axis.X),
        cells=[
            Cell([(0, 0), (500, 0), (500, 500), (0, 500)], (250, 250), 181),
            Cell([(500, 0), (1000, 0), (1000, 500), (500, 500)], (750, 250), 1),
        ],
        legend=[],
        bbox=(0, 0, 1000, 500),
        source_path="Нижняя по Х.dxf",
        meta={"scale_intervals": intervals, "shk_path": None},
    )


def test_apply_rebar_mapping_returns_enriched_copy_and_builds_problem():
    source = _mosaic()

    mapped = apply_rebar_mapping(source, PLATE_ZERO_D12)
    problem = build_layout_problem(mapped)

    assert mapped is not source
    assert mapped.cells[0] is not source.cells[0]
    assert source.legend == []
    assert all(cell.band is None for cell in source.cells)
    assert "rebar_mapping" not in source.meta
    assert len(mapped.legend) == 6
    assert mapped.cells[0].band is mapped.legend[0]
    assert mapped.cells[1].band is mapped.legend[5]
    assert mapped.meta["rebar_mapping"] == {
        "id": "plate-zero-d12-v1",
        "source": PLATE_ZERO_D12.source,
        "status": "mvp_assumption",
        "scale_bounds_as": list(_BOUNDS),
    }
    assert problem.demand.levels[0].requires_extra is False
    assert all(level.requires_extra is True for level in problem.demand.levels[1:])
    assert problem.demand.meta["demand_mapping"] == "legend"


def test_mapping_does_not_overwrite_existing_legend():
    mapped = apply_rebar_mapping(_mosaic(), PLATE_ZERO_D12)

    with pytest.raises(RebarMappingError, match="уже есть легенда"):
        apply_rebar_mapping(mapped, PLATE_ZERO_D12)


def test_mapping_rejects_unknown_cell_aci():
    source = _mosaic()
    source.cells.append(
        Cell([(0, 500), (500, 500), (500, 1000), (0, 1000)], (250, 750), 254)
    )

    with pytest.raises(RebarMappingError, match=r"\[254\]"):
        apply_rebar_mapping(source, PLATE_ZERO_D12)


def test_mapping_rejects_additional_unmapped_level():
    source = _mosaic()
    source.meta["scale_intervals"].append(
        {"index": 6, "aci": 2, "lower_as": 35.0, "upper_as": 45.0}
    )

    with pytest.raises(RebarMappingError, match="содержит 6 полос.*входная шкала — 7"):
        apply_rebar_mapping(source, PLATE_ZERO_D12)


def test_mapping_rejects_different_scale_with_same_band_count():
    source = _mosaic()
    intervals = deepcopy(source.meta["scale_intervals"])
    intervals[2]["upper_as"] = 12.0
    intervals[3]["lower_as"] = 12.0
    source.meta["scale_intervals"] = intervals

    with pytest.raises(RebarMappingError, match="несовместима с границей шкалы"):
        apply_rebar_mapping(source, PLATE_ZERO_D12)


def test_mapping_rejects_duplicate_scale_aci():
    source = _mosaic()
    source.meta["scale_intervals"][1]["aci"] = 181

    with pytest.raises(RebarMappingError, match="должны быть уникальными"):
        apply_rebar_mapping(source, PLATE_ZERO_D12)
