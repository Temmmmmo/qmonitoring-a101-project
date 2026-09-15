from dataclasses import replace

import pytest
from shapely.geometry import box

from rebar.models import Axis, Direction, Layer
from rebar.optimization.algorithms.tz_collision_tiers import propose_tz_collision_tiers
from rebar.optimization.services.shaped_fe_repair import ResearchLayerProfile, layer_elevations
from rebar.optimization.services.shaped_geometry import build_u_edge_bar
from rebar.optimization.services.solid_host import SolidHostSection
from rebar.optimization.services.tz_collision_tiers import (
    PROFILE_ID, check_tz_collision_tiers, make_second_tier,
)
from test_shaped_global_coverage import _case, _shapes


def _party():
    physical, lanes, problem, host = _case(duplicate=True)
    extra_bars, extra_lanes = [], []
    for name, q in (("c", 400.), ("d", 700.)):
        source = replace(lanes[0].source, id=f"zone-{name}/0/0", transverse_axis_mm=q)
        extra_bars.append(replace(physical[0], id=name, transverse_axis_mm=q, source_bar_ids=(source.id,)))
        extra_lanes.append(replace(lanes[0], source=source, zone_id=f"zone-{name}", axis_window_mm=(q-150, q+150)))
    return _shapes((*physical, *extra_bars), host), (*lanes, *extra_lanes), problem, host


def test_full_Z_hypothesis_removes_pair_preserves_FE_stock_and_reports_real_cover_regression():
    before, lanes, problem, host = _party()
    after, search = propose_tz_collision_tiers(before, lanes, problem, host)
    assert len(after) == 4 and search["candidate_changes"] == 1
    assert after[0] == make_second_tier(before[0])
    assert after[0].placement_profile_id == PROFILE_ID
    assert after[1:] == before[1:]
    checked = check_tz_collision_tiers(before, after, lanes, problem, host)
    assert checked["collisions_before"]["proven_collision_pair_count"] == 1
    assert checked["collisions_after"]["complete_no_body_collision_proof"]
    assert checked["source_coverage"]["uncovered_cell_count"] == 0
    assert checked["each_original_FE_service_offer_exactly_unchanged"]
    assert checked["external_boundary_failures_after"] == 0
    assert checked["stock_cutting"]["status"] == "pass"
    assert checked["physical_metrics"]["true_cut_length_mm"] == 11700
    assert checked["physical_metrics"]["physical_bar_count"] == 4
    assert checked["actual_host_informational_before"] == []
    assert checked["new_actual_host_informational_failure_ids"] == [("top-X", "a")]
    assert [r["tier"] for r in checked["whole_plate_tier_assignment"]] == [1, 0, 0, 0]
    assert checked["status"] == "research_checks_passed_not_approved"
    assert not checked["placement_eligible"] and not checked["engineering_approval"]
    assert not checked["effective_depth_and_capacity_checked"]
    assert not checked["rigid_Z_preserving_checker_reused_for_new_Z"]


def test_retained_old_pair_is_explicitly_blocked_not_hidden():
    before, lanes, problem, host = _party()
    checked = check_tz_collision_tiers(before, before, lanes, problem, host)
    assert checked["changed_bar_count"] == 0
    assert checked["tz_blockers"] == ["remaining_additional_3D_conflicts"]


@pytest.mark.parametrize("bad", ("XY", "Z", "diameter", "steel", "length", "owners", "old_profile", "missing", "duplicate"))
def test_independent_checker_rejects_noncanonical_or_partial_changes(bad):
    before, lanes, problem, host = _party()
    changed = make_second_tier(before[0])
    if bad in ("XY", "Z", "length"):
        line = changed.segments[0]
        a, b = list(line.start_mm), list(line.end_mm)
        if bad == "XY":
            a[1] += 1
            b[1] += 1
        elif bad == "Z":
            a[2] += 1
            b[2] += 1
        else:
            b[0] += 1
        changed = replace(changed, segments=(replace(line, start_mm=tuple(a), end_mm=tuple(b)),))
    elif bad == "diameter":
        changed = replace(changed, diameter_mm=12)
    elif bad == "steel":
        changed = replace(changed, steel_class="other-steel")
    elif bad == "owners":
        changed = replace(changed, source_bar_ids=before[1].source_bar_ids)
    elif bad == "old_profile":
        changed = replace(changed, placement_profile_id=before[0].placement_profile_id)
    after = (changed, *before[1:])
    if bad == "missing":
        after = after[:-1]
    elif bad == "duplicate":
        after = (changed, changed, *before[2:])
    with pytest.raises(ValueError):
        check_tz_collision_tiers(before, after, lanes, problem, host)


