"""Original FE union, not immutable old-owner responsibility or a transfer map."""
from dataclasses import replace

import pytest
from shapely.geometry import box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.algorithms.shaped_global_repair import propose_global_shaped_repair
from rebar.optimization.contracts.opening_relocation import SourceServiceLane
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.opening_relocation import lane_map
from rebar.optimization.services.shaped_fe_repair import ResearchLayerProfile, layer_elevations
from rebar.optimization.services.shaped_geometry import build_u_edge_bar, straight_bar_from_physical
from rebar.optimization.services.shaped_global_coverage import (
    check_shaped_global_repair, necessary_regions_covered, necessary_source_regions,
)
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def _case(*, edge=False, duplicate=False, demand_bounds=None, strong=False):
    direction = Direction(Layer.TOP, Axis.X)
    installed, required = ((-300, 2625), (100, 1500)) if edge else ((25, 2950), (500, 2000))
    diameter = 16 if strong else 10
    specs = [("a", "zone-a", 100., diameter), ("b", "zone-b", 100. if duplicate else 200., 10)]
    if strong:
        required = (700, 2000)
        specs.append(("c", "zone-c", 250., 10))
    bars, lanes = [], []
    for name, zone, q, d in specs:
        source = PhysicalSourceBar(f"{zone}/0/0", direction, "A500", d, q,
            installed, required, 10, 0)
        bars.append(PhysicalBar(name, direction, "A500", d, q, installed, (source.id,)))
        lanes.append(SourceServiceLane(source, zone, 0, 0, 300,
            (-50, 500) if strong else (-50, 350) if name == "a" else (-50, 450), (150, 150)))
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(300, diameter),))
    levels = (DemandLevel(0, 1, 0, 1, "background", None, False, replace(recipe, additions=())),
              DemandLevel(1, 2, 1, 2, "addition", recipe.additions[0], True, recipe))
    polygon = box(*(demand_bounds or (required[0], 100, required[1], 200)))
    problems = tuple(LayoutProblem(DemandMap(d, levels, (DemandCell(1,
        tuple(polygon.exterior.coords)[:-1], (polygon.centroid.x, polygon.centroid.y),
        2 if d == direction else 1, int(d == direction)),), polygon.bounds)) for d in PLATE_DIRECTIONS)
    host = OrthogonalSolidHost((SolidHostSection(0, 200, box(0, -1000, 5000, 1000)),),
                               40, 40, 25, 2e9, 6)
    return tuple(bars), tuple(lanes), PlateProblem(problems, "synthetic-global"), host


def _shapes(before, host, changes=None):
    changes = changes or {}
    profile = ResearchLayerProfile()
    result = []
    for bar in before:
        changed = replace(bar, transverse_axis_mm=changes.get(bar.id, bar.transverse_axis_mm))
        z, _ = layer_elevations(host, bar.direction, bar.diameter_mm, profile)
        result.append(straight_bar_from_physical(changed, axis_z_mm=z, placement_profile_id=profile.id))
    return tuple(result)


def test_global_reassignment_preserves_FE_without_old_owner_fragment_gate():
    before, lanes, problem, host = _case()
    after = _shapes(before, host, {"a": 320})
    report = check_shaped_global_repair(before, after, lanes, problem, host)
    assert report["source_coverage"]["status"] == "pass"
    assert report["source_coverage"]["positive_area_loss_tolerance_mm2"] == 0
    assert report["old_owner_service_loss_bar_count"] == 1
    assert report["bars_losing_some_old_owner_FE_service"] == [("top-X", "a")]
    assert not report["per_owner_FE_retention_required"]
    assert not report["per_owner_FE_certificate_reused"]
    assert not report["demand_transfer_used"] and not report["original_FE_geometry_changed"]
    assert report["physical_stock_inventory_preserved"]
    assert report["shaped_host_not_proven_after"] == 0
    assert report["3d_collisions_after"]["complete_no_body_collision_proof"]
    assert report["original_STO_phase_certificate"] == "not_reused_not_claimed"
    assert not report["placement_eligible"]


