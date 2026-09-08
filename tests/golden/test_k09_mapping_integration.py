"""Связь явных таблиц с полными четырёхнаправленными комплектами DXF."""

from __future__ import annotations

import pytest

from rebar import Axis, Direction, Layer
from rebar.dxf_ingest import read_mosaic
from rebar.golden import (
    GoldenSourceNotFoundError,
    get_engineer_reference_case,
    resolve_engineer_reference_files,
)
from rebar.optimization import (
    K09_ABOVE_3_D10,
    K09_MINUS_2_D12,
    RebarMappingError,
    apply_rebar_mapping,
    build_layout_problem,
)

ALL_DIRECTIONS = {
    Direction(Layer.BOTTOM, Axis.X),
    Direction(Layer.BOTTOM, Axis.Y),
    Direction(Layer.TOP, Axis.X),
    Direction(Layer.TOP, Axis.Y),
}


@pytest.fixture
def reference_files(data_dir):
    """Private DXF/PDF files are intentionally absent in a public checkout."""
    def resolve(case_id):
        case = get_engineer_reference_case(case_id)
        try:
            return resolve_engineer_reference_files(case, data_dir)
        except GoldenSourceNotFoundError as error:
            pytest.skip(str(error))
    return resolve


@pytest.mark.parametrize(
    ("case_id", "input_id", "mapping", "band_count"),
    [
        ("k09-minus-2", "k09-minus-2-input", K09_MINUS_2_D12, 6),
        ("k09-typical-3-14", "k09-above-3-input", K09_ABOVE_3_D10, 7),
    ],
)
def test_mapping_builds_four_real_layout_problems(
    reference_files,
    case_id,
    input_id,
    mapping,
    band_count,
):
    files = reference_files(case_id)
    paths = files.dxf_by_input_set[input_id]

    assert set(paths) == ALL_DIRECTIONS
    for direction, path in paths.items():
        source = read_mosaic(str(path))
        mapped = apply_rebar_mapping(source, mapping)
        problem = build_layout_problem(mapped)

        assert mapped.direction == direction
        assert len(mapped.legend) == band_count
        assert all(cell.band is not None for cell in mapped.cells)
        assert len(problem.demand.levels) == band_count
        assert problem.demand.meta["rebar_mapping"]["id"] == mapping.id


def test_above_3_mapping_rejects_unmapped_upper_ranges_of_plate_9(reference_files):
    files = reference_files("k09-typical-3-14")
    paths = files.dxf_by_input_set["k09-above-9-input"]

    for direction in (Direction(Layer.TOP, Axis.X), Direction(Layer.TOP, Axis.Y)):
        source = read_mosaic(str(paths[direction]))
        with pytest.raises(RebarMappingError, match="входная шкала — 8"):
            apply_rebar_mapping(source, K09_ABOVE_3_D10)
