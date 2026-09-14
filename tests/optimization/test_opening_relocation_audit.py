"""Independent finite-arrangement oracle and adversarial relocation checks.

The oracle builds its expected service polygons directly, without using the
solver's rectangle list, midpoint signatures or its MILP success flags.
"""

from dataclasses import replace
from itertools import product
import math
from random import Random

import pytest
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.algorithms.opening_relocation import _coverage_rows
from rebar.optimization.contracts.opening_relocation import OpeningRelocationConfig, SourceServiceLane
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.opening_relocation import check_relocation, coverage, lane_map
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def _problem(direction, points):
    polygon = Polygon(points)
    extra = Rebar(300, 10)
    level = DemandLevel(0, 1, 0.0, 1.0, "s300d10+s300d10", extra, True,
                        ReinforcementRecipe(Rebar(300, 10), (extra,)))
    cell = DemandCell(0, tuple(points), (polygon.centroid.x, polygon.centroid.y), 1, 0)
    return PlateProblem(tuple(LayoutProblem(DemandMap(
        d, (level,), (cell,) if d == direction else (), polygon.bounds,
    )) for d in PLATE_DIRECTIONS))


def _lane(direction, index, coordinate, *, start=600.0, end=1700.0,
          required=(1000.0, 1300.0), background_origin=0.0, window=(0.0, 1000.0)):
    zone = f"zone-{index}"
    source = PhysicalSourceBar(f"{zone}/0/0", direction, "A500", 10, coordinate,
                               (start, end), required, 10, background_origin)
    lane = SourceServiceLane(source, zone, 0, 0, 300, window, (150.0, 150.0))
    bar = PhysicalBar(f"physical-{index}", direction, "A500", 10, coordinate,
                      (start, end), (source.id,))
    return bar, lane


def _host(footprint=None):
    material = box(-100, -100, 3000, 3000) if footprint is None else footprint
    return OrthogonalSolidHost((SolidHostSection(0, 200, material),), 25, 25, 0,
                               material.area * 200, 6)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_exact_coverage_atoms_match_independent_polygon_union_on_540_joint_assignments(axis):
    rng = Random(1429)
    direction = Direction(Layer.TOP, axis)
    checked = 0
    for case in range(30):
        # Alternate a rectangle and a genuinely nonrectangular FE; every third
        # case includes fixed service coverage, exercising exact subtraction.
        points = ((1000, 250), (1300, 250), (1250, 700), (1000, 750)) if case % 2 else (
            (1000, 250), (1300, 250), (1300, 750), (1000, 750))
        if axis is Axis.Y:
            points = tuple((y, x) for x, y in points)
        polygon, problem = Polygon(points), _problem(direction, points)
        lanes, choices, groups = [], [], []
        for index, coordinate in enumerate((350.0, 650.0)):
            original, lane = _lane(direction, index, coordinate)
            lanes.append(lane)
            group = []
            for alternative in (coordinate, coordinate + rng.uniform(-90, 90),
                                coordinate + rng.uniform(-90, 90)):
                group.append(len(choices))
                choices.append(replace(original, transverse_axis_mm=alternative))
            groups.append(group)
        fixed = ()
        if case % 3 == 0:
            fixed_bar, fixed_lane = _lane(direction, 2, 500.0)
            fixed = (fixed_bar,)
            lanes.append(fixed_lane)
        sources = lane_map(tuple(lanes))
        rows, _ = _coverage_rows(problem, fixed, choices, sources, OpeningRelocationConfig())
        assert rows is not None  # Original choices are a complete incumbent.
        for assignment in product(*groups):
            selected = (*fixed, *(choices[i] for i in assignment))
            rectangles = []
            for bar in selected:
                low = max(0, bar.transverse_axis_mm - 150)
                high = min(1000, bar.transverse_axis_mm + 150)
                rectangles.append(box(1000, low, 1300, high) if axis is Axis.X
                                  else box(low, 1000, high, 1300))
            missing = polygon.difference(unary_union(rectangles)).area
            geometric_pass = missing <= max(1e-6, polygon.area * 1e-9)
            matrix_pass = all(set(assignment) & row for row in rows)
            validator_pass = coverage(problem, selected, sources)["status"] == "pass"
            assert matrix_pass == geometric_pass == validator_pass, (axis, case, assignment)
            checked += 1
    assert checked == 270  # Both parametrized axes together cover 540 assignments.


