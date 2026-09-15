from dataclasses import replace

import pytest
from shapely.geometry import Polygon, box

from rebar.optimization.algorithms.tz_boundary_trim import trim_straight_bars_to_outer_boundary
from rebar.optimization.contracts.shaped_physical import Line3D
from rebar.optimization.services.shaped_geometry import shaped_cut_length_mm
from rebar.optimization.services.solid_host import SolidHostSection
from rebar.optimization.services.tz_boundary_trim import (
    build_trimmed_pieces, check_boundary_trim, straight_outer_intersections,
)
from test_shaped_global_coverage import _case, _shapes


def _trim_case():
    original, lanes, problem, host = _case(edge=True)
    return _shapes(original, host), lanes, problem, host


def test_cut_is_real_and_does_not_reuse_anchor_or_stock_pass():
    before, lanes, problem, host = _trim_case()
    after, mapping = trim_straight_bars_to_outer_boundary(before, host)
    assert all(b.segments[0].start_mm[0] == 0 for b in after)
    assert [shaped_cut_length_mm(b) for b in after] == [2625, 2625]
    report = check_boundary_trim(before, after, mapping, lanes, problem, host)
    assert report["external_boundary_failures_before"] == 2
    assert report["external_boundary_failures_after"] == 0
    assert report["geometric_presence"]["status"] == "pass"
    assert report["coverage_with_control_40d"]["status"] == "fail"
    assert report["stock_cutting"]["status"] == "fail"
    assert report["removed_length_mm"] == 600
    assert report["removed_mass_kg"] > 0
    assert not report["placement_eligible"] and not report["engineering_approval"]
    assert not report["original_FE_geometry_changed"]


def test_concave_external_recess_splits_and_maps_every_piece():
    before, lanes, problem, host = _trim_case()
    material = box(0, -1000, 5000, 1000).difference(box(1000, 0, 2000, 1000))
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    after, mapping = trim_straight_bars_to_outer_boundary(before, host)
    assert len(after) == 4
    assert [b.id for b in after] == ["a/outer-piece-1", "a/outer-piece-2", "b/outer-piece-1", "b/outer-piece-2"]
    assert [shaped_cut_length_mm(b) for b in after] == [1000, 625, 1000, 625]
    report = check_boundary_trim(before, after, mapping, lanes, problem, host)
    assert report["external_boundary_failures_after"] == 0
    assert report["geometric_presence"]["status"] == "fail"  # ORIGINAL demand in the recess stays visible.
    assert report["physical_metrics"]["physical_bar_count"] == 4
    assert not report["source_demand_removed"]


def test_closed_holes_and_snapshot_covers_do_not_change_trim():
    before, _, _, host = _trim_case()
    material = Polygon(box(0, -1000, 5000, 1000).exterior.coords,
                       [box(1000, 0, 2000, 500).exterior.coords])
    hole_host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    assert straight_outer_intersections(before[0], hole_host) == ((0, 2625),)
    assert hole_host.side_cover_mm == 25 and len(hole_host.sections[0].footprint.interiors) == 1


def test_all_body_width_and_height_sections_not_only_axis_are_checked():
    before, _, _, host = _trim_case()
    # Axis at y100 is clear, but diameter10 hits a recess starting at y102.
    material = box(0, -1000, 5000, 1000).difference(box(1000, 102, 2000, 1000))
    host = replace(host, sections=(SolidHostSection(0, 150, box(0, -1000, 5000, 1000)),
                                  SolidHostSection(150, 200, material)),
                   volume_mm3=1e7*150+material.area*50)
    assert straight_outer_intersections(before[0], host) == ((0, 1000), (2000, 2625))


def test_no_positive_body_intersection_is_reported_not_silently_deleted():
    before, lanes, problem, host = _trim_case()
    material = box(4000, -1000, 5000, 1000)
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    after, mapping = trim_straight_bars_to_outer_boundary(before, host)
    assert after == before
    report = check_boundary_trim(before, after, mapping, lanes, problem, host)
    assert report["external_boundary_failures_after"] == 2
    assert len(report["unresolved_bars"]) == 2