def test_necessary_regions_are_all_original_demand_minus_other_current_supply():
    before, lanes, problem, host = _case()
    initial = _shapes(before, host)
    a, b = before
    assert necessary_source_regions(initial, lanes, problem, bar_key=(a.direction, a.id)) == ()
    updated = _shapes(before, host, {"a": 320})
    regions = necessary_source_regions(updated, lanes, problem, bar_key=(b.direction, b.id))
    assert len(regions) == 1 and regions[0].cell_id == 1
    assert regions[0].geometry.equals(box(500, 100, 2000, 170))
    assert necessary_regions_covered(updated[1], regions, lane_map(lanes))
    bad = _shapes(before, host, {"a": 320, "b": 400})
    assert not necessary_regions_covered(bad[1], regions, lane_map(lanes))
    with pytest.raises(ValueError, match="original FE positive-area"):
        check_shaped_global_repair(before, bad, lanes, problem, host)


def test_root_proposal_is_independently_checked_without_old_ownership_gate():
    before, lanes, problem, host = _case(edge=True, duplicate=True, demand_bounds=(100, -50, 1500, 250))
    # Match actual single-bar source windows used by the planner's regression.
    lanes = tuple(replace(lane, axis_window_mm=(-50, 250)) for lane in lanes)
    after, search = propose_global_shaped_repair(before, lanes, problem, host, time_limit_s=20)
    assert all(bar.shape_kind == "U" for bar in after)
    report = check_shaped_global_repair(before, after, lanes, problem, host)
    assert report["source_coverage"]["status"] == "pass"
    assert report["shaped_host_not_proven_before"] == 2
    assert report["shaped_host_not_proven_after"] == 0
    assert report["old_owner_service_loss_bar_count"] > 0
    assert report["3d_collisions_before"]["proven_collision_pair_count"] == 1
    assert report["3d_collisions_after"]["status"] == "pass"
    assert not search["source_field_transferred"]


def test_explicit_longitudinal_search_preserves_original_FE_without_needing_U():
    before, lanes, problem, host = _case(edge=True, demand_bounds=(500, 100, 1500, 200))
    after, search = propose_global_shaped_repair(before, lanes, problem, host,
        time_limit_s=20, maximum_longitudinal_shift_mm=11700)
    assert all(bar.shape_kind == "straight" for bar in after)
    assert all(bar.segments[0].start_mm[0] == 25 for bar in after)
    assert search["maximum_longitudinal_shift_mm"] == 11700
    with pytest.raises(ValueError, match="longitudinal translation"):
        check_shaped_global_repair(before, after, lanes, problem, host)
    with pytest.raises(ValueError, match="longitudinal translation"):
        check_shaped_global_repair(before, after, lanes, problem, host, maximum_longitudinal_shift_mm=324)
    report = check_shaped_global_repair(before, after, lanes, problem, host,
                                      maximum_longitudinal_shift_mm=325)
    assert report["source_coverage"]["uncovered_cell_count"] == 0
    assert report["shaped_host_not_proven_after"] == 0
    assert report["U_substitution_count"] == 0
    assert report["physical_stock_inventory_preserved"]
    assert report["3d_collisions_after"]["status"] == "pass"
    assert not report["placement_eligible"]


def test_allowing_longitudinal_shift_never_waives_original_FE_coverage():
    before, lanes, problem, host = _case(edge=True, demand_bounds=(500, 100, 1500, 200))
    shifted = tuple(replace(b, installed_interval_mm=(1125, 4050)) for b in before)
    after = _shapes(shifted, host)
    with pytest.raises(ValueError, match="original FE positive-area"):
        check_shaped_global_repair(before, after, lanes, problem, host,
                                  maximum_longitudinal_shift_mm=2000)


@pytest.mark.parametrize("limit", (True, -1, 11701, float("nan"), float("inf")))
def test_independent_checker_rejects_bad_longitudinal_limit(limit):
    before, lanes, problem, host = _case()
    with pytest.raises(ValueError, match="longitudinal"):
        check_shaped_global_repair(before, _shapes(before, host), lanes, problem, host,
                                  maximum_longitudinal_shift_mm=limit)


