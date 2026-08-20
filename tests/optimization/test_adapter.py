"""Тесты границы ``Mosaic -> LayoutProblem``."""

import pytest

from rebar import Axis, Direction, Layer, Rebar
from rebar.optimization import (
    MissingRebarSpecificationError,
    build_demand_map,
    build_layout_problem,
)


def test_build_demand_map_preserves_geometry_and_levels(mosaic_with_legend):
    demand = build_demand_map(mosaic_with_legend)

    assert demand.direction == Direction(Layer.BOTTOM, Axis.X)
    assert demand.bbox == (0, 0, 1000, 500)
    assert [cell.level_index for cell in demand.cells] == [0, 1]
    assert demand.cells[1].poly == ((500, 0), (1000, 0), (1000, 500), (500, 500))
    assert demand.level(0).requires_extra is False
    assert demand.level(1).additional == Rebar(step=100, diameter=25)
    assert demand.meta["demand_mapping"] == "legend"


def test_build_layout_problem_uses_common_constraints(mosaic_with_legend):
    problem = build_layout_problem(mosaic_with_legend)

    assert problem.case_id == "Нижнее армирование вдоль ОСИ Х"
    assert problem.constraints.min_width_cells == 2
    assert problem.constraints.anchorage_diameters == 40
    assert problem.constraints.allow_overlaps is True
    assert problem.constraints.enforce_zone_gap is True


def test_as_only_mosaic_can_be_visualized_but_not_detailed(mosaic_with_legend):
    mosaic_with_legend.legend = []
    for cell in mosaic_with_legend.cells:
        cell.band = None

    demand = build_demand_map(mosaic_with_legend)
    assert [level.requires_extra for level in demand.levels] == [None, None]
    assert demand.meta["demand_mapping"] == "as_intervals_only"

    with pytest.raises(MissingRebarSpecificationError, match="нет назначенных"):
        build_layout_problem(mosaic_with_legend)


def test_unknown_aci_is_rejected_instead_of_becoming_background(mosaic_with_legend):
    mosaic_with_legend.cells[0].band = None
    mosaic_with_legend.cells[0].aci = 77

    with pytest.raises(ValueError, match=r"\[77\]"):
        build_demand_map(mosaic_with_legend)
