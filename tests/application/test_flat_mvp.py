from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from shapely.geometry import Polygon, box

from rebar.application.analyze_direction import load_direction_mosaic
from rebar.application.flat_mvp import (FlatMvpLayers, flat_mvp_domain, flat_mvp_elevations,
                                        flat_mvp_source_web_report, flat_mvp_web_report)
from rebar.application.s1_example import SOURCES, metadata
from rebar.models import Axis, Direction, Layer
from rebar.optimization.mappings.legacy_s1 import LEGACY_S1_D18
from test_boundary_trim_web import trim_case  # noqa: F401


def _problem(polygons):
    return SimpleNamespace(case_id="test-flat", direction_problems=tuple(
        SimpleNamespace(demand=SimpleNamespace(cells=[SimpleNamespace(poly=list(p.exterior.coords))
                                                     for p in polygons])) for _ in range(4)))


def test_domain_fills_hole_but_preserves_external_notch_and_original_demand():
    # A 3x3 ring, plus an external notch in its upper-right cell.
    tiles = [box(x, y, x+10, y+10) for x in (0, 10, 20) for y in (0, 10, 20)
             if (x, y) not in ((10, 10), (20, 20))]
    problem = _problem(tiles)
    before = deepcopy(problem)
    host, declaration = flat_mvp_domain(problem)
    footprint = host.sections[0].footprint
    assert footprint.covers(box(10, 10, 20, 20))
    assert not footprint.covers(box(20, 20, 30, 30))
    assert declaration["ignored_mesh_hole_count"] == 1
    assert declaration["ignored_mesh_hole_area_mm2"] == 100
    assert problem == before
    assert len(host.sections) == 1 and host.top_cover_mm == host.bottom_cover_mm == host.side_cover_mm == 0
    assert not declaration["actual_Revit_host_checked"]


def test_disconnected_or_mismatched_mesh_is_not_silently_replaced_by_rectangle():
    with pytest.raises(ValueError, match="connected"):
        flat_mvp_domain(_problem([box(0, 0, 10, 10), box(20, 0, 30, 10)]))
    problem = _problem([box(0, 0, 10, 10)])
    problem.direction_problems[2].demand.cells[0].poly = list(box(0, 0, 11, 10).exterior.coords)
    with pytest.raises(ValueError, match="same exact"):
        flat_mvp_domain(problem)


@pytest.mark.parametrize("diameter", [18, 20, 25, 28, 32, 36])
def test_s1_diameters_use_declared_layers_without_k09_d16_limit(diameter):
    host, _ = flat_mvp_domain(_problem([box(0, 0, 100, 100)]))
    for layer in Layer:
        x = flat_mvp_elevations(host, Direction(layer, Axis.X), diameter, FlatMvpLayers())[0]
        y = flat_mvp_elevations(host, Direction(layer, Axis.Y), diameter, FlatMvpLayers())[0]
        assert abs(x-y) == 36
        assert diameter/2 <= x <= 800-diameter/2


def test_whole_web_report_keeps_original_fe_and_separate_actual40d(trim_case):  # noqa: F811
    problem, recovery, _ = trim_case
    report = flat_mvp_web_report(problem, recovery, stock_time_limit_s=1)
    assert "working_host" not in report and "physical_trial_packet" not in report
    assert report["mvp_checks"]["openings"] == "out_of_scope"
    assert report["mvp_checks"]["height_irregularities"] == "out_of_scope"
    assert report["mvp_checks"]["outer_boundary"] == "pass"
    assert report["mvp_checks"]["control_40d"] == "fail"
    assert not report["boundary_trim"]["source_demand_removed"]
    assert not report["placement_eligible"]
    packet = report["graphic_bar_plan_draft"]
    assert "MVP FLAT" in packet["binding_source"]
    assert packet["checks"]["collisions_3d"] == {"status": "not_checked",
        "proven_pair_count": None, "uncertain_pair_count": None}
    assert packet["after"]["physical_bar_count"] == report["front"][0]["physical_bar_count"]


def test_source_web_report_exposes_diagnostic_source_when_batch_cut_fails(trim_case):  # noqa: F811
    problem, recovery, _ = trim_case
    source = json.loads(recovery.patterned_report_bytes)
    assert source["diagnostic_front_before_cutting"]
    source["front"] = []
    source["selected_index"] = None
    report = flat_mvp_source_web_report(problem, source, stock_time_limit_s=1)
    assert report["schema_version"] == "composite-plate-analysis/v1"
    assert report["front"] and report["front"][0]["direction_candidate_indexes"] == [0, 0, 0, 0]
    assert len(report["source_geometry_coverage"]) == 4
    assert all(row["status"] == "pass" and row["uncovered_cell_count"] == 0
               for row in report["source_geometry_coverage"])
    assert len(report["directions"]) == 4
    assert all(row["source_zone_drafts"] and len(row["candidates"]) == 1 for row in report["directions"])
    assert report["source_demand_preserved"] and report["source_graphics_candidate_index"] == 0
    assert report["front_scope"] == "source-geometry-only-stock-separately-checked"
    assert "physical_trial_packet" not in report and "working_host" not in report
    assert report["placement_eligible"] is False and report["mvp_checks"]["actual_Revit_geometry"] == "not_checked"


def test_source_web_report_rejects_tampered_geometry_and_preserves_nonzero_selection(trim_case):  # noqa: F811
    problem, recovery, _ = trim_case
    source = json.loads(recovery.patterned_report_bytes)
    assert source["front"] and source["selected_index"] == 0
    indexes = source["front"][0]["direction_candidate_indexes"]
    assert indexes == [1, 1, 1, 1]
    report = flat_mvp_source_web_report(problem, source, stock_time_limit_s=1)
    assert report["front"][0]["direction_candidate_indexes"] == [0, 0, 0, 0]
    assert all(row["candidates"][0]["candidate_index"] == 1 for row in report["directions"])
    assert report["source_graphics_candidate_index"] == 0
    source["directions"][0]["candidates"][indexes[0]]["zone_drafts"][0]["demand_bbox_mm"][2] += 1
    with pytest.raises(ValueError):
        flat_mvp_source_web_report(problem, source, stock_time_limit_s=1)


def test_s1_metadata_does_not_borrow_combined_pdf_or_k09_totals():
    entry = metadata(available=True, status="ready")
    assert all(entry["reference"][key] is None for key in ("mass_kg", "physical_bar_count", "position_count"))
    assert not entry["supports_working_host_trim"]
    assert len(entry["sources"]) == 4


def test_four_original_s1_dxf_match_the_png_mapping_and_have_no_internal_mesh_holes():
    root = Path(__file__).resolve().parents[2] / "Дополнительные материалы/Для верификации изополей/1-КЖ00.С1-2"
    if not root.is_dir():
        pytest.skip("Private A101 sources not installed")
    from hashlib import sha256
    from shapely.ops import unary_union
    for layer, axis, name, digest in SOURCES:
        path = root / name
        assert sha256(path.read_bytes()).hexdigest() == digest
        mosaic = load_direction_mosaic(path, mapping_id=LEGACY_S1_D18.id)
        assert str(mosaic.direction) == f"{layer}-{axis}"
        assert len(mosaic.cells) == 3680 and len(mosaic.legend) == 9
        shape = unary_union([Polygon(cell.poly) for cell in mosaic.cells])
        assert shape.geom_type == "Polygon" and not shape.interiors
        assert mosaic.legend[0].background.diameter == 18
        assert mosaic.legend[-1].additional.diameter == 36
