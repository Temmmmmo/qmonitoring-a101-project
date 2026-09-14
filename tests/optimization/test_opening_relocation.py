"""Joint geometric bypasses retain every source FE, physical bar and full 40d."""
from copy import deepcopy
from dataclasses import replace

import pytest
from shapely.affinity import affine_transform
from shapely.geometry import box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.algorithms.opening_relocation import relocate_small_openings
from rebar.optimization.contracts.opening_relocation import (
    OpeningRelocationConfig, OpeningRelocationLimitError, SourceServiceLane,
)
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.opening_relocation import (
    check_relocation, eligible_holes, material_and_holes,
)
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def case(axis=Axis.X, *, hole=None, demand_bounds=(1000, 80, 1500, 170), second_axis=None):
    direction = Direction(Layer.TOP, axis)
    footprint = box(-1000, -1000, 6000, 6000).difference(
        hole if hole is not None else box(1200, 80, 1300, 120))
    poly = box(*demand_bounds)
    if axis is Axis.Y:
        footprint = affine_transform(footprint, (0, 1, 1, 0, 0, 0))
        poly = affine_transform(poly, (0, 1, 1, 0, 0, 0))
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(300, 10),))
    levels = (DemandLevel(0, 1, 0, 1, "s300d10", None, False, replace(recipe, additions=())),
        DemandLevel(1, 2, 1, 2, "s300d10+s300d10", recipe.additions[0], True, recipe))
    problems = []
    for current in PLATE_DIRECTIONS:
        cell = DemandCell(1, tuple(poly.exterior.coords)[:-1], (poly.centroid.x, poly.centroid.y),
                          2 if current == direction else 1, int(current == direction))
        problems.append(LayoutProblem(DemandMap(current, levels, (cell,), poly.bounds)))
    sources, bars, lanes = [], [], []
    for index, coordinate in enumerate((100,) if second_axis is None else (100, second_axis)):
        zone = f"zone-{index}"
        source = PhysicalSourceBar(f"{zone}/0/0", direction, "A500", 10, coordinate,
            (600, 2550), (1000, 1500), 10, 0)
        sources.append(source)
        bars.append(PhysicalBar(f"bar-{index}", direction, "A500", 10, coordinate,
                                (600, 2550), (source.id,)))
        lanes.append(SourceServiceLane(source, zone, 0, 0, 300, (-100, 500), (150, 150)))
    host = OrthogonalSolidHost((SolidHostSection(0, 200, footprint),), 25, 25, 25,
                              footprint.area * 200, 10)
    return tuple(bars), tuple(lanes), PlateProblem(tuple(problems)), host


def run(data, **config):
    return relocate_small_openings(*data, config=OpeningRelocationConfig(**config))


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_contained_bypass_preserves_full_source_inventory_coverage_and_mass(axis):
    data = case(axis)
    old = deepcopy(data)
    result = run(data)
    assert data == old
    assert result.review["moved_bar_count"] == 1
    assert result.review["host_blocked_before"] == 1
    assert result.review["host_blocked_after"] == 0
    assert result.review["source_coverage_after"]["uncovered_cell_count"] == 0
    assert result.review["mass_delta_kg"] == 0
    assert result.review["new_same_direction_body_pairs"] == 0
    assert result.review["full_new_diameter_40d_status"] == "pass"
    assert result.bars[0].installed_length_mm == 1950
    assert result.bars[0].source_bar_ids == data[0][0].source_bar_ids
    assert result.bars[0].diameter_mm == data[0][0].diameter_mm
    assert not result.placement_eligible and not result.review["engineering_approval"]
    assert check_relocation(data[0], result.bars, *data[1:])["moved_bar_count"] == 1


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_geometry_room_does_not_allow_losing_source_coverage(axis):
    data = case(axis, demand_bounds=(1000, -50, 1500, 250))
    result = run(data)
    assert result.bars == data[0]
    assert result.review["host_blocked_after"] == 1
    assert result.review["source_coverage_after"]["status"] == "pass"
    with pytest.raises(ValueError, match="lost original FE"):
        check_relocation(data[0], (replace(data[0][0], transverse_axis_mm=150),), *data[1:])


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_fixed_neighbour_keeps_body_collision_checks_in_joint_search(axis):
    data = case(axis, second_axis=170)
    result = run(data)
    assert result.review["host_blocked_after"] == 0
    assert result.review["new_same_direction_body_pairs"] == 0
    assert result.bars[1] == data[0][1]
    assert abs(result.bars[0].transverse_axis_mm-result.bars[1].transverse_axis_mm) >= 10-1e-6


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
@pytest.mark.parametrize(("hole", "reason"), (
    (box(1200, -1000, 1300, 120), "outer_boundary"),
    (box(1200, -50, 1300, 250), "opening_width_not_below_300"),
    (box(1200, 50, 1250, 150).union(box(1250, 80, 1300, 120)), "nonrectangular_hole"),
))
def test_notch_large_or_nonrectangular_hole_never_gets_small_hole_permission(axis, hole, reason):
    data = case(axis, hole=hole)
    result = run(data)
    assert result.bars == data[0]
    assert result.review["moved_bar_count"] == 0
    assert result.review["search"]["classification"][reason] == 1


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_one_small_hole_does_not_excuse_a_second_large_hole(axis):
    data = case(axis, hole=box(1200, 80, 1300, 120).union(box(1800, -50, 1900, 250)))
    material, outer, holes = material_and_holes(data[-1])
    assert eligible_holes(data[0][0], material, outer, holes, 25)[1] == "opening_width_not_below_300"
    assert run(data).review["moved_bar_count"] == 0


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_unchanged_body_containment_does_not_shift_a_healthy_bar(axis):
    data = case(axis, hole=box(3500, 80, 3600, 120))
    result = run(data)
    assert result.bars == data[0]
    assert result.review["host_blocked_after"] == 0
    assert result.review["search"]["classification"] == {"already_contained": 1}


@pytest.mark.parametrize("change", ("diameter", "steel", "length", "owners", "missing", "short40d", "background"))
def test_independent_checker_rejects_inventory_and_engineering_changes(change):
    data = case()
    bar = run(data).bars[0]
    values = {"diameter": {"diameter_mm": 12}, "steel": {"steel_class": "A400"},
        "length": {"installed_interval_mm": (600, 2500)}, "owners": {"source_bar_ids": ()},
        "short40d": {"installed_interval_mm": (601, 2551)}, "background": {"transverse_axis_mm": 300}}
    after = () if change == "missing" else (replace(bar, **values[change]),)
    with pytest.raises(ValueError):
        check_relocation(data[0], after, *data[1:])


@pytest.mark.parametrize("config", ({"maximum_shift_mm": 301}, {"maximum_shift_mm": float("nan")},
    {"time_limit_s": 0}, {"maximum_candidates_per_bar": True}, {"maximum_total_candidates": 0}))
def test_invalid_configuration_fails_before_search(config):
    with pytest.raises(ValueError):
        run(case(), **config)


def test_complete_candidate_budget_is_not_silently_truncated():
    with pytest.raises(OpeningRelocationLimitError, match="candidate budget"):
        run(case(), maximum_total_candidates=1)


def test_small_search_bound_retains_complete_incumbent_not_partial_result():
    data = case()
    result = run(data, maximum_shift_mm=1)
    assert result.bars == data[0]
    assert result.review["moved_bar_count"] == 0
    assert result.review["physical_bar_count"] == 1
    assert result.review["host_blocked_after"] == 1
