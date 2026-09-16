from dataclasses import replace
import hashlib
import json

import pytest

from rebar.application.boundary_trim_web import _graphic_packet
from rebar.application.flat_mvp import FlatMvpLayers, flat_mvp_elevations
from rebar.application.flat_trim_repair_web import build_repaired_graphic_packet
from rebar.application.physical_layout_recovery import _bytes
from rebar.optimization.algorithms.flat_trim_repair import repair_flat_trimmed_bars
from rebar.optimization.algorithms.tz_boundary_trim import trim_straight_bars_to_outer_boundary
from rebar.optimization.services.flat_trim_repair import check_flat_trim_repair
from rebar.optimization.services.shaped_geometry import straight_bar_from_physical
from rebar.optimization.algorithms.trimmed_repair import _bar
from rebar.optimization.services.solid_host import SolidHostSection
from rebar.optimization.services.shaped_fe_repair import ResearchLayerProfile, layer_elevations
from rebar.optimization.services.tz_boundary_trim import check_boundary_trim
from test_shaped_global_coverage import _case


def tiny_repaired_case():
    physical, lanes, problem, host = _case(demand_bounds=(500, -50, 2000, 350))
    host = replace(host, sections=(SolidHostSection(0, 800, host.sections[0].footprint),),
                   top_cover_mm=0, bottom_cover_mm=0, side_cover_mm=0, volume_mm3=8e9)
    physical = (replace(physical[0], installed_interval_mm=(25, 900)), physical[1])
    before = tuple(straight_bar_from_physical(bar, axis_z_mm=flat_mvp_elevations(
        host, bar.direction, bar.diameter_mm, FlatMvpLayers())[0], placement_profile_id=FlatMvpLayers().id)
        for bar in physical)
    after, checks = repair_flat_trimmed_bars(before, lanes, problem, host,
        elevations=flat_mvp_elevations, profile=FlatMvpLayers(), maximum_mass_increase_pct=100,
        maximum_candidates=500, time_limit_s=10, stock_time_limit_s=.1)
    unchanged, mapping = trim_straight_bars_to_outer_boundary(before, host)
    assert unchanged == before
    baseline = check_boundary_trim(before, before, mapping, lanes, problem, host, stock_time_limit_s=.1)
    source = _graphic_packet(problem, before, before, baseline, "1"*64, "2"*64, 2)
    packet = build_repaired_graphic_packet(source, before, after, checks, lanes, problem)
    return before, after, checks, lanes, problem, host, packet


def test_generic_addition_closes_deficit_and_retains_failed_independent_rules():
    before, after, checks, _, _, _, packet = tiny_repaired_case()
    assert len(after) > len(before)
    assert checks["accepted_nonregression"]
    assert checks["geometric_presence"]["status"] == "pass"
    assert checks["previously_covered_area_lost_mm2"] == {"geometric_presence": 0, "control_40d": 0}
    assert checks["new_collision_pair_count"] == checks["grown_collision_pair_count"] == 0
    assert checks["material_boundary_failures_after"] == 0
    assert checks["stock_cutting"]["status"] != "pass"
    assert packet["schema_version"] == "graphic-bar-plan-repaired/v1"
    assert all("original_bar_id" not in bar for row in packet["directions"] for bar in row["bars"])
    assert len(packet["operations"]) == len(before) and packet["additions"]
    assert packet["source_trim_packet_sha256"] == hashlib.sha256(packet["source_trim_packet_json"].encode()).hexdigest()
    assert json.loads(packet["source_trim_packet_json"]) == packet["source_trim_packet"]
    assert packet["repair"]["checker_sha256"] == hashlib.sha256(_bytes(checks)).hexdigest()
    assert not packet["placement_eligible"] and not packet["engineering_approval"]


def test_checker_rejects_forged_owner_and_removal_without_tolerance_changes():
    before, after, _, lanes, problem, host, _ = tiny_repaired_case()
    kwargs = dict(elevations=flat_mvp_elevations, profile=FlatMvpLayers(), maximum_mass_kg=100,
                  maximum_axis_nudge_mm=.01, stock_time_limit_s=.1)
    with pytest.raises(ValueError, match="Unknown"):
        check_flat_trim_repair(before, (*after[:-1], replace(after[-1], source_bar_ids=("forged",))),
                              lanes, problem, host, **kwargs)
    with pytest.raises(ValueError, match="remove"):
        check_flat_trim_repair(before, after[1:], lanes, problem, host, **kwargs)


