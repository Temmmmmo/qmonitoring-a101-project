from copy import deepcopy
from dataclasses import replace
from itertools import product
import math

import pytest

from rebar.models import Axis, Direction, Layer
from rebar.optimization.algorithms.composite_partition import solve_composite_partition
from rebar.optimization.algorithms.composite_pool import solve_composite_pool
from rebar.optimization.services.composite_coverage import check_composite_zone_coverage_geometry, evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone, prepare_composite_detailing
from rebar.optimization.services.composite_grid import grid_bbox, make_fragment_grid
from rebar.optimization.services.composite_host import evaluate_composite_host
from rebar.optimization.services.composite_interior import partition_interior_demand
from rebar.optimization.services.geometry import polygon_area, polygon_in_bbox

from test_composite_host import host_sample
from test_composite_search import problem_sample


def fragment_sample(axis=Axis.X, layer=Layer.TOP):
    p = problem_sample()
    cells = [replace(c, poly=tuple((x + 1500, y + 400) for x, y in c.poly),
                     centroid=(c.centroid[0] + 1500, c.centroid[1] + 400)) for c in p.demand.cells]
    cells.append(replace(cells[0], id=999, poly=((0, 400), (400, 400), (400, 800), (0, 800)), centroid=(200, 600)))
    outer = (0, 0, 9000, 3000)
    if axis is Axis.Y:
        cells = [replace(c, poly=tuple((y, x) for x, y in c.poly), centroid=tuple(reversed(c.centroid))) for c in cells]
        outer = (0, 0, 3000, 9000)
    placements = tuple((i, replace(pl, additions=tuple(replace(a, origin_mm=-50) if j else a
                                                     for j, a in enumerate(pl.additions)))) for i, pl in p.placements)
    return replace(p, placements=placements, demand=replace(p.demand, cells=tuple(cells), direction=Direction(layer, axis)),
                   host_envelope=host_sample(outer_mm=outer), boundary_mode="interior-exceptions")


@pytest.mark.parametrize("axis,layer", product((Axis.X, Axis.Y), (Layer.TOP, Layer.BOTTOM)))
def test_fragments_solve_length_limited_case_without_changing_target_or_anchorage(axis, layer):
    p = fragment_sample(axis, layer)
    before = deepcopy(p)
    assert not solve_composite_pool(p, maximum_candidates=16, maximum_bar_length_mm=3800).points
    result = solve_composite_partition(p, along_step_mm=1500, across_step_mm=900, maximum_bar_length_mm=3800,
                                      zone_penalties=(0, 300), bar_penalties=(0,))
    assert result.points and p == before and not result.placement_eligible
    assert result.telemetry["interior_partition"] == partition_interior_demand(p)
    assert result.telemetry["fragment_grid"]["fragment_count"] > 2
    for point in result.points:
        check = evaluate_composite_coverage(p.demand, point.zones, policy_id=p.policy_id, constraints=p.constraints)
        assert check == point.coverage and check.uncovered_cell_count == 1
        assert all(c.covered for c in check.cells if c.cell_id != 999)
        assert len(point.zones) >= 3
        assert max(c.installed_length_mm for z in point.zones for c in z.components) <= 3800
        assert all(c.anchored_length_mm == pytest.approx(c.required_length_mm + 80 * c.rebar.diameter)
                   for z in point.zones for c in z.components)
        host = evaluate_composite_host(p.demand, point.zones, p.host_envelope)
        assert host["checks"]["same_direction_zone_overlap"] == "pass"
        assert host["checks"]["planar_host_and_openings"] == "pass"
        assert host["unknown_depth_projected_interzone_conflicts"]["pair_count"] > 0
        assert host["checks"]["additional_bar_collisions"] == "not_checked"


