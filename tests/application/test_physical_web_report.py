from copy import deepcopy
from dataclasses import replace
import importlib.util
from pathlib import Path
import xml.etree.ElementTree as ET

import pytest

from rebar.application.physical_layout_recovery import recover_physical_layout
from rebar.application.physical_web_report import physical_web_report
from rebar.optimization.contracts.physical import PhysicalNormalizationConfig
from rebar.reporting.source_graphics import build_source_graphics, render_source_graphics_svg


@pytest.fixture(scope="module")
def physical_case():
    path = Path(__file__).with_name("test_patterned_layout_recovery.py")
    spec = importlib.util.spec_from_file_location("web_physical_fixture", path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    problem, solution, settings = fixture._source()
    recovery = recover_physical_layout(problem, solution, settings,
        normalization_config=PhysicalNormalizationConfig(time_limit_s=5, stock_balance_time_limit_s=2),
        balance_time_limit_s=5, stock_time_limit_s=3)
    assert recovery.packet is not None
    return problem, recovery


def test_displayed_geometry_mass_count_and_schedule_are_one_accepted_inventory(physical_case):
    problem, recovery = physical_case
    before = deepcopy(recovery)
    report = physical_web_report(problem, recovery)
    assert report["output_kind"] == "normalized-physical-bars"
    assert not report["placement_eligible"] and report["host_envelope"] is None
    point = report["front"][0]
    count = 0
    for direction in report["directions"]:
        candidate = direction["candidates"][0]
        assert candidate["zone_drafts"] == []
        assert direction["source_zone_drafts"]
        assert candidate["coverage"]["uncovered_cell_count"] == 0
        svg = ET.fromstring(candidate["svg"])
        lines = svg.findall(".//{http://www.w3.org/2000/svg}line")
        assert len(lines) == len(candidate["physical_bars"]) == candidate["metrics"]["physical_bar_count"]
        _, _, width, height = map(float, svg.attrib["viewBox"].split())
        for line, bar in zip(lines, candidate["physical_bars"]):
            x1, x2, y1, y2 = (float(line.attrib[key]) for key in ("x1", "x2", "y1", "y2"))
            assert abs(x2-x1)+abs(y2-y1) == pytest.approx(bar["longitudinal_mm"][1]-bar["longitudinal_mm"][0], abs=.0011)
            assert min(x1, x2, y1, y2) >= -.001
            assert max(x1, x2) <= width+.001 and max(y1, y2) <= height+.001
        assert not svg.findall(".//{http://www.w3.org/2000/svg}clipPath")
        count += len(lines)
    assert count == point["physical_bar_count"] == sum(row["physical_bar_count"] for row in point["bar_schedule"])
    assert point["additional_mass_kg"] == pytest.approx(sum(row["total_mass_kg"] for row in point["bar_schedule"]))
    assert "actual-3d-placement-and-layer-order" in report["blocking_check_ids"]
    assert report["physical_trial_packet"]["expected"] == recovery.review["expected"]
    assert recovery == before


@pytest.mark.parametrize("field,value", [("physical_bar_count", 1), ("additional_mass_kg", 1)])
def test_cannot_mix_geometry_and_metrics_from_different_stages(physical_case, field, value):
    problem, original = physical_case
    review = deepcopy(original.review)
    review["expected"][field] = value
    with pytest.raises(ValueError, match="Displayed physical inventory"):
        physical_web_report(problem, replace(original, review=review))


def test_rejected_physical_pipeline_is_labeled_as_source_only(physical_case):
    problem, recovery = physical_case
    report = physical_web_report(problem, replace(recovery, packet=None))
    assert report["output_kind"] == "source-zones-only"
    assert "промежуточный" in report["warning"]
    assert "physical_trial_packet" not in report
    assert report["default_drawing_view"] == "source"
    assert report["source_graphics"] and all(d["source_svg"] for d in report["directions"])


def test_original_source_graphics_are_not_normalized_bar_rectangles(physical_case):
    problem, recovery = physical_case
    report = physical_web_report(problem, recovery)
    packet = report["source_graphics"]
    assert packet["schema_version"] == "source-isofields-zones/v1"
    assert packet["source_stage"] == "original-parametric-zones-before-physical-normalization"
    assert packet["units"] == "mm" and not packet["placement_eligible"] and not packet["engineering_approval"]
    assert packet["case_id"] == problem.case_id and len(packet["directions"]) == 4
    assert report["original_source_zones"] == recovery.packet["source_zones"]
    point = recovery.patterned_report["front"][recovery.patterned_report["selected_index"]]
    for index, (source, original, direction) in enumerate(zip(packet["directions"], problem.direction_problems,
                                                            report["directions"])):
        prior = recovery.patterned_report["directions"][index]["candidates"][point["direction_candidate_indexes"][index]]
        assert source["zone_drafts"] == prior["zone_drafts"] == direction["source_zone_drafts"]
        assert len(source["cells"]) == len(original.demand.cells)
        for cell, expected in zip(source["cells"], original.demand.cells):
            assert cell == {"cell_id": expected.id, "polygon_mm": [list(p) for p in expected.poly],
                            "aci": expected.aci, "level_index": expected.level_index}
        assert [row["level_index"] for row in source["legend"]] == [level.index for level in original.demand.levels]
        svg = ET.fromstring(direction["source_svg"])
        assert len(svg.findall(".//{http://www.w3.org/2000/svg}polygon")) == len(original.demand.cells)
        ns = "{http://www.w3.org/2000/svg}"
        assert len(svg.findall(f'.//{ns}g[@class="source-zones"]/{ns}g/{ns}rect')) == len(prior["zone_drafts"])
        assert len(svg.findall(f'.//{ns}g[@class="source-component-envelopes"]/{ns}rect')) == sum(
            len(zone["components"]) for zone in prior["zone_drafts"])
        assert not svg.findall(".//{http://www.w3.org/2000/svg}line")
        physical = direction["candidates"][0]
        assert "class=\"source-zones\"></g>" in physical["svg"]
        assert physical["zone_drafts"] == []
    metrics = report["source_zone_metrics"]
    assert metrics["source_zone_count"] == sum(len(d["zone_drafts"]) for d in packet["directions"])
    assert metrics["physical_bar_count_before_normalization"] == sum(c["bar_count"] for d in packet["directions"]
        for z in d["zone_drafts"] for c in z["components"])


def test_source_packet_is_independent_copy_sanitizes_provenance_and_escapes_svg(physical_case):
    problem, recovery = physical_case
    report = deepcopy(recovery.patterned_report)
    candidate = report["directions"][0]["candidates"][report["front"][report["selected_index"]]["direction_candidate_indexes"][0]]
    candidate["zone_drafts"][0]["source_zone_id"] = '<unsafe & "quoted">'
    packet = build_source_graphics(problem, report, source_files=({"role": "dxf", "sha256": "a"*64,
        "path": "/private/tmp/secret-real.dxf", "private": "do not export"},))
    assert packet["source_files"] == [{"role": "dxf", "sha256": "a"*64, "filename": "secret-real.dxf"}]
    assert packet["provenance_status"] == "sha256_recorded"
    source = packet["directions"][0]
    svg = render_source_graphics_svg(source)
    parsed = ET.fromstring(svg)
    assert '<unsafe' not in svg
    assert parsed.find('.//{http://www.w3.org/2000/svg}g[@data-zone-id]').attrib["data-zone-id"] == '<unsafe & "quoted">'
    source["zone_drafts"][0]["components"][0]["bar_count"] = 123456
    assert candidate["zone_drafts"][0]["components"][0]["bar_count"] != 123456
    unverified = build_source_graphics(problem, report, source_files=({"role": "dxf",
        "path": r"C:\private\original.dxf"},))
    assert unverified["provenance_status"] == "unverified"
    assert unverified["source_files"] == [{"role": "dxf", "filename": "original.dxf"}]


@pytest.mark.parametrize("bad", ["direction", "count", "selection", "physical_inventory", "nan", "case"])
def test_source_packet_refuses_mixed_candidate_or_fabricated_source_geometry(physical_case, bad):
    problem, recovery = physical_case
    report = deepcopy(recovery.patterned_report)
    candidate = report["directions"][0]["candidates"][report["front"][report["selected_index"]]["direction_candidate_indexes"][0]]
    if bad == "direction":
        report["directions"][0]["direction"] = {"layer": "top", "axis": "Y"}
    elif bad == "count":
        candidate["metrics"]["zone_count"] += 1
    elif bad == "selection":
        report["selected_index"] = True
    elif bad == "physical_inventory":
        candidate["zone_drafts"] = []
    elif bad == "nan":
        candidate["zone_drafts"][0]["demand_bbox_mm"][0] = float("nan")
    else:
        report["case_id"] = "different-source-case"
    with pytest.raises(ValueError):
        build_source_graphics(problem, report)


def test_original_source_svg_does_not_clip_rectangle_outside_FE_or_stale_bbox(physical_case):
    problem, recovery = physical_case
    packet = build_source_graphics(problem, recovery.patterned_report)
    direction = deepcopy(packet["directions"][0])
    direction["zone_drafts"][0]["demand_bbox_mm"] = [-20000, -15000, 30000, 40000]
    drawing = ET.fromstring(render_source_graphics_svg(direction))
    vx, vy, width, height = map(float, drawing.attrib["viewBox"].split())
    for rect in drawing.findall(".//{http://www.w3.org/2000/svg}rect"):
        x, y, w, h = (float(rect.attrib[k]) for k in ("x", "y", "width", "height"))
        assert vx <= x <= x+w <= vx+width and vy <= y <= y+h <= vy+height
    assert not drawing.findall(".//{http://www.w3.org/2000/svg}clipPath")