def test_zero_addition_budget_keeps_baseline_and_explicit_remaining_deficit():
    before, _, _, lanes, problem, host, _ = tiny_repaired_case()
    after, checks = repair_flat_trimmed_bars(before, lanes, problem, host,
        elevations=flat_mvp_elevations, profile=FlatMvpLayers(), maximum_additions=0,
        maximum_candidates=10, time_limit_s=1, stock_time_limit_s=.1)
    assert after == before and checks["geometric_presence"]["status"] == "fail"
    assert checks["search"]["global_optimality_claimed"] is False


def test_checker_enforces_new_length_without_rejecting_original_short_fragments():
    before, after, _, lanes, problem, host, _ = tiny_repaired_case()
    addition = next(b for b in after if b.id.startswith("flat-repair-"))
    tiny = _bar(addition, addition.segments[0].start_mm[1], 900, 901, addition.id)
    with pytest.raises(ValueError, match="New bar length"):
        check_flat_trim_repair(before, (*before, tiny), lanes, problem, host,
            elevations=flat_mvp_elevations, profile=FlatMvpLayers(), maximum_mass_kg=100,
            maximum_axis_nudge_mm=.01, stock_time_limit_s=.1)


def test_repaired_builder_rejects_tampered_immutable_trim_geometry():
    before, after, checks, lanes, problem, _, packet = tiny_repaired_case()
    source = packet["source_trim_packet"]
    source["directions"][2]["bars"][0]["coordinate_mm"] += 1
    with pytest.raises(ValueError, match="Exact trimmed"):
        build_repaired_graphic_packet(source, before, after, checks, lanes, problem)


def test_public_axis_nudge_limit_cannot_expand_confirmed_policy():
    before, _, _, lanes, problem, host, _ = tiny_repaired_case()
    kwargs = dict(elevations=flat_mvp_elevations, profile=FlatMvpLayers(),
                  maximum_axis_nudge_mm=.011, stock_time_limit_s=.1)
    with pytest.raises(ValueError, match="budget"):
        repair_flat_trimmed_bars(before, lanes, problem, host, **kwargs)
    with pytest.raises(ValueError, match="limits"):
        check_flat_trim_repair(before, before, lanes, problem, host, maximum_mass_kg=100, **kwargs)


def test_exact_deficit_edge_nudge_is_coordinate_operation_not_tolerance():
    physical, lanes, problem, host = _case(demand_bounds=(500, -50, 2000, 350.001))
    host = replace(host, sections=(SolidHostSection(0, 800, host.sections[0].footprint),),
                   top_cover_mm=0, bottom_cover_mm=0, side_cover_mm=0, volume_mm3=8e9)
    before = tuple(straight_bar_from_physical(bar, axis_z_mm=flat_mvp_elevations(
        host, bar.direction, bar.diameter_mm, FlatMvpLayers())[0], placement_profile_id=FlatMvpLayers().id)
        for bar in physical)
    after, checks = repair_flat_trimmed_bars(before, lanes, problem, host, elevations=flat_mvp_elevations,
        profile=FlatMvpLayers(), maximum_additions=0, maximum_candidates=500, time_limit_s=10, stock_time_limit_s=.1)
    assert len(after) == len(before)
    assert after[1].segments[0].start_mm[1] == 350.001-150
    assert checks["geometric_presence"]["status"] == "pass"
    assert checks["geometric_presence"]["positive_area_loss_tolerance_mm2"] == 0
    assert checks["previously_covered_area_lost_mm2"] == {"geometric_presence": 0, "control_40d": 0}


def test_generic_repair_uses_explicit_k09_200mm_profile_not_s1_elevations():
    physical, lanes, problem, host = _case(demand_bounds=(500, -50, 2000, 350))
    physical = (replace(physical[0], installed_interval_mm=(25, 900)), physical[1])
    profile = ResearchLayerProfile()
    before = tuple(straight_bar_from_physical(bar, axis_z_mm=layer_elevations(
        host, bar.direction, bar.diameter_mm, profile)[0], placement_profile_id=profile.id) for bar in physical)
    after, checks = repair_flat_trimmed_bars(before, lanes, problem, host, elevations=layer_elevations,
        profile=profile, maximum_mass_increase_pct=100, maximum_candidates=500, time_limit_s=10, stock_time_limit_s=.1)
    assert checks["geometric_presence"]["status"] == "pass"
    assert all(bar.segments[0].start_mm[2] == 155 for bar in after)
    assert all(bar.placement_profile_id == profile.id for bar in after)
