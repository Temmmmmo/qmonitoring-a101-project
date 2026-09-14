"""Frozen owner service, not global self-certification, controls replacement."""
from dataclasses import replace

import pytest
from shapely.geometry import box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.opening_relocation import SourceServiceLane
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.collision_replacement import (
    CollisionReplacementLimits, CollisionReplacementOperation, check_collision_replacement,
    freeze_owner_fe_service, operation_bars,
)
from rebar.optimization.services.fe_host_repair import installed_fe_coverage
from rebar.optimization.services.opening_relocation import lane_map
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def case(axis=Axis.X):
    direction = Direction(Layer.TOP, axis)
    sources = tuple(PhysicalSourceBar(f"zone{i}/0/0", direction, "A500", 10, 100,
        (600, 6450), (1000, 2000), 10, 0) for i in range(2))
    before = tuple(PhysicalBar(f"bar{i}", direction, "A500", 10, 100, (600, 6450), (s.id,)) for i, s in enumerate(sources))
    lanes = tuple(SourceServiceLane(s, f"zone{i}", 0, 0, 300, (-50, 250), (150, 150)) for i, s in enumerate(sources))
    polygon = box(1000, 0, 2000, 200) if axis is Axis.X else box(0, 1000, 200, 2000)
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(300, 10),))
    levels = (DemandLevel(0, 1, 0, 1, "background", None, False, replace(recipe, additions=())),
        DemandLevel(1, 2, 1, 2, "addition", recipe.additions[0], True, recipe))
    problems = tuple(LayoutProblem(DemandMap(d, levels, (DemandCell(1, tuple(polygon.exterior.coords)[:-1],
        (polygon.centroid.x, polygon.centroid.y), 2 if d == direction else 1, int(d == direction)),), polygon.bounds)) for d in PLATE_DIRECTIONS)
    material = box(0, -1000, 15000, 5000) if axis is Axis.X else box(-1000, 0, 5000, 15000)
    host = OrthogonalSolidHost((SolidHostSection(0, 200, material),), 25, 25, 25, material.area*200, 6)
    op = CollisionReplacementOperation("split_sides", direction, ("bar0", "bar1"), ("bar0",),
        tuple(replace(before[0], id=side, transverse_axis_mm=q, installed_interval_mm=(600, 3525))
            for side, q in (("left", 90), ("right", 110))))
    return before, lanes, PlateProblem(direction_problems=problems, case_id="synthetic"), host, op


LIMITS = CollisionReplacementLimits(1000, 20, 20)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_two_tangent_descendants_preserve_every_owner_FE_and_full_stock(axis):
    before, lanes, problem, host, op = case(axis)
    after, checked = check_collision_replacement(before, (op,), lanes, problem, host, limits=LIMITS)
    assert len(after) == 3 and checked["same_direction_body_pairs_before"] == 1
    assert checked["same_direction_body_pairs_after"] == 0
    assert checked["source_coverage"]["status"] == checked["stock_cutting"]["status"] == "pass"
    assert checked["owner_proof"]["duplicated_owner_count"] == 1
    assert checked["owner_proof"]["positive_fragment_count"] == 2
    assert checked["owner_proof"]["every_positive_fragment_preserved"]
    assert not checked["input_transverse_axes_preserved"] and not checked["legacy_source_certificate_reused"]
    assert "original_STO_axes_preserved" not in checked
    assert not checked["placement_eligible"] and not checked["engineering_approval"]


def test_fusion_preserves_union_owners_but_does_not_sum_As():
    before, lanes, problem, host, _ = case()
    fused = replace(before[0], id="fused", installed_interval_mm=(600, 12300),
        source_bar_ids=tuple(b.source_bar_ids[0] for b in before))
    op = CollisionReplacementOperation("fuse", before[0].direction, ("bar0", "bar1"), ("bar0", "bar1"), (fused,))
    after, checked = check_collision_replacement(before, (op,), lanes, problem, host, limits=LIMITS)
    assert len(after) == 1 and checked["stock_cutting"]["status"] == "pass"
    assert checked["input_transverse_axes_preserved"] and not checked["owner_proof"]["weak_As_summed"]


