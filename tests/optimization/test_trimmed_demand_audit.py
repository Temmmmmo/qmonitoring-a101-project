"""Diagnostic alternative credit never mutates original demand or physical bars."""
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from shapely.geometry import box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.opening_relocation import SourceServiceLane
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.opening_relocation import lane_map
from rebar.optimization.services.shaped_geometry import straight_bar_from_physical
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection
from rebar.optimization.services.tz_boundary_trim import geometry_presence_offers

_spec = importlib.util.spec_from_file_location("audit_trimmed_demand", Path(__file__).resolve().parents[2]/"scripts/audit_trimmed_demand.py")
audit = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = audit
_spec.loader.exec_module(audit)

_bound_spec = importlib.util.spec_from_file_location("audit_trimmed_reachability", Path(__file__).resolve().parents[2]/"scripts/audit_trimmed_reachability.py")
bound = importlib.util.module_from_spec(_bound_spec)
sys.modules[_bound_spec.name] = bound
_bound_spec.loader.exec_module(bound)

_extension_spec = importlib.util.spec_from_file_location("experiment_trimmed_length_extension", Path(__file__).resolve().parents[2]/"scripts/experiment_trimmed_length_extension.py")
extension = importlib.util.module_from_spec(_extension_spec)
sys.modules[_extension_spec.name] = extension
_extension_spec.loader.exec_module(extension)


def fixture(axis=Axis.X):
    direction = Direction(Layer.TOP, axis)
    source = PhysicalSourceBar("zone/0/0", direction, "A500", 10, 100, (0, 1000), (200, 800), 10, 0)
    lane = SourceServiceLane(source, "zone", 0, 0, 300, (50, 100), (150, 150))
    bar = straight_bar_from_physical(PhysicalBar("bar", direction, "A500", 16, 100,
        (100, 900), (source.id,)), axis_z_mm=100, placement_profile_id="synthetic")
    return direction, (bar,), lane_map((lane,))


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_exact_default_diagnostic_offers_equal_shared_service_boxes(axis):
    _, bars, sources = fixture(axis)
    baseline = geometry_presence_offers(bars, sources)
    diagnostic = audit.relaxed_presence_offers(bars, sources)
    for direction in baseline:
        for old, new in zip(baseline[direction], diagnostic[direction], strict=True):
            assert old[:2] == new[:2]
            assert old[2].equals(new[2])


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_each_diagnostic_relaxation_is_separate_and_bar_geometry_unchanged(axis):
    direction, bars, sources = fixture(axis)
    old = bars
    limited = audit.relaxed_presence_offers(bars, sources)[direction][0]
    widened = audit.relaxed_presence_offers(bars, sources, release_windows=True)[direction][0]
    stronger = audit.relaxed_presence_offers(bars, sources, credit_actual_diameter=True)[direction][0]
    assert limited[:2] == widened[:2] == (10, 300)
    assert stronger[:2] == (16, 300)
    assert stronger[2].equals(limited[2])
    assert widened[2].covers(limited[2]) and widened[2].area > limited[2].area
    assert bars == old


def problem_with_cells(polygons, *, diameter=10, step=300):
    direction = Direction(Layer.TOP, Axis.X)
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(step, diameter),))
    levels = (DemandLevel(0, 1, 0, 2, "background", None, False, replace(recipe, additions=())),
        DemandLevel(1, 2, 2, 8, "additional", recipe.additions[0], True, recipe))
    cells = tuple(DemandCell(i, tuple(p.exterior.coords)[:-1], (p.centroid.x, p.centroid.y), 2, 1)
        for i, p in enumerate(polygons))
    p = LayoutProblem(DemandMap(direction, levels, cells, (-500, -500, 2000, 2000)))
    return SimpleNamespace(direction_problems=(p,))


