from copy import deepcopy
from dataclasses import replace
from itertools import combinations, product
import math

import pytest

from rebar.models import Axis, Layer
from rebar.optimization.algorithms.composite_joint_depths import solve_composite_joint_depths
from rebar.optimization.algorithms.composite_partition import solve_composite_partition
from rebar.optimization.services.axis_patterns import pattern_coordinates
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_grid import make_fragment_grid
from rebar.optimization.services.composite_host import evaluate_composite_host
from rebar.optimization.services.composite_interior import partition_interior_demand
from rebar.optimization.services.composite_joint_graph import component_distance_graph

from test_composite_partition import fragment_sample


def joint_sample(axis=Axis.X, layer=Layer.TOP):
    p = fragment_sample(axis, layer)
    pl = dict(p.placements)[2]
    pl = replace(pl, additions=(pl.additions[0], replace(pl.additions[1], origin_mm=150)))
    zones = []
    for i, j in product(range(2), repeat=2):
        box = (1500 + i*1950, 300 + j*900, 3450 + i*1950, 1200 + j*900)
        if axis is Axis.Y:
            box = (box[1], box[0], box[3], box[2])
        zones.append(build_composite_zone(p.demand, box, 2, f"J-{i}-{j}", pl))
    return replace(p, constraints=replace(p.constraints, minimum_clear_spacing_mm=25)), tuple(zones)


def solve(p, zones, **kwargs):
    options = dict(candidate_depths_by_component=((34, 77), (37.5, 87.5)),
                   hypothesis_source="synthetic depth hypothesis, not engineering approval", preserve_component_order=True)
    options.update(kwargs)
    return solve_composite_joint_depths(p.demand, zones, p.host_envelope, policy_id=p.policy_id, constraints=p.constraints, **options)


@pytest.mark.parametrize("axis,layer", product((Axis.X, Axis.Y), (Layer.TOP, Layer.BOTTOM)))
def test_joint_assignment_preserves_all_demand_axes_lengths_and_mass(axis, layer):
    p, zones = joint_sample(axis, layer)
    before = deepcopy((p, zones))
    base = evaluate_composite_coverage(p.demand, zones, policy_id=p.policy_id, constraints=p.constraints)
    result = solve(p, zones)
    assert result.points and not result.placement_eligible and (p, zones) == before
    point = result.points[0]
    assert point.coverage == base and base.uncovered_cell_count == 1
    assert result.telemetry["maximum_axis_depth_mm"] == 87.5
    host = evaluate_composite_host(p.demand, point.zones, p.host_envelope, constraints=p.constraints)
    assert host["checks"]["additional_bar_collisions"] == host["checks"]["top_bottom_cover"] == "pass"
    assert host["checks"]["background_and_other_directions"] == "not_checked"
    for original, assigned in zip(zones, point.zones):
        assert original.demand_bbox == assigned.demand_bbox and original.recipe == assigned.recipe
        assert assigned.placement.additions[0].axis_depth_from_face_mm < assigned.placement.additions[1].axis_depth_from_face_mm
        for a, b in zip(original.components, assigned.components):
            assert replace(b, placement=a.placement) == a


def test_two_zone_exact_depth_oracle_uses_independent_full_host_checker():
    p, zones = joint_sample()
    zones = (zones[0], zones[2])
    minimum = math.inf
    for depths in product((34, 77), (37.5, 87.5), (34, 77), (37.5, 87.5)):
        if any(depths[i] >= depths[i+1] for i in (0, 2)):
            continue
        candidate = tuple(build_composite_zone(p.demand, z.demand_bbox, z.level_index, z.id,
            replace(z.placement, additions=tuple(replace(a, axis_depth_from_face_mm=depths[2*i+j])
                                                  for j, a in enumerate(z.placement.additions)))) for i, z in enumerate(zones))
        check = evaluate_composite_host(p.demand, candidate, p.host_envelope, constraints=p.constraints)
        if check["checks"]["additional_bar_collisions"] == "pass":
            minimum = min(minimum, max(depths))
    result = solve(p, zones)
    assert minimum == 87.5 and result.telemetry["maximum_axis_depth_mm"] == minimum


def test_phase_and_grid_joint_hypothesis_is_rechecked_end_to_end_on_public_cells():
    p = fragment_sample()
    settings = dict(along_step_mm=2200, across_step_mm=900, across_origin_mm=300,
                    maximum_bar_length_mm=4500, zone_penalties=(0,), bar_penalties=(0,))
    control = solve_composite_partition(p, **settings)
    shifted = replace(p, placements=tuple((i, replace(pl, additions=tuple(replace(a, origin_mm=150) if j else a
        for j, a in enumerate(pl.additions)))) for i, pl in p.placements))
    searched = solve_composite_partition(shifted, **settings)
    assert searched.points
    assert searched.telemetry["interior_partition"]["target_cell_ids"] == control.telemetry["interior_partition"]["target_cell_ids"]
    for point in searched.points:
        geometry = solve(replace(shifted, constraints=replace(p.constraints, minimum_clear_spacing_mm=25)), point.zones)
        assert geometry.points
        assert geometry.points[0].coverage.cells == point.coverage.cells