def test_grid_intersection_uses_polygons_not_centroids_and_preserves_sliver_area():
    p = fragment_sample()
    # A thin positive-area triangle crossing an atom boundary must not disappear.
    cell = replace(p.demand.cells[0], poly=((2524.9, 500), (2525.1, 500), (2525, 501)), centroid=(999999, 999999))
    p = replace(p, demand=replace(p.demand, cells=(cell, *p.demand.cells[1:])))
    part = partition_interior_demand(p)
    grid = make_fragment_grid(p, part, along_step_mm=1500, across_step_mm=900)
    assert grid["required_masks"][0][0] & (1 << cell.level_index)
    assert grid["required_masks"][0][1] & (1 << cell.level_index)
    assert grid["target_area_mm2"] == pytest.approx(sum(polygon_area(c.poly) for c in p.demand.cells if c.id in part["target_cell_ids"]))


def test_empty_interior_and_opening_do_not_produce_false_feasible_layout():
    p = fragment_sample()
    empty = replace(p, demand=replace(p.demand, cells=(p.demand.cells[-1],)))
    assert not solve_composite_partition(empty).points
    blocked = replace(p, host_envelope=host_sample(outer_mm=(0, 0, 9000, 3000), openings_mm=((1200, 200, 5700, 1800),)))
    result = solve_composite_partition(blocked, zone_penalties=(0,), bar_penalties=(0,))
    assert not result.points and result.telemetry["rejected_proposals"] > 0


@pytest.mark.parametrize("options", [{"maximum_physical_bars": 1}, {"maximum_zones": 1}, {"maximum_mass_kg": 1}])
def test_quality_limits_are_not_relaxed_to_return_a_partition(options):
    result = solve_composite_partition(fragment_sample(), maximum_bar_length_mm=3800, zone_penalties=(0,), bar_penalties=(0,), **options)
    assert not result.points
    assert result.telemetry["scalar_runs"][0]["rejection"] == "hard_budget_not_relaxed"


@pytest.mark.parametrize("options", [{"along_step_mm": 0}, {"across_step_mm": float("nan")}, {"along_step_mm": 1},
    {"maximum_zones": True}, {"maximum_zones": 129}, {"maximum_mass_kg": -1}, {"maximum_bar_length_mm": float("inf")},
    {"zone_penalties": ()}, {"bar_penalties": (float("nan"),)}, {"zone_penalties": tuple(range(33))}, {"time_limit_s": 301},
    {"maximum_zones": None}, {"time_limit_s": None}])
def test_bad_grids_and_budgets_fail_explicitly(options):
    with pytest.raises(ValueError):
        solve_composite_partition(fragment_sample(), **options)


def test_timeout_does_not_return_a_partial_dynamic_program_as_a_solution():
    result = solve_composite_partition(fragment_sample(), time_limit_s=1e-9)
    assert not result.points and result.telemetry["timed_out"] and not result.placement_eligible


