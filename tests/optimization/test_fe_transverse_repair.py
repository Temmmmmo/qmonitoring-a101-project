"""Frozen owner FE service permits checked axes without reverting prior repairs."""
from copy import deepcopy
from dataclasses import replace

import pytest
from shapely.affinity import affine_transform
from shapely.geometry import box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.opening_relocation import SourceServiceLane
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.fe_host_repair import installed_fe_coverage
from rebar.optimization.services.fe_transverse_repair import check_fe_transverse_repair
from rebar.optimization.services import fe_transverse_repair
from rebar.optimization.services.opening_relocation import lane_map
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def case(axis=Axis.X, *, axes=(100,), demand_bounds=(1000, 80, 1500, 170), hole=None):
    direction = Direction(Layer.TOP, axis)
    material = box(-1000, -1000, 15000, 6000).difference(
        hole if hole is not None else box(1200, 80, 1300, 120))
    polygon = box(*demand_bounds)
    if axis is Axis.Y:
        material = affine_transform(material, (0, 1, 1, 0, 0, 0))
        polygon = affine_transform(polygon, (0, 1, 1, 0, 0, 0))
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(300, 10),))
    levels = (DemandLevel(0, 1, 0, 1, "background", None, False, replace(recipe, additions=())),
              DemandLevel(1, 2, 1, 2, "addition", recipe.additions[0], True, recipe))
    problems = tuple(LayoutProblem(DemandMap(d, levels,
        (DemandCell(1, tuple(polygon.exterior.coords)[:-1], (polygon.centroid.x, polygon.centroid.y),
                    2 if d == direction else 1, int(d == direction)),), polygon.bounds)) for d in PLATE_DIRECTIONS)
    bars, lanes = [], []
    for index, q in enumerate(axes):
        zone = f"zone-{index}"
        source = PhysicalSourceBar(f"{zone}/0/0", direction, "A500", 10, q,
            (600, 12300), (1000, 1500), 10, 0)
        bars.append(PhysicalBar(f"bar-{index}", direction, "A500", 10, q, (600, 12300), (source.id,)))
        lanes.append(SourceServiceLane(source, zone, 0, 0, 300, (-100, 2000), (150, 150)))
    host = OrthogonalSolidHost((SolidHostSection(0, 200, material),), 25, 25, 25, material.area*200, 10)
    return tuple(bars), tuple(lanes), PlateProblem(problems, case_id="synthetic"), host


def check(data, axes, **kwargs):
    before, lanes, problem, host = data
    return check_fe_transverse_repair(before, tuple(replace(bar, transverse_axis_mm=q)
        for bar, q in zip(before, axes, strict=True)), lanes, problem, host, **kwargs)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_complete_joint_checked_move_preserves_FE40d_inventory_and_inputs(axis):
    data = case(axis)
    original = deepcopy(data)
    report = check(data, (150,))
    assert report["host_blocked_before"] == 1 and report["host_blocked_after"] == 0
    assert report["moved_bar_count"] == 1
    assert report["source_coverage"]["status"] == report["stock_cutting"]["status"] == "pass"
    assert report["all_original_owner_FE_pieces_preserved"] and report["all_installed_intervals_unchanged"]
    assert report["full40d_from_frozen_FE_portions"] == "pass"
    assert report["new_body_pairs"] == report["changed_bars_with_body_collisions"] == 0
    assert report["status"] == "research_checks_passed_not_placement_approved"
    assert not report["placement_eligible"] and not report["legacy_source_certificate_reused"]
    assert not report["source_demand_removed"] and not report["structural_placement_supported"]
    assert data == original


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_previous_FE_based_along_repair_is_not_replaced_by_old_zone_bbox(axis):
    bars, lanes, problem, host = case(axis, demand_bounds=(1100, 80, 1200, 170))
    # Original source needs start<=600; the already accepted FE repair uses700.
    # Its actual core still covers every original FE. Retain that exact interval.
    bars = (replace(bars[0], installed_interval_mm=(700, 12400)),)
    checked = check((bars, lanes, problem, host), (150,))
    assert checked["moves"][0]["installed_interval_mm"] == (700, 12400)
    assert checked["frozen_original_owner_FE_obligations"][0]["owners"][0]["required_FE_interval_mm"] == (1100, 1200)
    assert checked["full40d_from_frozen_FE_portions"] == "pass"


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_global_coverage_cannot_hide_loss_of_one_original_owners_FE_portion(axis):
    data = case(axis, axes=(100, 170))
    after = (replace(data[0][0], transverse_axis_mm=260), data[0][1])
    # The second bar still covers everything, but the first loses y=80..110.
    assert installed_fe_coverage(data[2], after, lane_map(data[1]))["status"] == "pass"
    with pytest.raises(ValueError, match="frozen original owner"):
        check_fe_transverse_repair(data[0], after, *data[1:])


def test_even_tiny_positive_owner_loss_is_rejected_despite_other_full_coverage():
    data = case(axes=(100, 170))
    after = (replace(data[0][0], transverse_axis_mm=230+1e-8), data[0][1])
    assert installed_fe_coverage(data[2], after, lane_map(data[1]))["status"] == "pass"
    with pytest.raises(ValueError, match="frozen original owner"):
        check_fe_transverse_repair(data[0], after, *data[1:])