def test_component_graph_matches_naive_all_axis_pairs():
    p, zones = joint_sample()
    graph = component_distance_graph(zones, minimum_clear_spacing_mm=25)
    edges = {(i, j): d for i, j, d, _ in graph["edges"]}
    for i, j in combinations(range(len(graph["nodes"])), 2):
        a, b = graph["nodes"][i][2], graph["nodes"][j][2]
        gap = max(a.longitudinal_interval_mm[0] - b.longitudinal_interval_mm[1],
                  b.longitudinal_interval_mm[0] - a.longitudinal_interval_mm[1], 0)
        minimum = min(math.hypot(gap, x-y) for x, y in product(pattern_coordinates(a.placement, a.axis_window_mm),
                                                             pattern_coordinates(b.placement, b.axis_window_mm)))
        conflict = minimum < (a.rebar.diameter + b.rebar.diameter)/2 + 25 - 0.01
        assert ((i, j) in edges) == conflict
        if conflict:
            assert edges[i, j] == pytest.approx(minimum)


def test_known_depths_are_not_moved_even_if_they_block_all_other_options():
    p, zones = joint_sample()
    first = zones[0]
    fixed = build_composite_zone(p.demand, first.demand_bbox, first.level_index, first.id,
        replace(first.placement, additions=(replace(first.placement.additions[0], axis_depth_from_face_mm=77),
                                            replace(first.placement.additions[1], axis_depth_from_face_mm=87.5))))
    result = solve(p, (fixed, *zones[1:]))
    assert result.points and result.points[0].zones[0] == fixed
    blocked = solve(p, (fixed, *zones[1:]), candidate_depths_by_component=((34,), (37.5,)))
    assert not blocked.points and blocked.telemetry["status"] == "no_candidate_depth_respects_cover_or_fixed_depth"


def test_cap_and_budget_failure_never_relax_depth_or_spacing():
    p, zones = joint_sample()
    one = solve(p, zones, candidate_depths_by_component=((34,), (37.5,)))
    assert not one.points and one.telemetry["status"] == "no_assignment_in_given_depths"
    exhausted = solve(p, zones, time_limit_s=1e-12)
    assert not exhausted.points and exhausted.telemetry["timed_out"]
    assert exhausted.telemetry["status"] == "search_budget_exhausted_not_infeasible"
    intrinsic = solve(replace(p, constraints=replace(p.constraints, minimum_clear_spacing_mm=100)), zones)
    assert not intrinsic.points and intrinsic.telemetry["status"] == "intrinsic_set_spacing_cannot_be_fixed_by_depth"


def test_independent_checker_rejects_a_forged_conflict_graph(monkeypatch):
    p, zones = joint_sample()
    graph = component_distance_graph(zones, minimum_clear_spacing_mm=25)
    monkeypatch.setattr("rebar.optimization.algorithms.composite_joint_depths.component_distance_graph",
                        lambda *args, **kwargs: {**graph, "edges": ()})
    result = solve(p, zones)
    assert not result.points and result.telemetry["status"] == "independent_revalidation_failed"


def test_side_cover_and_same_direction_overlap_are_not_fixed_by_depth():
    p, zones = joint_sample()
    overlap = (*zones, replace(zones[0], id="extra"))
    assert solve(p, overlap).telemetry["status"] == "non_depth_host_failure"
    host = replace(p.host_envelope, openings_mm=((2000, 300, 2400, 2200),))
    assert solve(replace(p, host_envelope=host), zones).telemetry["status"] == "non_depth_host_failure"


@pytest.mark.parametrize("kwargs", [
    {"candidate_depths_by_component": ()}, {"candidate_depths_by_component": ((34,),)},
    {"candidate_depths_by_component": ((True,), (65,))}, {"candidate_depths_by_component": ((34, 34), (65,))},
    {"candidate_depths_by_component": ((34,), (float("nan"),))}, {"hypothesis_source": ""},
    {"preserve_component_order": 1}, {"time_limit_s": 61}, {"maximum_search_nodes": True},
])
def test_depth_hypotheses_must_be_explicit_finite_and_bounded(kwargs):
    p, zones = joint_sample()
    with pytest.raises(ValueError):
        solve(p, zones, **kwargs)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_offset_grid_retains_original_boundary_atoms_and_target_area(axis):
    p = fragment_sample(axis)
    partition = partition_interior_demand(p)
    base = make_fragment_grid(p, partition, along_step_mm=1500, across_step_mm=900)
    grid = make_fragment_grid(p, partition, along_step_mm=1500, across_step_mm=900, across_origin_mm=300)
    assert grid["target_area_mm2"] == pytest.approx(base["target_area_mm2"])
    assert grid["target_cell_count"] == base["target_cell_count"]
    assert grid["across_coordinates_mm"][0] == base["across_coordinates_mm"][0]
    assert grid["across_coordinates_mm"][-1] == base["across_coordinates_mm"][-1]
    assert 300 in grid["across_coordinates_mm"]
    assert partition == partition_interior_demand(p)


@pytest.mark.parametrize("origin", [True, float("inf"), 1e12])
def test_offset_grid_rejects_invalid_origins(origin):
    p = fragment_sample()
    with pytest.raises(ValueError):
        make_fragment_grid(p, partition_interior_demand(p), along_step_mm=1500, across_step_mm=900, across_origin_mm=origin)
