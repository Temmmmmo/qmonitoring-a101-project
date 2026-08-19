"""Интеграция ручной таблицы с четырьмя реальными DXF плиты нуля."""

from rebar import Axis, Direction, Layer
from rebar.dxf_ingest import read_mosaic
from rebar.optimization import PLATE_ZERO_D12, apply_rebar_mapping, build_layout_problem


def test_plate_zero_mapping_builds_four_layout_problems(plate_zero_dxf_files):
    assert len(plate_zero_dxf_files) == 4
    directions = set()

    for path in plate_zero_dxf_files:
        source = read_mosaic(str(path))
        assert source.legend == []

        mapped = apply_rebar_mapping(source, PLATE_ZERO_D12)
        problem = build_layout_problem(mapped)

        assert len(mapped.legend) == 6
        assert all(cell.band is not None for cell in mapped.cells)
        assert len(problem.demand.levels) == 6
        assert problem.demand.meta["rebar_mapping"]["id"] == "plate-zero-d12-v1"
        directions.add(mapped.direction)

    assert directions == {
        Direction(Layer.BOTTOM, Axis.X),
        Direction(Layer.BOTTOM, Axis.Y),
        Direction(Layer.TOP, Axis.X),
        Direction(Layer.TOP, Axis.Y),
    }