def test_two_bars_cannot_keep_their_old_collision_on_the_second_tier():
    before, lanes, problem, host = _party()
    after = (make_second_tier(before[0]), make_second_tier(before[1]), *before[2:])
    with pytest.raises(ValueError, match="changed bar still"):
        check_tz_collision_tiers(before, after, lanes, problem, host)


def test_second_promotion_and_wrong_baseline_Z_are_rejected():
    before, lanes, problem, host = _party()
    with pytest.raises(ValueError, match="original-profile"):
        make_second_tier(make_second_tier(before[0]))
    invalid = (make_second_tier(before[0]), *before[1:])
    with pytest.raises(ValueError, match="single-tier"):
        propose_tz_collision_tiers(invalid, lanes, problem, host)


def _U_collision_party():
    physical, original_lanes, problem, host = _case(duplicate=True)
    footprint = box(0, -1000, 5000, 5000)
    host = replace(host, sections=(SolidHostSection(0, 200, footprint),), volume_mm3=footprint.area*200)
    bars, lanes = [], []
    directions = (Direction(Layer.BOTTOM, Axis.Y), Direction(Layer.BOTTOM, Axis.Y),
                  Direction(Layer.BOTTOM, Axis.X), Direction(Layer.BOTTOM, Axis.Y))
    for name, q, direction in zip(("a", "b", "u", "d"), (22., 22., 100., 800.), directions):
        source = replace(original_lanes[0].source, id=f"zone-{name}/0/0", direction=direction, transverse_axis_mm=q)
        lanes.append(replace(original_lanes[0], zone_id=f"zone-{name}", source=source, axis_window_mm=(q-150, q+150)))
        straight = replace(physical[0], id=name, direction=direction, transverse_axis_mm=q, source_bar_ids=(source.id,))
        bars.append(_shapes((straight,), host)[0])
    zm, zr = layer_elevations(host, directions[2], 10, ResearchLayerProfile())
    built = build_u_edge_bar(bar_id="u", direction=directions[2], steel_class="A500", diameter_mm=10,
        transverse_axis_mm=100, edge_coordinate_mm=0, inward_sign=1, main_axis_z_mm=zm, return_axis_z_mm=zr,
        slab_thickness_mm=200, side_cover_mm=25, cut_length_mm=2925, source_bar_ids=("zone-u/0/0",),
        placement_profile_id=ResearchLayerProfile().id)
    assert built.status == "geometry_conditions_met"
    bars[2] = built.bar
    problem = replace(problem, direction_problems=tuple(replace(row, demand=replace(row.demand,
        cells=tuple(replace(cell, level_index=0, aci=1) for cell in row.demand.cells))) for row in problem.direction_problems))
    return tuple(bars), tuple(lanes), problem, host


def test_new_collision_with_unchanged_U_bridge_is_rolled_back_by_real_3D_checks():
    before, lanes, problem, host = _U_collision_party()
    after = (make_second_tier(before[0]), *before[1:])
    with pytest.raises(ValueError, match="new proven or uncertain"):
        check_tz_collision_tiers(before, after, lanes, problem, host)
    result, search = propose_tz_collision_tiers(before, lanes, problem, host)
    assert result == before
    assert search["attempts"] and all(row["outcome"] == "rollback_new_or_uncertain_3D_pair" for row in search["attempts"])
    assert result[2] == before[2]


@pytest.mark.parametrize("limit", (True, 0, -1, 121, float("nan"), float("inf")))
def test_bounded_search_limits_do_not_disable_verification(limit):
    before, lanes, problem, host = _party()
    with pytest.raises(ValueError, match="bounded"):
        propose_tz_collision_tiers(before, lanes, problem, host, time_limit_s=limit)


def test_outer_body_failure_after_shift_is_not_accepted():
    before, lanes, problem, host = _party()
    # A high-Z external recess removes material only above the old body's top.
    small = box(0, -1000, 1000, 1000)
    host = replace(host, sections=(SolidHostSection(0, 165, host.sections[0].footprint),
        SolidHostSection(165, 200, small)), volume_mm3=host.sections[0].footprint.area*165+small.area*35)
    after = (make_second_tier(before[0]), *before[1:])
    with pytest.raises(ValueError, match="external-body"):
        check_tz_collision_tiers(before, after, lanes, problem, host)
    proposed, search = propose_tz_collision_tiers(before, lanes, problem, host)
    assert proposed == before and all(r["outcome"] == "rollback_new_external_body_failure" for r in search["attempts"])