def test_non300_background_cannot_be_checked_against_fictitious_300_grid():
    direction = Direction(Layer.TOP, Axis.X)
    _, lane = _lane(direction, 0, 100.0)
    lane = replace(lane, source=replace(lane.source, background_step_mm=200.0))
    # A proposed move 100 -> 200 would hit the actual @200 background axis,
    # despite being clear of the fictitious @300 grid.
    with pytest.raises(ValueError, match="service window"):
        lane_map((lane,))


def test_moving_into_an_old_collision_pair_is_not_grandfathered_by_same_ids():
    direction = Direction(Layer.TOP, Axis.X)
    first, lane1 = _lane(direction, 0, 100.0)
    second, lane2 = _lane(direction, 1, 108.0)
    problem = _problem(direction, ((1000, 90), (1300, 90), (1300, 110), (1000, 110)))
    material = box(-100, -100, 3000, 3000).difference(box(1000, 94, 1100, 101))
    after = (replace(first, transverse_axis_mm=109.0), second)
    # The small hole is bypassed and FE coverage retained, but overlap with the
    # same second bar becomes worse. Pair identity alone must not excuse it.
    with pytest.raises(ValueError, match="body intersections"):
        check_relocation((first, second), after, (lane1, lane2), problem, _host(material))


@pytest.mark.parametrize("field,value", (
    ("transverse_axis_mm", math.nan),
    ("transverse_axis_mm", math.inf),
    ("installed_interval_mm", (math.nan, 1951.0)),
    ("installed_interval_mm", (True, 1951.0)),
))
def test_corrected_numeric_values_are_checked_before_equality_or_geometry(field, value):
    direction = Direction(Layer.TOP, Axis.X)
    bar, lane = _lane(direction, 0, 100.0, start=1.0, end=1951.0)
    problem = _problem(direction, ((1000, 90), (1300, 90), (1300, 110), (1000, 110)))
    with pytest.raises(ValueError, match="finite numbers"):
        check_relocation((bar,), (replace(bar, **{field: value}),), (lane,), problem, _host())


def test_boolean_axis_equal_to_original_one_is_still_not_a_valid_number():
    direction = Direction(Layer.TOP, Axis.X)
    bar, lane = _lane(direction, 0, 1.0, background_origin=100.0, window=(-200.0, 300.0))
    problem = _problem(direction, ((1000, 0), (1300, 0), (1300, 10), (1000, 10)))
    assert replace(bar, transverse_axis_mm=True) == bar  # Python equality trap.
    with pytest.raises(ValueError, match="finite numbers"):
        check_relocation((bar,), (replace(bar, transverse_axis_mm=True),), (lane,), problem, _host())


@pytest.mark.parametrize("widths", ((150.0, 149.0), (150.0, True), (150.0, math.nan)))
def test_nominal300_cannot_use_forged_original_service_half_widths(widths):
    direction = Direction(Layer.TOP, Axis.X)
    _, lane = _lane(direction, 0, 100.0)
    with pytest.raises(ValueError, match="service"):
        lane_map((replace(lane, service_half_widths_mm=widths),))


def test_consistent_noninteger_phase_tolerates_float_remainders_within_same_zone():
    direction = Direction(Layer.TOP, Axis.X)
    _, lane = _lane(direction, 0, 350.1, window=(0.0, 1500.0))
    other = replace(lane, bar_index=1, source=replace(
        lane.source, id="zone-0/0/1", transverse_axis_mm=1250.1,
    ))
    # Three exact nominal periods, but modulo floats need not compare equal.
    assert len(lane_map((lane, other))) == 2


