from copy import deepcopy
import json

import pytest

from rebar.models import Cell
from rebar.application.composite_host_review import HOST_COORDINATE_POLICY
from rebar.application.composite_layout_review import prepare_composite_review
from rebar.application.optimize_composite_layout import optimize_composite_layout
from rebar.dxf_ingest import read_mosaic

from test_composite_host_review import reference_sample
from test_composite_layout_review import ROOT, recipe_mosaic
from test_optimize_composite_layout import config_sample


def partial_mosaic():
    mosaic = recipe_mosaic()
    mosaic.cells.append(Cell([(-2000, 0), (-1600, 0), (-1600, 400), (-2000, 400)],
                             (-1800, 200), 2, mosaic.legend[0]))
    return mosaic


def test_partial_application_keeps_full_review_red_and_unknown_engineer_comparison():
    pytest.importorskip("scipy")
    mosaic, config, reference = partial_mosaic(), config_sample(), reference_sample()
    before = deepcopy((mosaic, config, reference))
    result = optimize_composite_layout(mosaic, config, host_reference=reference, coordinate_policy=HOST_COORDINATE_POLICY,
                                      host_policy="interior-exceptions", maximum_candidates=16)
    assert result["status"] == "partial_research_front_found"
    assert result["selected_review"]["status"] == "needs_revision" and not result["placement_eligible"]
    assert result["interior_partition"]["boundary_exception_cell_count"] == 1
    for point in result["front"]:
        assert point["uncovered_cell_count"] == 1 and point["target_uncovered_cell_count"] == 0
        assert point["original_uncovered_area_mm2"] == 160000 and point["target_cell_count"] == 2
        assert point["full_solution_mass_kg"] is None and point["specification_position_count"] is None
        assert point["engineer_comparison"]["status"] == "not_checked"
        assert point["engineer_comparison"]["mass_gate_passed"] is None
        assert point["stock_length_and_splices_approval"] == "not_checked"
        assert point["maximum_installed_bar_length_mm"] == 5900
    json.dumps(result, allow_nan=False)
    assert (mosaic, config, reference) == before


def test_empty_interior_application_cannot_advertise_a_zero_mass_solution():
    mosaic = partial_mosaic()
    mosaic.cells = mosaic.cells[-1:]
    result = optimize_composite_layout(mosaic, config_sample(), host_reference=reference_sample(),
                                      coordinate_policy=HOST_COORDINATE_POLICY, host_policy="interior-exceptions")
    assert result["status"] == "no_interior_demand"
    assert result["front"] == [] and result["selected_review"] is None and result["selected_index"] is None
    assert not result["optimizer_executed"] and result["interior_partition"]["boundary_exception_cell_count"] == 1


def test_actual_051_whole_cell_interior_is_partial_and_not_a_good_engineer_match():
    pytest.importorskip("scipy")
    folder = ROOT / "Дополнительные материалы/Изополя(мозаики) армирования/2 фона"
    dxf, shk = folder / "Верхнее армирование вдоль ОСИ Х.dxf", folder / "К09_фп_2 фона_Вх.shk"
    reference_path = ROOT / "revit_info/051/qmonitoring-reference-20260910-095803-020000.json"
    if not all(path.exists() for path in (dxf, shk, reference_path)):
        pytest.skip("private source and Revit 051 reference unavailable")
    config = config_sample()
    config["additional_origins_by_level"] = {"1": [0], "2": [0, -50], "3": [0, -50]}
    mosaic = read_mosaic(str(dxf), str(shk))
    original, _, _ = prepare_composite_review(mosaic, config)
    result = optimize_composite_layout(mosaic, config, host_reference=json.loads(reference_path.read_text()),
        coordinate_policy=HOST_COORDINATE_POLICY, host_policy="interior-exceptions", maximum_candidates=16,
        maximum_zones=64, partition_depth=8, solver_time_limit_s=5)
    partition = result["interior_partition"]
    assert (len(original.cells), partition["original_demanded_cell_count"], partition["target_cell_count"]) == (2132, 1341, 1210)
    assert partition["boundary_exception_cell_count"] == 131
    assert partition["common_target_bbox_mm"] == pytest.approx((1025, 100, 22575, 13900), abs=0.01)
    assert result["status"] == "partial_research_front_found" and not result["placement_eligible"]
    point = result["front"][result["selected_index"]]
    assert point["zone_count"] == 1 and point["physical_bar_count"] == 178
    assert point["additional_mass_kg"] == pytest.approx(12057.86401568)
    assert point["maximum_installed_bar_length_mm"] == pytest.approx(23348.126)
    assert point["uncovered_cell_count"] == 131 and point["target_uncovered_cell_count"] == 0
    assert point["host_preflight"]["invalid_bar_count"] == 0
    assert point["engineer_comparison"]["mass_gate_passed"] is None