def test_weak_As_offers_never_sum_to_replace_stronger_original_demand():
    before, lanes, problem, host = _case(strong=True, demand_bounds=(700, 100, 2000, 150))
    initial = _shapes(before, host)
    regions = necessary_source_regions(initial, lanes, problem,
                                        bar_key=(before[0].direction, before[0].id))
    assert len(regions) == 1 and regions[0].geometry.area == 1300*50
    assert regions[0].required_diameter_mm == 16
    after = _shapes(before, host, {"a": 350})
    with pytest.raises(ValueError, match="original FE positive-area"):
        check_shaped_global_repair(before, after, lanes, problem, host)


def test_no_positive_microfragment_is_discarded_by_global_final_check():
    before, lanes, problem, host = _case(demand_bounds=(500, 100, 2000, 200))
    # Both supplies begin at 100+epsilon; old 1e-9 relative tolerance would pass.
    after = _shapes(before, host, {"a": 250+1e-10, "b": 400})
    with pytest.raises(ValueError, match="original FE positive-area"):
        check_shaped_global_repair(before, after, lanes, problem, host)


def test_leaving_old_background_tangency_is_allowed_but_penetration_is_not():
    before, lanes, problem, host = _case(demand_bounds=(500, 270, 2000, 310))
    source = replace(lanes[0].source, transverse_axis_mm=290)
    lane = replace(lanes[0], source=source, nominal_step_mm=100,
                   axis_window_mm=(0, 400), service_half_widths_mm=(45, 55))
    before = (replace(before[0], transverse_axis_mm=290),)
    lanes = (lane,)
    after = _shapes(before, host, {"a": 280})
    report = check_shaped_global_repair(before, after, lanes, problem, host)
    assert report["minimum_source_prescribed_background_gap_mm"] == 10
    assert not report["old_background_contact_retention_required"]
    with pytest.raises(ValueError, match="background"):
        check_shaped_global_repair(before, _shapes(before, host, {"a": 298}), lanes, problem, host)


def test_retained_old_host_and_body_failures_are_reported_not_green():
    before, lanes, problem, host = _case(edge=True, duplicate=True)
    report = check_shaped_global_repair(before, _shapes(before, host), lanes, problem, host)
    assert report["status"] == "blocked_host"
    assert report["shaped_host_not_proven_after"] == 2
    assert report["3d_collisions_after"]["proven_collision_pair_count"] == 1
    assert report["changed_bar_count"] == 0


def test_changed_bar_may_not_keep_an_old_collision_with_same_pair_ids():
    before, lanes, problem, host = _case(duplicate=True)
    with pytest.raises(ValueError, match="changed bar participates"):
        check_shaped_global_repair(before, _shapes(before, host, {"a": 105}), lanes, problem, host)


def test_new_body_collision_rejected_even_with_full_original_FE_union():
    before, lanes, problem, host = _case()
    with pytest.raises(ValueError, match="New proven"):
        check_shaped_global_repair(before, _shapes(before, host, {"a": 205}), lanes, problem, host)