def test_fusion_does_not_claim_original_STO_phases_after_upstream_axis_shift():
    before, lanes, problem, host, _ = case()
    direction = before[0].direction
    source = PhysicalSourceBar("shifted/0/0", direction, "A500", 10, 400,
        (600, 12300), (1000, 2000), 10, 0)
    # An unrelated bar has already been independently moved from the original
    # phase400 to425. A later fusion leaves that INPUT axis intact, not original.
    moved = PhysicalBar("upstream-shifted", direction, "A500", 10, 425, (600, 12300), (source.id,))
    before = (*before, moved)
    lanes = (*lanes, SourceServiceLane(source, "shifted", 0, 0, 300, (250, 550), (150, 150)))
    original = problem.problem(direction)
    polygon = box(1000, 300, 2000, 500)
    demand = replace(original.demand, cells=(*original.demand.cells,
        DemandCell(2, tuple(polygon.exterior.coords)[:-1], (1500, 400), 2, 1)))
    changed = replace(original, demand=demand)
    problem = replace(problem, direction_problems=tuple(changed if p is original else p for p in problem.direction_problems))
    fused = replace(before[0], id="fused", installed_interval_mm=(600, 12300),
        source_bar_ids=(before[0].source_bar_ids[0], before[1].source_bar_ids[0]))
    op = CollisionReplacementOperation("fuse", direction, ("bar0", "bar1"), ("bar0", "bar1"), (fused,))
    after, checked = check_collision_replacement(before, (op,), lanes, problem, host, limits=LIMITS)
    assert next(b for b in after if b.id == moved.id).transverse_axis_mm == 425 != source.transverse_axis_mm
    assert checked["input_transverse_axes_preserved"]
    assert "original_STO_axes_preserved" not in checked
    assert "original_STO_phase_certificate" in checked["not_checked"]
    assert not checked["legacy_source_certificate_reused"] and not checked["placement_eligible"]


@pytest.mark.parametrize("lost", (1, 1e-9))
def test_other_strong_owner_cannot_hide_lost_even_tiny_original_owner_piece(lost):
    before, lanes, problem, host, op = case()
    right = replace(op.added_bars[1], installed_interval_mm=(600+lost, 3525+lost))
    # Lose a thin strip at one outer transverse side, while stationary bar1
    # continues to cover the whole original FE. A global-only check would pass.
    op = replace(op, added_bars=(op.added_bars[0], right))
    after = operation_bars(before, (op,))
    assert installed_fe_coverage(problem, after, lane_map(lanes))["status"] == "pass"
    # Use a FE reaching the original service limit: the left descendant does
    # not reach its last 10 mm. No tiny positive fragment may disappear.
    original = next(p for p in problem.direction_problems if p.demand.direction == before[0].direction)
    poly = box(1000, 0, 2000, 250)
    changed = replace(original, demand=replace(original.demand, cells=(replace(original.demand.cells[0],
        poly=tuple(poly.exterior.coords)[:-1]),)))
    problem = replace(problem, direction_problems=tuple(changed if p is original else p for p in problem.direction_problems))
    assert installed_fe_coverage(problem, after, lane_map(lanes))["status"] == "pass"
    with pytest.raises(ValueError, match="Frozen original owner FE fragment"):
        check_collision_replacement(before, (op,), lanes, problem, host, limits=LIMITS)