def test_small_grid_matches_independent_exhaustive_rectangle_cover_oracle():
    p = fragment_sample()
    prototype = p.demand.cells[0]
    cells = []
    for i, j in product(range(2), range(4)):
        box = (1025 + 1000*i, 100 + 450*j, 2025 + 1000*i, 550 + 450*j)
        poly = ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))
        cells.append(replace(prototype, id=10*i+j, poly=poly, centroid=(box[0]+500, box[1]+225), level_index=1 if i == 0 else 2))
    p = replace(p, demand=replace(p.demand, cells=tuple(cells)), host_envelope=host_sample(outer_mm=(0, 0, 4050, 2025)))
    part = partition_interior_demand(p)
    grid = make_fragment_grid(p, part, along_step_mm=1000, across_step_mm=900)
    assert len(grid["along_coordinates_mm"]) == len(grid["across_coordinates_mm"]) == 3
    candidates = []
    # Exhaust all rectangles and full-level assignments on a 2x2 grid, without
    # the optimizer's DP or state choices. Enumerate exact covers of the four tiles.
    for a, b, c, d in product(range(2), range(1, 3), range(2), range(1, 3)):
        if a >= b or c >= d:
            continue
        box = grid_bbox(Axis.X, grid["along_coordinates_mm"], grid["across_coordinates_mm"], (a,b,c,d))
        for level, placement in p.placements:
            zone = build_composite_zone(p.demand, box, level, f"O-{a}-{b}-{c}-{d}-{level}", placement)
            if max(comp.installed_length_mm for comp in zone.components) > 3500:
                continue
            check = check_composite_zone_coverage_geometry(p.demand, zone)
            if not check.geometry_and_pattern_valid or any(not polygon_in_bbox(
                    ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])), body)
                    for body in check.component_service_bboxes_mm):
                continue
            whole = evaluate_composite_coverage(p.demand, (zone,), policy_id=p.policy_id)
            inside = [cell for cell in p.demand.cells if polygon_in_bbox(cell.poly, box)]
            if any(not cell.covered for cell in whole.cells if cell.cell_id in {c.id for c in inside}):
                continue
            mask = sum(1 << (2*y+x) for x in range(a,b) for y in range(c,d))
            candidates.append((mask, check.additional_mass_kg))

    def brute(mask):
        if mask == 15:
            return 0.0
        first = next(1 << i for i in range(4) if not mask & (1 << i))
        return min((cost + brute(mask | covered) for covered, cost in candidates if covered & first and not covered & mask), default=math.inf)

    oracle = brute(0)
    result = solve_composite_partition(p, along_step_mm=1000, across_step_mm=900, maximum_bar_length_mm=3500,
                                      zone_penalties=(0,), bar_penalties=(0,))
    assert math.isfinite(oracle) and result.points
    assert min(point.coverage.additional_mass_kg for point in result.points) == pytest.approx(oracle)


def test_cached_detailing_matches_original_and_cannot_supply_a_forged_final_width():
    p = fragment_sample()
    context = prepare_composite_detailing(p.demand)
    placement = dict(p.placements)[1]
    box = (1500, 100, 2500, 1000)
    zone = build_composite_zone(p.demand, box, 1, "same", placement)
    assert build_composite_zone(p.demand, box, 1, "same", placement, context=context) == zone
    with pytest.raises(ValueError, match="контекст"):
        build_composite_zone(replace(p.demand), box, 1, "wrong", placement, context=context)
    forged = replace(context, typical_transverse_size_mm=1)
    narrow = build_composite_zone(p.demand, (1500, 100, 2500, 200), 1, "forged", placement, context=forged)
    # The original-demand independent checker never accepts optimizer context.
    assert not evaluate_composite_coverage(p.demand, (narrow,), policy_id=p.policy_id).geometry_and_patterns_valid


def test_known_coplanar_depths_reject_the_same_partition_instead_of_approving_laps():
    p = fragment_sample()
    p = replace(p, placements=tuple((i, replace(pl, additions=tuple(
        replace(a, axis_depth_from_face_mm=(34,65)[j]) for j,a in enumerate(pl.additions)))) for i,pl in p.placements))
    result = solve_composite_partition(p, along_step_mm=1500, across_step_mm=900, maximum_bar_length_mm=3800,
                                      zone_penalties=(0,), bar_penalties=(0,))
    assert not result.points and result.telemetry["rejected_proposals"] == 1


def test_projected_diagnostic_is_bounded_and_does_not_turn_unknown_z_into_a_pass(monkeypatch):
    p = fragment_sample()
    result = solve_composite_partition(p, along_step_mm=1500, across_step_mm=900, maximum_bar_length_mm=3800,
                                      zone_penalties=(0,), bar_penalties=(0,))
    zones = result.points[0].zones
    report = evaluate_composite_host(p.demand, zones, p.host_envelope)
    assert len(report["unknown_depth_projected_interzone_conflicts"]["examples"]) <= 30
    monkeypatch.setattr("rebar.optimization.services.composite_host.MAX_PAIR_CHECKS", 1)
    limited = evaluate_composite_host(p.demand, zones, p.host_envelope)
    assert limited["unknown_depth_projected_interzone_conflicts"]["pair_count"] is None
    assert limited["unknown_depth_projected_interzone_conflicts"]["status"] == "not_checked"
    assert limited["checks"]["additional_bar_collisions"] == "not_checked"
