from dataclasses import replace

import pytest
from shapely.geometry import box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.algorithms.shaped_edge_repair import propose_exterior_u_repair
from rebar.optimization.contracts.opening_relocation import SourceServiceLane
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.shaped_fe_repair import (
    ResearchLayerProfile, check_shaped_fe_repair, exterior_edge_choices, layer_elevations,
    shaped_service_offers,
)
from rebar.optimization.services.shaped_geometry import build_u_edge_bar, straight_bar_from_physical
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def case(axis=Axis.X, layer=Layer.TOP):
    direction = Direction(layer, axis)
    source = PhysicalSourceBar("zone/0/0", direction, "A500", 10, 100,
        (-300, 2625), (100, 1500), 10, 0)
    before = (PhysicalBar("bar", direction, "A500", 10, 100, (-300, 2625), (source.id,)),)
    lanes = (SourceServiceLane(source, "zone", 0, 0, 300, (-50, 250), (150, 150)),)
    polygon = box(100, 0, 1500, 200) if axis is Axis.X else box(0, 100, 200, 1500)
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(300, 10),))
    levels = (DemandLevel(0, 1, 0, 1, "background", None, False, replace(recipe, additions=())),
        DemandLevel(1, 2, 1, 2, "addition", recipe.additions[0], True, recipe))
    problems = tuple(LayoutProblem(DemandMap(d, levels, (DemandCell(1, tuple(polygon.exterior.coords)[:-1],
        (polygon.centroid.x, polygon.centroid.y), 2 if d == direction else 1, int(d == direction)),),
        polygon.bounds)) for d in PLATE_DIRECTIONS)
    material = box(0, -1000, 5000, 1000) if axis is Axis.X else box(-1000, 0, 1000, 5000)
    host = OrthogonalSolidHost((SolidHostSection(0, 200, material),), 40, 40, 25,
                              material.area*200, 6)
    return before, lanes, PlateProblem(direction_problems=problems, case_id="synthetic"), host


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
@pytest.mark.parametrize("layer", (Layer.TOP, Layer.BOTTOM))
def test_true_stock_U_repair_retains_all_FE_and_only_changes_shape(axis, layer):
    before, lanes, problem, host = case(axis, layer)
    after, search = propose_exterior_u_repair(before, lanes, problem, host)
    assert search["candidate_checks"] > 0
    checked = check_shaped_fe_repair(before, after, lanes, problem, host)
    assert checked["U_substitution_count"] == 1
    assert checked["shaped_host_not_proven_before"] == 1
    assert checked["shaped_host_not_proven_after"] == 0
    assert checked["source_coverage"]["status"] == "pass"
    assert checked["all_original_owner_FE_fragments_preserved"]
    assert checked["physical_stock_inventory_preserved"]
    assert checked["physical_metrics"]["physical_bar_count"] == 1
    assert checked["physical_metrics"]["true_cut_length_mm"] == pytest.approx(2925)
    assert not checked["arc_bridge_and_return_demand_credit"]
    assert not checked["placement_eligible"] and not checked["engineering_approval"]
    assert checked["bent_end_anchorage_capacity"] == "not_checked"
    assert "whole_party_3D_collisions" in checked["not_checked"]


def test_complete_exterior_not_bbox_and_closed_hole_not_anchor_choice():
    _, _, _, host = case()
    material = box(0, 0, 5000, 1000).difference(box(1000, 100, 1200, 500))
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    assert exterior_edge_choices(host, Direction(Layer.TOP, Axis.X), 300, 10) == ((0, 1), (5000, -1))
    assert exterior_edge_choices(host, Direction(Layer.TOP, Axis.X), 20, 10) == ()
    material = material.difference(box(4000, 0, 5000, 500))
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    assert exterior_edge_choices(host, Direction(Layer.TOP, Axis.X), 300, 10) == ((0, 1), (4000, -1))


def test_large_inner_axis_U_does_not_fit_so_original_bar_remains_explicit():
    before, lanes, problem, host = case(Axis.Y)
    # Explicit layer geometry alone: maxD16 consumes too much inner-layer height.
    zm, zr = layer_elevations(host, before[0].direction, 16, ResearchLayerProfile())
    result = build_u_edge_bar(bar_id="candidate", direction=before[0].direction,
        steel_class="A500", diameter_mm=16, transverse_axis_mm=100, edge_coordinate_mm=0,
        inward_sign=1, main_axis_z_mm=zm, return_axis_z_mm=zr, slab_thickness_mm=200,
        side_cover_mm=25, cut_length_mm=2925, source_bar_ids=("owner",),
        placement_profile_id=ResearchLayerProfile().id)
    assert result.bar is None and result.status == "blocked_geometry"


@pytest.mark.parametrize("mutation", ("owner", "diameter", "cut", "q", "z", "profile", "shape", "duplicate", "missing"))
def test_independent_checker_rejects_forged_shape_or_inventory(mutation):
    before, lanes, problem, host = case()
    after, _ = propose_exterior_u_repair(before, lanes, problem, host)
    bar = after[0]
    if mutation == "owner":
        bar = replace(bar, source_bar_ids=("invented",))
    elif mutation == "diameter":
        bar = replace(bar, diameter_mm=12)
    elif mutation == "cut":
        bar = replace(bar, selected_cut_length_mm=3900)
    elif mutation in ("q", "z"):
        p = list(bar.segments[0].start_mm)
        p[1 if mutation == "q" else 2] += 1
        bar = replace(bar, segments=(replace(bar.segments[0], start_mm=tuple(p)), *bar.segments[1:]))
    elif mutation == "profile":
        bar = replace(bar, placement_profile_id="invented")
    elif mutation == "shape":
        bar = replace(bar, shape_kind="G")
    malformed = (bar, bar) if mutation == "duplicate" else () if mutation == "missing" else (bar,)
    with pytest.raises(ValueError):
        check_shaped_fe_repair(before, malformed, lanes, problem, host)