@pytest.mark.parametrize("limit", ("MAX_TOTAL_CELLS", "MAX_TOTAL_FE_VERTICES", "MAX_FE_INTERSECTION_CHECKS"))
def test_geometry_resource_limit_does_not_truncate_coverage(monkeypatch, limit):
    monkeypatch.setattr(fe_transverse_repair, limit, 0)
    with pytest.raises(ValueError):
        check(case(), (150,))


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_two_individually_safe_moves_cannot_collide_when_applied_together(axis):
    data = case(axis, axes=(100, 200))
    check(data, (150, 200))
    check(data, (100, 159))  # First unchanged host failure remains explicit.
    with pytest.raises(ValueError, match="New or changed-bar"):
        check(data, (150, 159))


def test_existing_pair_does_not_allow_a_moved_bar_to_stay_in_it():
    data = case(axes=(100, 105), hole=box(13000, 80, 13100, 120))
    assert check(data, (100, 105))["same_direction_body_pairs_after"] == 1
    with pytest.raises(ValueError, match="New or changed-bar"):
        check(data, (109, 105))


def test_background_touch_cannot_be_relaxed_to_make_an_axis_fit():
    data = case(axes=(10,), demand_bounds=(1000, 80, 1500, 150))
    with pytest.raises(ValueError, match="background contact"):
        check(data, (50,))


def test_background_penetration_is_rejected_even_if_coverage_remains():
    with pytest.raises(ValueError, match="background contact"):
        check(case(), (0,))


def test_no_demand_owner_is_retained_and_not_moved_as_free_capacity():
    data = case(axes=(100, 700))
    with pytest.raises(ValueError, match="No-demand bars are frozen"):
        check(data, (150, 750))


def test_all_height_sections_are_checked_for_the_new_axis():
    bars, lanes, problem, host = case()
    second = host.sections[0].footprint.difference(box(1500, 140, 1600, 160))
    host = replace(host, sections=(replace(host.sections[0], top_z_mm=100), SolidHostSection(100, 200, second)),
                   volume_mm3=host.sections[0].footprint.area*100+second.area*100)
    with pytest.raises(ValueError, match="Changed bar is outside"):
        check((bars, lanes, problem, host), (150,))


def test_outer_notch_uses_explicit_broader_research_profile_not_small_hole_permission():
    checked = check(case(hole=box(1200, -1000, 1300, 120)), (150,))
    assert checked["host_blocked_after"] == 0
    assert "Broader FE-preserving" in checked["warning"]
    assert not checked["engineering_approval"]


@pytest.mark.parametrize("change", ("missing", "duplicate", "ends", "length", "diameter", "steel", "owner", "nan", "bool"))
def test_malformed_or_non_transverse_changes_are_rejected(change):
    bars, lanes, problem, host = case()
    options = {"ends": {"installed_interval_mm": (601, 12301)},
        "length": {"installed_interval_mm": (600, 12200)}, "diameter": {"diameter_mm": 12},
        "steel": {"steel_class": "A400"}, "owner": {"source_bar_ids": ("invented",)},
        "nan": {"transverse_axis_mm": float("nan")}, "bool": {"transverse_axis_mm": True}}
    changed = replace(bars[0], transverse_axis_mm=150)
    after = () if change == "missing" else (changed, changed) if change == "duplicate" else (
        replace(changed, **options[change]),)
    with pytest.raises(ValueError):
        check_fe_transverse_repair(bars, after, lanes, problem, host)


@pytest.mark.parametrize("kwargs", ({"maximum_shift_mm": True}, {"maximum_shift_mm": float("nan")},
    {"maximum_shift_mm": -1}, {"maximum_shift_mm": 301}, {"stock_time_limit_s": 0},
    {"stock_time_limit_s": float("inf")}))
def test_invalid_resource_and_shift_bounds_fail_closed(kwargs):
    with pytest.raises(ValueError):
        check(case(), (150,), **kwargs)


def test_original_baseline_bounds_apply_even_for_otherwise_valid_moves():
    with pytest.raises(ValueError, match="original-baseline bound"):
        check(case(), (150,), maximum_shift_mm=49)


def test_source_lane_tampering_and_incomplete_ownership_fail_closed():
    bars, lanes, problem, host = case()
    for invalid in ((replace(lanes[0], service_half_widths_mm=(140, 160)),),
                    (replace(lanes[0], source=replace(lanes[0].source, installed_interval_mm=(601, 12301))),),
                    (*lanes, replace(lanes[0], zone_id="unused", source=replace(lanes[0].source, id="unused/0/0")))):
        with pytest.raises(ValueError):
            check_fe_transverse_repair(bars, bars, invalid, problem, host)


def test_a_lost_baseline_FE_cannot_be_silently_recertified():
    bars, lanes, problem, host = case()
    bars = (replace(bars[0], installed_interval_mm=(1200, 12900)),)
    with pytest.raises(ValueError, match="Complete original FE coverage"):
        check_fe_transverse_repair(bars, bars, lanes, problem, host)


@pytest.mark.parametrize("bad_level", (-1, 99, True))
def test_unknown_original_FE_levels_remain_errors(bad_level):
    bars, lanes, problem, host = case()
    target = problem.problem(bars[0].direction)
    broken = replace(target, demand=replace(target.demand,
        cells=(replace(target.demand.cells[0], level_index=bad_level),)))
    problem = replace(problem, direction_problems=tuple(broken if p == target else p for p in problem.direction_problems))
    with pytest.raises(ValueError):
        check_fe_transverse_repair(bars, bars, lanes, problem, host)