@pytest.mark.parametrize("change", ("nan", "bool_axis", "float_diameter", "owner", "missing_descendant", "same_side", "diameter", "material", "id_reuse", "noncatalogue", "phase", "bad_kind"))
def test_tampered_operation_is_rejected(change):
    before, lanes, problem, host, op = case()
    first = op.added_bars[0]
    mutations = {"nan": {"transverse_axis_mm": float("nan")}, "bool_axis": {"transverse_axis_mm": True},
        "float_diameter": {"diameter_mm": 10.0}, "owner": {"source_bar_ids": ("missing",)},
        "same_side": {"transverse_axis_mm": 110}, "diameter": {"diameter_mm": 12},
        "material": {"steel_class": "other"}, "id_reuse": {"id": "bar0"},
        "noncatalogue": {"installed_interval_mm": (600, 3600)}, "phase": {"transverse_axis_mm": 89}}
    if change in mutations:
        op = replace(op, added_bars=(replace(first, **mutations[change]), op.added_bars[1]))
    elif change == "missing_descendant":
        op = replace(op, added_bars=(first,))
    else:
        op = replace(op, kind="erase")
    with pytest.raises(ValueError):
        check_collision_replacement(before, (op,), lanes, problem, host, limits=LIMITS)


def test_owner_must_be_once_only_before_and_original_inputs_unchanged():
    before, lanes, problem, _, _ = case()
    frozen = freeze_owner_fe_service(before, lanes, problem)
    assert frozen[(before[0].direction, "zone0/0/0")].required_interval_mm == (1000, 2000)
    with pytest.raises(ValueError, match="every source owner exactly once"):
        freeze_owner_fe_service((before[0],), lanes, problem)
    with pytest.raises(ValueError, match="every source owner exactly once"):
        freeze_owner_fe_service((*before, replace(before[0], id="duplicate-owner")), lanes, problem)
    assert before[0].transverse_axis_mm == 100


def test_fragment_budget_is_fail_closed_not_partial_proof():
    before, lanes, problem, _, _ = case()
    with pytest.raises(ValueError, match="budget exceeded"):
        freeze_owner_fe_service(before, lanes, problem, maximum_fragments=1)


@pytest.mark.parametrize("field,value", (("maximum_mass_kg", float("nan")), ("maximum_mass_kg", True),
    ("maximum_bar_count", True), ("maximum_bar_count", 2), ("maximum_position_count", 1), ("maximum_mass_kg", 0.1)))
def test_explicit_limits_are_not_relaxed(field, value):
    before, lanes, problem, host, op = case()
    with pytest.raises(ValueError):
        check_collision_replacement(before, (op,), lanes, problem, host, limits=replace(LIMITS, **{field: value}))


def test_duplicate_original_pair_cannot_be_replaced_twice():
    before, _, _, _, op = case()
    with pytest.raises(ValueError, match="disjoint"):
        operation_bars(before, (op, op))


def test_background_penetration_from_greater_fused_diameter_is_rejected():
    before, lanes, problem, host, _ = case()
    # Both original source bars tangent to background. Increasing to D12 would
    # penetrate it even though max-D geometry could otherwise cover all FE.
    first = replace(before[0], transverse_axis_mm=10)
    second = replace(before[1], transverse_axis_mm=10, diameter_mm=12)
    # Invalid before itself must fail, rather than bless the subsequent fusion.
    with pytest.raises(ValueError, match="Before source windows/material/background"):
        check_collision_replacement((first, second), (), lanes, problem, host, limits=LIMITS)


def test_unresolved_host_and_stock_are_explicit_never_placement_pass():
    before, lanes, problem, host, op = case()
    # Same FE coverage but retained excessive straight tail crosses the host.
    material = box(0, -1000, 6000, 5000)
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    op = replace(op, added_bars=tuple(replace(b, installed_interval_mm=(600, 12300)) for b in op.added_bars))
    _, checked = check_collision_replacement(before, (op,), lanes, problem, host, limits=LIMITS)
    assert checked["host_blocked_before"] == 2 and checked["host_blocked_after"] == 3
    assert checked["host_regression"] and checked["status"] == "blocked_host_regression"
    assert checked["stock_cutting"]["status"] == "fail" and not checked["placement_eligible"]
