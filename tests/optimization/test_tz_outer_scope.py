"""User-selected task boundary never impersonates a measured Revit host proof."""
from dataclasses import replace

import pytest
from shapely.geometry import box

from rebar.optimization.algorithms.tz_outer_repair import propose_tz_outer_repair
from rebar.optimization.services.shaped_geometry import check_shaped_host, shaped_cut_length_mm
from rebar.optimization.services.solid_host import SolidHostSection
from rebar.optimization.services.tz_outer_scope import (
    check_tz_outer_bar, check_tz_outer_batch, outer_scope_domain, outer_start_intervals,
    translate_straight_whole,
)
from test_shaped_global_coverage import _case, _shapes


def test_holes_and_cover_are_excluded_only_in_explicit_task_domain():
    before, _, _, host = _case()
    material = host.sections[0].footprint.difference(box(1000, 50, 1100, 150))
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    bar = _shapes(before, host)[0]
    assert check_shaped_host(bar, host)["status"] != "pass"
    report = check_tz_outer_bar(bar, host)
    assert report["status"] == "pass"
    assert not report["actual_Revit_host_pass_claimed"] and not report["placement_eligible"]
    assert not report["openings_checked"] and not report["concrete_cover_checked"]
    shifted = translate_straight_whole(bar, start_mm=0)
    assert check_shaped_host(shifted, host)["status"] != "pass"
    assert check_tz_outer_bar(shifted, host)["status"] == "pass"
    derived = outer_scope_domain(host)
    assert len(host.sections[0].footprint.interiors) == 1
    assert len(derived.sections[0].footprint.interiors) == 0
    assert host.side_cover_mm == 25 and derived.side_cover_mm == 0


def test_outer_recess_body_radius_and_intermediate_height_are_retained():
    before, _, _, host = _case()
    material = host.sections[0].footprint.difference(box(1000, -1000, 1100, 100))
    host = replace(host, sections=(SolidHostSection(0, 150, host.sections[0].footprint),
                                  SolidHostSection(150, 200, material)),
                   volume_mm3=host.sections[0].footprint.area*150+material.area*50)
    bar = _shapes(before, host)[0]  # z=155, axis touches recess, body crosses it
    assert check_tz_outer_bar(bar, host)["status"] != "pass"
    domain = outer_scope_domain(host)
    assert domain.sections[1].footprint.equals(material)
    assert not domain.sections[0].footprint.equals(domain.sections[1].footprint)
    assert outer_start_intervals(bar, host) == ((1100., 2075.),)


def test_literal_whole_shift_can_pass_all_retained_geometric_FE_checks():
    before, lanes, problem, host = _case(edge=True, demand_bounds=(500, 100, 1500, 200))
    bars = _shapes(before, host)
    after, search = propose_tz_outer_repair(bars, lanes, problem, host)
    assert search["changed_bar_count"] == 2 and not search["budget_exhausted"]
    assert all(b.segments[0].start_mm[0] == 0 for b in after)
    assert all(shaped_cut_length_mm(b) == 2925 for b in after)
    report = check_tz_outer_batch(bars, after, lanes, problem, host)
    assert report["external_boundary_failures_after"] == 0
    assert report["source_coverage"]["status"] == "pass"
    assert report["collisions"]["status"] == "pass"
    assert "closed_openings" not in report["tz_blockers"]
    assert "concrete_cover" not in report["tz_blockers"]
    assert not report["all_TZ_requirements_certified"]


def test_literal_geometry_trial_exposes_FE_loss_instead_of_silently_accepting_it():
    before, lanes, problem, host = _case(edge=True)
    bars = _shapes(before, host)
    geometry, search = propose_tz_outer_repair(bars, lanes, problem, host, mode="geometry-first")
    assert search["changed_bar_count"] == 2
    report = check_tz_outer_batch(bars, geometry, lanes, problem, host)
    assert report["external_boundary_failures_after"] == 0
    assert report["source_coverage"]["uncovered_cell_count"] == 1
    assert "original_FE_coverage_with_retained_40d" in report["tz_blockers"]
    safe, search = propose_tz_outer_repair(bars, lanes, problem, host)
    assert search["rejections"]["original_FE_loss_with_retained_40d"] > 0
    # One bar can move while the other retains the boundary FE. Moving BOTH
    # is what creates the uncovered region; ownership is not frozen per bar.
    retained = check_tz_outer_batch(bars, safe, lanes, problem, host)
    assert retained["external_boundary_failures_after"] == 1
    assert retained["source_coverage"]["status"] == "pass"


def test_background_axis_penetration_is_independently_reported():
    before, lanes, problem, host = _case()
    bars = _shapes(before, host)
    after = (translate_straight_whole(bars[0], start_mm=25, transverse_axis_mm=0), bars[1])
    report = check_tz_outer_batch(bars, after, lanes, problem, host)
    assert "source_prescribed_background_axis_collision" in report["tz_blockers"]
    assert report["minimum_source_prescribed_background_gap_mm"] == -10


def test_existing_pair_is_repaired_without_creating_a_new_edge_or_FE_failure():
    before, lanes, problem, host = _case(duplicate=True)
    bars = _shapes(before, host)
    after, search = propose_tz_outer_repair(bars, lanes, problem, host,
        allow_transverse=True, repair_collisions=True)
    report = check_tz_outer_batch(bars, after, lanes, problem, host)
    assert search["changed_bar_count"] == 1
    assert report["external_boundary_failures_after"] == 0
    assert report["source_coverage"]["uncovered_cell_count"] == 0
    assert report["collisions"]["proven_collision_pair_count"] == 0
    assert report["whole_lengths_preserved"]


@pytest.mark.parametrize("kind", ("shorten", "z", "diameter", "missing", "duplicate"))
def test_batch_rejects_nonrigid_and_incomplete_changes(kind):
    before, lanes, problem, host = _case()
    bars = _shapes(before, host)
    bar = bars[0]
    if kind == "shorten":
        segment = bar.segments[0]
        end = (segment.end_mm[0]-1, *segment.end_mm[1:])
        bar = replace(bar, segments=(replace(segment, end_mm=end),), selected_cut_length_mm=2924)
    elif kind == "z":
        segment = bar.segments[0]
        bar = replace(bar, segments=(replace(segment,
            start_mm=(*segment.start_mm[:2], segment.start_mm[2]-1),
            end_mm=(*segment.end_mm[:2], segment.end_mm[2]-1)),))
    elif kind == "diameter":
        bar = replace(bar, diameter_mm=12)
    after = () if kind == "missing" else (bar, bar) if kind == "duplicate" else (bar, bars[1])
    with pytest.raises(ValueError):
        check_tz_outer_batch(bars, after, lanes, problem, host)


@pytest.mark.parametrize("budget", (True, 0, -1, float("nan"), float("inf")))
def test_bad_budgets_do_not_disable_checks(budget):
    before, lanes, problem, host = _case()
    with pytest.raises(ValueError, match="budget"):
        propose_tz_outer_repair(_shapes(before, host), lanes, problem, host, time_limit_s=budget)