def test_missing_area_categories_do_not_crop_or_discard_original_hole_FE():
    polygons = (box(100, 100, 200, 200), box(-50, 100, 50, 200), box(300, 300, 400, 400))
    problem = problem_with_cells(polygons)
    footprint = box(0, 0, 1000, 1000).difference(box(300, 300, 400, 400))
    host = OrthogonalSolidHost((SolidHostSection(0, 200, footprint),), 25, 25, 25, footprint.area*200, 10)
    before = tuple(c.poly for c in problem.direction_problems[0].demand.cells)
    result = audit.audit_missing(problem, {}, host)
    assert result["uncovered_direction_FE_count"] == 3
    assert result["categories_above_0_001mm2"] == {
        "inside_material": 1, "inside_material+outside_outer": 1, "over_opening": 1}
    assert result["areas_mm2"]["uncovered_area_mm2"] == 30000
    assert result["areas_mm2"]["inside_material_area_mm2"] == 15000
    assert tuple(c.poly for c in problem.direction_problems[0].demand.cells) == before


def test_diagnostic_never_sums_weak_recipe_offers():
    polygon = box(100, 100, 200, 200)
    problem = problem_with_cells((polygon,), diameter=16)
    footprint = box(0, 0, 1000, 1000)
    host = OrthogonalSolidHost((SolidHostSection(0, 200, footprint),), 25, 25, 25, footprint.area*200, 10)
    direction = problem.direction_problems[0].demand.direction
    result = audit.audit_missing(problem, {direction: [(10, 300, polygon), (12, 300, polygon)]}, host)
    assert result["uncovered_direction_FE_count"] == 1
    assert result["areas_mm2"]["uncovered_area_mm2"] == polygon.area


def test_microscopic_original_uncovered_piece_is_still_reported():
    polygon = box(100, 100, 100.000001, 100.000001)
    problem = problem_with_cells((polygon,))
    footprint = box(0, 0, 1000, 1000)
    host = OrthogonalSolidHost((SolidHostSection(0, 200, footprint),), 25, 25, 25, footprint.area*200, 10)
    result = audit.audit_missing(problem, {}, host)
    assert result["uncovered_direction_FE_count"] == 1
    assert result["categories_above_0_001mm2"] == {"only_below_0.001mm2": 1}
    assert result["areas_mm2"]["uncovered_area_mm2"] > 0


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_any_axis_bound_keeps_degenerate_transverse_centerline(axis):
    material = box(0, 0, 1000, 10) if axis is Axis.X else box(0, 0, 10, 1000)
    actual = bound.any_axis_presence_domain(material, axis, 10, 55)
    expected = box(0, -50, 1000, 60) if axis is Axis.X else box(-50, 0, 60, 1000)
    assert actual.equals(expected)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_any_axis_bound_has_no_longitudinal_extension(axis):
    material = box(0, 0, 1000, 1000)
    actual = bound.any_axis_presence_domain(material, axis, 10, 150)
    outside = box(-10, 100, -1, 200) if axis is Axis.X else box(100, -10, 200, -1)
    assert actual.intersection(outside).area == 0
    assert actual.covers(material)


@pytest.mark.parametrize("hole_width, expected_missing", ((480, 19000), (120, 0)))
def test_any_axis_bound_distinguishes_transverse_hole_width(hole_width, expected_missing):
    hole = box(400, 500-hole_width/2, 500, 500+hole_width/2)
    material = box(0, 0, 1000, 1000).difference(hole)
    actual = bound.any_axis_presence_domain(material, Axis.X, 10, 150)
    assert hole.difference(actual).area == expected_missing
    # Serving a narrow hole in the presence surrogate is not steel inside it.
    assert material.intersection(hole).area == 0


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_catalogue_extension_preserves_identity_and_transverse_Z(axis):
    _, bars, _ = fixture(axis)
    old = bars[0]
    new = extension.segment(old, 0, 1170)
    assert new.id == old.id and new.source_bar_ids == old.source_bar_ids
    assert new.diameter_mm == old.diameter_mm and new.direction == old.direction
    across = 1 if axis is Axis.X else 0
    assert new.segments[0].start_mm[across] == old.segments[0].start_mm[across]
    assert new.segments[0].start_mm[2] == old.segments[0].start_mm[2]
    assert new.selected_cut_length_mm == 1170


def test_extension_rejects_even_preexisting_collision_involving_modified_bar():
    _, bars, _ = fixture()
    old = bars[0]
    colliding = replace(old, id="other")
    assert not extension.separate_from_every_other(extension.segment(old, 0, 1170), (old, colliding))
    distant = extension.segment(colliding, 1170, 2000)
    assert extension.separate_from_every_other(extension.segment(old, 0, 1170), (old, distant))