def test_other_owners_cannot_hide_a_positive_frozen_fragment_loss():
    before, lanes, problem, host = case()
    after, _ = propose_exterior_u_repair(before, lanes, problem, host)
    # Extending the original FE demand to the old 40d core limit would require
    # more of the main branch; neither the return nor arc length receives credit.
    original = problem.problem(before[0].direction)
    polygon = box(100, 0, 2225, 200)
    changed = replace(original, demand=replace(original.demand, cells=(replace(
        original.demand.cells[0], poly=tuple(polygon.exterior.coords)[:-1]),)))
    problem = replace(problem, direction_problems=tuple(changed if p is original else p for p in problem.direction_problems))
    source = replace(lanes[0].source, required_interval_mm=(100, 2225))
    lanes = (replace(lanes[0], source=source),)
    with pytest.raises(ValueError, match="positive frozen"):
        check_shaped_fe_repair(before, after, lanes, problem, host)


def test_straight_representation_preserves_legacy_service_core():
    before, lanes, _, host = case()
    from rebar.optimization.services.fe_host_repair import _installed_offers
    from rebar.optimization.services.opening_relocation import lane_map
    zm, _ = layer_elevations(host, before[0].direction, 10, ResearchLayerProfile())
    shaped = straight_bar_from_physical(before[0], axis_z_mm=zm,
        placement_profile_id=ResearchLayerProfile().id)
    old = _installed_offers(before[0], lane_map(lanes))
    new = shaped_service_offers(shaped, lane_map(lanes))
    assert len(old) == len(new)
    assert all(a[:2] == b[:2] and a[2].equals(b[2]) for a, b in zip(old, new))


@pytest.mark.parametrize("value", (True, 0, -1, 100001))
def test_bounded_search_never_silently_truncates(value):
    with pytest.raises(ValueError, match="budget"):
        propose_exterior_u_repair(*case(), maximum_candidate_checks=value)


def test_profile_is_explicit_and_cannot_be_weakened_under_unchanged_id():
    before, _, _, host = case()
    with pytest.raises(ValueError, match="Exact explicit"):
        layer_elevations(host, before[0].direction, 10,
            replace(ResearchLayerProfile(), outer_envelope_diameter_mm=10))


def test_global_repair_preserves_source_without_freezing_redundant_owner_regions():
    from rebar.optimization.algorithms.shaped_global_repair import propose_global_shaped_repair
    from rebar.optimization.services.collision_replacement import freeze_owner_fe_service
    from rebar.optimization.services.opening_relocation import coverage_from_offers, lane_map
    from rebar.optimization.services.shaped_collisions import check_shaped_collisions
    from rebar.optimization.services.shaped_fe_repair import owner_fragments_preserved
    from rebar.optimization.services.shaped_geometry import check_shaped_host

    before, lanes, problem, host = case()
    second_source = replace(lanes[0].source, id="zone2/0/0")
    before = (*before, replace(before[0], id="second", source_bar_ids=(second_source.id,)))
    lanes = (*lanes, replace(lanes[0], source=second_source, zone_id="zone2"))
    original = problem.problem(before[0].direction)
    polygon = box(100, -50, 1500, 250)
    changed = replace(original, demand=replace(original.demand, cells=(replace(
        original.demand.cells[0], poly=tuple(polygon.exterior.coords)[:-1]),)))
    problem = replace(problem, direction_problems=tuple(changed if p is original else p for p in problem.direction_problems))
    after, search = propose_global_shaped_repair(before, lanes, problem, host, time_limit_s=20)
    assert len(after) == 2 and all(b.shape_kind == "U" for b in after)
    assert all(check_shaped_host(b, host)["whole_body_with_cover_contained"] for b in after)
    assert check_shaped_collisions(after)["status"] == "pass"
    sources = lane_map(lanes)
    offered = {before[0].direction: [o for bar in after for o in shaped_service_offers(bar, sources)]}
    assert coverage_from_offers(problem, offered, policy="test")["status"] == "pass"
    frozen = freeze_owner_fe_service(before, lanes, problem)
    assert not all(owner_fragments_preserved(bar, frozen, sources) for bar in after)
    assert not search["source_field_transferred"] and not search["old_owner_positive_fragments_frozen"]
    assert not search["budget_exhausted"] and not search["placement_eligible"]


def test_global_search_budget_keeps_a_complete_unapproved_incumbent():
    from rebar.optimization.algorithms.shaped_global_repair import propose_global_shaped_repair
    before, lanes, problem, host = case()
    after, report = propose_global_shaped_repair(before, lanes, problem, host,
                                                maximum_candidates=1, time_limit_s=20)
    assert len(after) == len(before)
    assert report["budget_exhausted"] and not report["placement_eligible"]