def test_new_uncertainty_is_not_allowed_as_green(monkeypatch):
    before, lanes, problem, host = _case()
    from rebar.optimization.services import shaped_global_coverage as module
    actual = module.check_shaped_collisions
    calls = []

    def injected(bars, **kwargs):
        report = actual(bars, **kwargs)
        calls.append(bars)
        if len(calls) == 2:
            report["uncertain_pairs"] = [{"first": {"direction": "top-X", "bar_id": "a"},
                                          "second": {"direction": "top-X", "bar_id": "b"}}]
        return report

    monkeypatch.setattr(module, "check_shaped_collisions", injected)
    with pytest.raises(ValueError, match="New proven or uncertain"):
        check_shaped_global_repair(before, _shapes(before, host, {"a": 320}), lanes, problem, host)
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["owner", "diameter", "steel", "cut", "Z", "along", "profile", "duplicate", "missing"])
def test_same_inventory_and_canonical_geometry_cannot_be_forged(kind):
    before, lanes, problem, host = _case()
    after = list(_shapes(before, host))
    bar = after[0]
    if kind == "owner":
        bar = replace(bar, source_bar_ids=("invented",))
    elif kind == "diameter":
        bar = replace(bar, diameter_mm=12)
    elif kind == "steel":
        bar = replace(bar, steel_class="other")
    elif kind == "cut":
        bar = replace(bar, selected_cut_length_mm=3900)
    elif kind in ("Z", "along"):
        coordinate = 2 if kind == "Z" else 0
        segment = bar.segments[0]
        start, end = list(segment.start_mm), list(segment.end_mm)
        start[coordinate] += 1
        end[coordinate] += 1
        bar = replace(bar, segments=(replace(segment, start_mm=tuple(start), end_mm=tuple(end)),))
    elif kind == "profile":
        bar = replace(bar, placement_profile_id="other")
    after[0] = bar
    if kind == "duplicate":
        after = [bar, bar]
    elif kind == "missing":
        after = [bar]
    with pytest.raises(ValueError):
        check_shaped_global_repair(before, tuple(after), lanes, problem, host)


def test_changed_straight_needs_actual_host_and_original_q_window():
    before, lanes, problem, host = _case(edge=True)
    with pytest.raises(ValueError, match="complete host"):
        check_shaped_global_repair(before, _shapes(before, host, {"a": 120}), lanes, problem, host)
    before, lanes, problem, host = _case()
    with pytest.raises(ValueError, match="lane window"):
        check_shaped_global_repair(before, _shapes(before, host, {"a": 351}), lanes, problem, host)
    with pytest.raises(ValueError, match="shift exceeds"):
        check_shaped_global_repair(before, _shapes(before, host, {"a": 320}), lanes, problem, host, maximum_shift_mm=200)


def test_U_at_an_interior_hole_edge_cannot_borrow_exterior_certificate():
    before, lanes, problem, host = _case(edge=True)
    host = replace(host, sections=(SolidHostSection(0, 200,
        host.sections[0].footprint.difference(box(1000, -200, 1200, 400))),))
    after = list(_shapes(before, host))
    old = before[0]
    result = build_u_edge_bar(bar_id=old.id, direction=old.direction, steel_class=old.steel_class,
        diameter_mm=10, transverse_axis_mm=100, edge_coordinate_mm=1200, inward_sign=1,
        main_axis_z_mm=155, return_axis_z_mm=45, slab_thickness_mm=200, side_cover_mm=25,
        cut_length_mm=old.installed_length_mm, source_bar_ids=old.source_bar_ids,
        placement_profile_id=ResearchLayerProfile().id)
    after[0] = result.bar
    with pytest.raises(ValueError, match="actual exterior"):
        check_shaped_global_repair(before, tuple(after), lanes, problem, host)


@pytest.mark.parametrize("kwargs", [dict(maximum_shift_mm=True), dict(maximum_shift_mm=301),
    dict(stock_time_limit_s=float("nan")), dict(collision_options={"skip": True}),
    dict(collision_options={"background_bars": ()})])
def test_final_check_has_no_skip_or_weakened_bound(kwargs):
    before, lanes, problem, host = _case()
    with pytest.raises(ValueError):
        check_shaped_global_repair(before, _shapes(before, host), lanes, problem, host, **kwargs)


def test_necessary_region_budget_and_target_are_explicit():
    before, lanes, problem, host = _case()
    current = _shapes(before, host)
    with pytest.raises(ValueError, match="overlay budget"):
        necessary_source_regions(current, lanes, problem,
            bar_key=(before[0].direction, before[0].id), maximum_overlay_operations=1)
    with pytest.raises(ValueError, match="Target"):
        necessary_source_regions(current, lanes, problem, bar_key=(before[0].direction, "missing"))