@pytest.mark.parametrize("unknown_level", (-1, 99, True))
def test_unknown_fe_level_never_silently_becomes_background(unknown_level):
    direction = Direction(Layer.TOP, Axis.X)
    bar, lane = _lane(direction, 0, 100.0)
    problem = _problem(direction, ((1000, 90), (1300, 90), (1300, 110), (1000, 110)))
    one = problem.problem(direction)
    one = replace(one, demand=replace(one.demand, cells=(
        replace(one.demand.cells[0], level_index=unknown_level),
    )))
    problem = replace(problem, direction_problems=tuple(
        one if p.demand.direction == direction else p for p in problem.direction_problems
    ))
    with pytest.raises(ValueError):
        check_relocation((bar,), (bar,), (lane,), problem, _host())


@pytest.mark.parametrize("points", (
    (),
    ((1000.0, 90.0), (1300.0, 90.0)),
    ((1000.0, 90.0), (1100.0, 90.0), (1300.0, 90.0)),
    ((1000.0, 90.0), (1300.0, 110.0), (1300.0, 90.0), (1000.0, 110.0)),
    ((math.nan, 90.0), (1300.0, 90.0), (1300.0, 110.0)),
    ((math.inf, 90.0), (1300.0, 90.0), (1300.0, 110.0)),
    ((True, 90.0), (1300.0, 90.0), (1300.0, 110.0)),
    ((1000.0, 90.0, 0.0), (1300.0, 90.0), (1300.0, 110.0)),
))
@pytest.mark.parametrize("background_only", (False, True))
def test_invalid_original_fe_geometry_is_rejected_even_for_background(points, background_only):
    direction = Direction(Layer.TOP, Axis.X)
    bar, lane = _lane(direction, 0, 100.0)
    problem = _problem(direction, ((1000, 90), (1300, 90), (1300, 110), (1000, 110)))
    one = problem.problem(direction)
    levels = one.demand.levels
    if background_only:
        levels = (replace(levels[0], additional=None, requires_extra=False,
                          recipe=ReinforcementRecipe(Rebar(300, 10))),)
    one = replace(one, demand=replace(one.demand, levels=levels, cells=(
        replace(one.demand.cells[0], poly=points),
    )))
    problem = replace(problem, direction_problems=tuple(
        one if p.demand.direction == direction else p for p in problem.direction_problems
    ))
    with pytest.raises(ValueError, match="original FE polygon"):
        check_relocation((bar,), (bar,), (lane,), problem, _host())


@pytest.mark.parametrize("background_only", (False, True))
def test_duplicate_fe_identity_is_rejected_even_when_entire_polygon_is_covered(background_only):
    direction = Direction(Layer.TOP, Axis.X)
    bar, lane = _lane(direction, 0, 100.0)
    problem = _problem(direction, ((1000, 90), (1300, 90), (1300, 110), (1000, 110)))
    one = problem.problem(direction)
    levels = one.demand.levels
    if background_only:
        levels = (replace(levels[0], additional=None, requires_extra=False,
                          recipe=ReinforcementRecipe(Rebar(300, 10))),)
    one = replace(one, demand=replace(one.demand, levels=levels, cells=one.demand.cells * 2))
    problem = replace(problem, direction_problems=tuple(
        one if p.demand.direction == direction else p for p in problem.direction_problems
    ))
    with pytest.raises(ValueError, match="Unique integer original FE identities"):
        check_relocation((bar,), (bar,), (lane,), problem, _host())


def test_corrected_float_diameter_equal_to_integer_is_not_accepted_by_dataclass_equality():
    direction = Direction(Layer.TOP, Axis.X)
    bar, lane = _lane(direction, 0, 100.0)
    problem = _problem(direction, ((1000, 90), (1300, 90), (1300, 110), (1000, 110)))
    corrected = replace(bar, diameter_mm=10.0)
    assert corrected == bar
    with pytest.raises(ValueError, match="diameter must remain an integer"):
        check_relocation((bar,), (corrected,), (lane,), problem, _host())
