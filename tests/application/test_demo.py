"""Проверки публичного синтетического DXF-примера."""

from rebar.application import (
    IRREGULAR_PLATE_DEMO,
    analyze_direction,
    available_demo_cases,
    get_demo_case,
    write_demo_dxf,
)
from rebar.models import Axis, Layer


def test_demo_dxf_passes_real_ingest_mapping_and_optimizer(tmp_path):
    destination = tmp_path / IRREGULAR_PLATE_DEMO.filename

    case = write_demo_dxf(IRREGULAR_PLATE_DEMO.id, destination)
    analysis = analyze_direction(
        destination,
        mapping_id=case.mapping_id,
        algorithm_names=("bbox",),
        min_width_cells=1,
    )

    assert destination.is_file()
    assert len(analysis.mosaic.cells) == 96
    assert len(analysis.mosaic.legend) == 6
    assert {cell.aci for cell in analysis.mosaic.cells} == {1, 2, 3, 6, 30, 181}
    assert analysis.mosaic.direction.layer is Layer.BOTTOM
    assert analysis.mosaic.direction.axis is Axis.X
    assert analysis.mosaic.meta["unit_scale_to_mm"] == 1.0
    assert analysis.mosaic.meta["rebar_mapping"]["id"] == "plate-zero-d12-v1"
    assert analysis.solutions[0].metrics.under_reinforced_cell_count == 0


def test_demo_catalog_has_stable_default():
    assert available_demo_cases() == (IRREGULAR_PLATE_DEMO,)
    assert get_demo_case("IRREGULAR-PLATE-X") is IRREGULAR_PLATE_DEMO
