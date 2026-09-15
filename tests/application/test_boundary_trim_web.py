"""Real typed miniature pipeline; no cached physical/coverage flags as evidence."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import xml.etree.ElementTree as ET

import pytest

from rebar import Band, Cell, Mosaic
from rebar.application.analyze_composite_plate import CompositeDirectionSettings
from rebar.application.boundary_trim_web import boundary_trim_web_report
from rebar.application.physical_layout_recovery import _bytes, recover_physical_layout
from rebar.application.physical_web_report import physical_web_report
from rebar.legend import parse_recipe
from rebar.optimization import (
    PLATE_DIRECTIONS, LayoutConstraints, PlateDirectionSolution, StrongestBBoxOptimizer,
    build_layout_problem, build_plate_problem, build_plate_solution,
)
from rebar.optimization.contracts.physical import PhysicalNormalizationConfig
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
from test_working_solid_host import stepped_snapshot


def _translated_snapshot(dx,dy):
    def moved(value):
        if isinstance(value,dict):
            return {k:([v[0]+dx,v[1]+dy,v[2]] if k in ("origin_mm","start_mm","end_mm","min_mm","max_mm") else moved(v))
                    for k,v in value.items()}
        if isinstance(value,list):
            return [moved(v) for v in value]
        return value
    return moved(stepped_snapshot())


@pytest.fixture(scope="module")
def trim_case():
    bands = []
    for i, label in enumerate(("s300d10", "s300d10+s150d10")):
        recipe = parse_recipe(label)
        bands.append(Band(i, i+1, label, 10+20*i, recipe.background,
                          recipe.additions[0] if recipe.additions else None, recipe))
    cells = [Cell([(x,y),(x+750,y),(x+750,y+750),(x,y+750)], (x+375,y+375),2,bands[1])
             for x in (0,750) for y in (0,750)]
    constraints = LayoutConstraints(allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM,
                                     cutting_profile="plate-11700")
    problems = tuple(build_layout_problem(Mosaic(d,deepcopy(cells),deepcopy(bands),
        (0,0,1500,1500), source_path=f"{d}.dxf"),constraints) for d in PLATE_DIRECTIONS)
    problem = build_plate_problem(problems,case_id="synthetic-boundary-only")
    solution = build_plate_solution(PlateDirectionSolution(p.demand.direction,StrongestBBoxOptimizer().solve(p))
                                    for p in problems)
    settings = tuple(CompositeDirectionSettings(d,0,100,0,"A500","explicit synthetic test","left")
                     for d in PLATE_DIRECTIONS)
    recovery = recover_physical_layout(problem,solution,settings,
        normalization_config=PhysicalNormalizationConfig(time_limit_s=5,stock_balance_time_limit_s=2),
        balance_time_limit_s=5,stock_time_limit_s=3)
    assert recovery.packet is not None
    host_bytes = json.dumps(stepped_snapshot()).encode()
    return problem,recovery,host_bytes


@pytest.fixture(scope="module")
def trim_result(trim_case):
    problem,recovery,host_bytes = trim_case
    return boundary_trim_web_report(problem,recovery,host_bytes,confirm_identity_xy=True,stock_time_limit_s=3)


def test_full_web_trim_uses_real_sections_and_separate_fresh_checks(trim_case,trim_result):
    problem,recovery,host_bytes = trim_case
    report = trim_result
    assert report["output_kind"] == "boundary-trimmed-physical-bars"
    assert report["source_graphics"] == physical_web_report(problem,recovery)["source_graphics"]
    assert report["default_drawing_view"] == "source"
    assert report["original_source_zones"] == recovery.packet["source_zones"]
    assert len(report["working_host"]["geometry"]["sections"]) == 2  # actual nonrectangular solid
    assert report["working_host"]["source_report_sha256"] == hashlib.sha256(host_bytes).hexdigest()
    assert report["working_host"]["source_to_revit_xy_mm"] == [0,0]
    assert not report["placement_profile"]["measured_in_Revit"]
    checks = report["boundary_trim"]
    assert checks["changed_input_bar_count"] > 0
    assert checks["removed_mass_kg"] > 0
    assert checks["coverage_with_control_40d"]["status"] == "fail"
    assert checks["stock_cutting"]["status"] == "fail"
    assert not checks["source_demand_removed"] and not checks["old_coverage_or_cutting_certificate_reused"]
    assert not report["placement_eligible"] and not report["engineering_approval"]
    for old_certificate in ("physical_trial_packet","physical_review","same_plane_conflicts","normalization_status"):
        assert old_certificate not in report
    point = report["front"][0]
    assert point["physical_bar_count"] == checks["physical_metrics"]["physical_bar_count"]
    assert point["additional_mass_kg"] == checks["physical_metrics"]["mass_kg"]
    assert point["additional_mass_kg"] == pytest.approx(sum(p["total_mass_kg"] for p in point["bar_schedule"]))
    for direction in report["directions"]:
        candidate = direction["candidates"][0]
        assert candidate["zone_drafts"] == [] and direction["source_zone_drafts"]
        svg = ET.fromstring(candidate["svg"])
        ns = "{http://www.w3.org/2000/svg}"
        assert svg.findall(f'.//{ns}g[@class="host-contours"]/{ns}polygon')
        assert not svg.findall(f'.//{ns}clipPath')
        lines = svg.findall(f'.//{ns}line')
        assert len(lines) == len(candidate["physical_bars"])
        for line,bar in zip(lines,candidate["physical_bars"]):
            length = abs(float(line.attrib["x2"])-float(line.attrib["x1"]))+abs(float(line.attrib["y2"])-float(line.attrib["y1"]))
            assert length == pytest.approx(bar["longitudinal_mm"][1]-bar["longitudinal_mm"][0],abs=.0011)


def test_graphical_export_maps_all_parents_and_real_pieces_without_old_revit_packet(trim_case,trim_result):
    _,recovery,_ = trim_case
    draft = trim_result["graphic_bar_plan_draft"]
    assert draft["schema_version"] == "graphic-bar-plan-draft/v1"
    assert draft["geometry_kind"] == "straight-bars-only"
    assert draft["source_report_sha256"] == hashlib.sha256(recovery.patterned_report_bytes).hexdigest()
    assert draft["checks"]["anchorage_40d"] == "fail"
    assert draft["coverage_policy"] == "physical-main-leg-presence-NOT-anchorage"
    assert draft["radius_sized_edge_axis_nudge_enabled"]
    assert draft["removed_wholly_external_bars"] == []
    checks = trim_result["boundary_trim"]
    assert draft["checks"]["outer_boundary"] == {
        "status":"fail" if checks["external_boundary_failures_after"] else "pass",
        "failure_count":checks["external_boundary_failures_after"]}
    assert draft["checks"]["collisions_3d"]["proven_pair_count"] == checks["collisions"]["proven_collision_pair_count"]
    assert draft["checks"]["collisions_3d"]["uncertain_pair_count"] == checks["collisions"]["uncertain_pair_count"]
    assert not draft["placement_eligible"] and not draft["engineering_approval"]
    before = {(f'{d["direction"]["layer"]}-{d["direction"]["axis"]}',b["id"]):b
              for d in draft["before"]["directions"] for b in d["bars"]}
    after = {(f'{d["direction"]["layer"]}-{d["direction"]["axis"]}',b["id"]):b
             for d in draft["directions"] for b in d["bars"]}
    assert len(before) == draft["before"]["physical_bar_count"]
    assert len(after) == draft["after"]["physical_bar_count"]
    mapped = set()
    for row in draft["piece_mapping"]:
        old = before[row["direction"],row["source_bar_id"]]
        for identifier in row["piece_ids"]:
            key = row["direction"],identifier
            assert key not in mapped
            mapped.add(key)
            bar = after[key]
            assert bar["original_bar_id"] == old["id"]
            assert bar["original_longitudinal_mm"] == old["longitudinal_mm"]
            assert old["longitudinal_mm"][0] <= bar["longitudinal_mm"][0] < bar["longitudinal_mm"][1] <= old["longitudinal_mm"][1]
            assert abs(bar["coordinate_mm"]-bar["original_coordinate_mm"]) in (0,bar["diameter_mm"]/2)
            assert bar["axis_nudged"] == (bar["coordinate_mm"] != old["coordinate_mm"])
            assert bar["physically_cut"] == (bar["longitudinal_mm"] != old["longitudinal_mm"])
            assert "segments" not in bar and "axis_z_mm" not in bar
    assert mapped == set(after)


def test_radius_only_axis_movement_is_explicit_in_new_graphic_inventory(trim_case):
    problem,recovery,_ = trim_case
    # Shift this synthetic host, not the source FE: its maximum becomes x=y=1400.
    # Rebuild values, avoiding mutable corner-list aliases in the voxel fixture.
    snapshot = _translated_snapshot(-100,-100)
    report = boundary_trim_web_report(problem,recovery,json.dumps(snapshot).encode(),confirm_identity_xy=True,
                                      stock_time_limit_s=3)
    moved_bars = [b for d in report["graphic_bar_plan_draft"]["directions"] for b in d["bars"] if b["axis_nudged"]]
    assert moved_bars
    for bar in moved_bars:
        assert bar["original_coordinate_mm"] == 1400 and bar["coordinate_mm"] == 1395
        assert bar["physically_cut"]


def test_wholly_external_bars_are_explicit_zero_piece_parents_not_deleted_FE(trim_case):
    problem,recovery,_ = trim_case
    snapshot = _translated_snapshot(-1000,0)
    report = boundary_trim_web_report(problem,recovery,json.dumps(snapshot).encode(),confirm_identity_xy=True,
                                      stock_time_limit_s=3)
    graphic = report["graphic_bar_plan_draft"]
    removed = {(r["direction"],r["bar_id"]) for r in graphic["removed_wholly_external_bars"]}
    empty = {(r["direction"],r["source_bar_id"]) for r in graphic["piece_mapping"] if not r["piece_ids"]}
    assert removed and removed == empty
    assert all(r["reason"] for r in graphic["removed_wholly_external_bars"])
    assert report["source_graphics"] == physical_web_report(problem,recovery)["source_graphics"]
    assert report["boundary_trim"]["geometric_presence"]["status"] == "fail"
    assert not report["boundary_trim"]["source_demand_removed"]


def test_no_bar_inside_host_retains_all_FE_but_disables_graphic_export(trim_case):
    problem,recovery,_ = trim_case
    snapshot = _translated_snapshot(100000,100000)
    report = boundary_trim_web_report(problem,recovery,json.dumps(snapshot).encode(),confirm_identity_xy=True,
                                      stock_time_limit_s=3)
    assert report["graphic_bar_plan_draft"] is None
    assert report["front"][0]["physical_bar_count"] == 0
    assert report["front"][0]["additional_mass_kg"] == 0
    assert report["boundary_trim"]["stock_cutting"]["status"] == "not_checked"
    assert len(report["boundary_trim"]["removed_wholly_external_bars"]) == recovery.review["expected"]["physical_bar_count"]
    assert report["boundary_trim"]["geometric_presence"]["uncovered_cell_count"] == 16
    assert all(not d["candidates"][0]["physical_bars"] and d["source_zone_drafts"] for d in report["directions"])


@pytest.mark.parametrize("confirmed",[False,None,1,"true"])
def test_explicit_identity_binding_is_required(trim_case,confirmed):
    problem,recovery,host_bytes = trim_case
    with pytest.raises(ValueError,match="XY"):
        boundary_trim_web_report(problem,recovery,host_bytes,confirm_identity_xy=confirmed)


@pytest.mark.parametrize("bad",[b"{}",b'{"units":"mm","units":"m"}',b'{"value":NaN}'])
def test_no_rectangle_fallback_for_unsupported_host(trim_case,bad):
    problem,recovery,_ = trim_case
    with pytest.raises(ValueError):
        boundary_trim_web_report(problem,recovery,bad,confirm_identity_xy=True)


@pytest.mark.parametrize("tamper",["report","bytes","old_packet"])
def test_mutated_upstream_recovery_does_not_authorize_a_trim(trim_case,tamper):
    problem,recovery,host_bytes = trim_case
    if tamper == "bytes":
        changed = replace(recovery,patterned_report_bytes=recovery.patterned_report_bytes+b" ")
    elif tamper == "old_packet":
        packet = deepcopy(recovery.packet)
        packet["expected"]["physical_bar_count"] += 1
        changed = replace(recovery,packet=packet,packet_bytes=_bytes(packet))
    else:
        review = deepcopy(recovery.review)
        review["expected"]["additional_mass_kg"] += 1
        changed = replace(recovery,review=review)
    with pytest.raises(ValueError):
        boundary_trim_web_report(problem,changed,host_bytes,confirm_identity_xy=True)