def test_optional_radius_nudge_of_axis_on_boundary_is_explicit_and_checked():
    before, lanes, problem, host = _trim_case()
    material = box(0, 100, 5000, 1000)  # first bar y100, half of diameter outside.
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    after, mapping = trim_straight_bars_to_outer_boundary(before, host, lanes=lanes, nudge_edge_axis=True)
    assert after[0].segments[0].start_mm[1] == 105
    report = check_boundary_trim(before, after, mapping, lanes, problem, host, nudge_edge_axis=True)
    assert report["external_boundary_failures_after"] == 0
    assert report["changes"][0]["transverse_shift_mm"] == 5
    with pytest.raises(ValueError):
        check_boundary_trim(before, after, mapping, lanes, problem, host)


def test_empty_intersection_can_be_explicitly_removed_but_not_its_original_demand():
    before, lanes, problem, host = _trim_case()
    material = box(4000, -1000, 5000, 1000)
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    after, mapping = trim_straight_bars_to_outer_boundary(before, host, discard_empty_intersections=True)
    assert after == () and all(row["piece_ids"] == [] for row in mapping)
    report = check_boundary_trim(before, after, mapping, lanes, problem, host, discard_empty_intersections=True)
    assert report["status"] == "empty_physical_result"
    assert report["external_boundary_failures_after"] == 0
    assert report["removed_input_bar_count"] == 2
    assert len(report["removed_wholly_external_bars"]) == 2
    assert report["geometric_presence"]["uncovered_cell_count"] == 1
    assert report["coverage_with_control_40d"]["uncovered_cell_count"] == 1
    assert report["stock_cutting"]["status"] == "not_checked"
    assert report["physical_metrics"]["physical_bar_count"] == 0
    assert report["removed_length_mm"] == 5850
    assert not report["source_demand_removed"] and not report["placement_eligible"]
    with pytest.raises(ValueError):
        check_boundary_trim(before, after, mapping, lanes, problem, host)


def test_opt_in_does_not_allow_removing_an_actual_retained_piece():
    before, lanes, problem, host = _trim_case()
    after, mapping = trim_straight_bars_to_outer_boundary(before, host, discard_empty_intersections=True)
    false_mapping = ({**mapping[0], "piece_ids": []}, mapping[1])
    with pytest.raises(ValueError):
        check_boundary_trim(before, after[1:], false_mapping, lanes, problem, host, discard_empty_intersections=True)


@pytest.mark.parametrize("mutation", ("old_geometry", "drop_piece", "diameter", "mapping", "length", "z"))
def test_readback_rejects_stale_metrics_geometry_and_missing_provenance(mutation):
    before, lanes, problem, host = _trim_case()
    after, mapping = trim_straight_bars_to_outer_boundary(before, host)
    if mutation == "old_geometry":
        after = before
    elif mutation == "drop_piece":
        after = after[:1]
    elif mutation == "diameter":
        after = (replace(after[0], diameter_mm=12), after[1])
    elif mutation == "mapping":
        mapping = ({**mapping[0], "piece_ids": []}, mapping[1])
    elif mutation == "length":
        after = (replace(after[0], selected_cut_length_mm=2925), after[1])
    else:
        line = after[0].segments[0]
        changed = Line3D((*line.start_mm[:2], 154), (*line.end_mm[:2], 154))
        after = (replace(after[0], segments=(changed,)), after[1])
    with pytest.raises(ValueError):
        check_boundary_trim(before, after, mapping, lanes, problem, host)


@pytest.mark.parametrize("intervals", (((0, 0),), ((0, 3000),), ((0, 1000), (500, 2000)), ((False, 100),)))
def test_invalid_piece_intervals_fail_closed(intervals):
    before, _, _, _ = _trim_case()
    with pytest.raises(ValueError):
        build_trimmed_pieces(before[0], intervals)
