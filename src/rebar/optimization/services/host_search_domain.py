"""Necessary host-domain bound, not a structural design or a clipped demand map.

Within the explicitly stated straight-bar/full40d model, each demand point must
have some serving axis whose local rectangle (40d + side cover along the bar,
radius + side cover across it) lies inside the exact orthogonal host material.
We relax common phases, finite stock, bar count and joint collisions. Therefore a
missing point rules out this model; a covered point proves no installable layout.

All Z sections are intersected: results concern that conservative common-section
model, not arbitrary layer heights in a varying solid. Orthogonal event cells,
including isolated lines and points, preserve closed-contact degeneracies before
the optimistic transverse service expansion. There is no raster/grid sampling.
"""
from __future__ import annotations

from collections.abc import Callable
import math

import numpy as np
import shapely
from shapely.geometry import GeometryCollection, LineString, Point, Polygon, box, mapping
from shapely.ops import unary_union

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe

from ..contracts.problem import LayoutProblem
from ..contracts.result import LayoutZone
from .axis_patterns import a101_sto_279_slab_recipe_placement
from .composite_coverage import monotone_single_recipe_covers
from .physical_host_fit import _polygons, _validate_host
from .solid_host import AREA_TOLERANCE_MM2, OrthogonalSolidHost

POLICY = "optimistic-any-axis-straight-full40d-common-section/v1"
NOT_CHECKED = (
    "joint_axis_phases_and_finite_service_windows", "minimum_zone_width_and_bar_count",
    "stock_cutting_and_mass", "same_and_cross_direction_body_collisions",
    "actual_Z_and_existing_revit_reinforcement", "Revit_readback", "engineering_acceptance",
)
TOL = 1e-6


class HostSearchDomainLimitError(ValueError):
    """An explicit resource bound was exceeded; never interpreted as impossible."""


def _finite(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and abs(value) <= 1e9)


def _limit(value, label):
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive integer")


def _common_material(host):
    _validate_host(host, 20000)
    material = host.sections[0].footprint
    for section in host.sections[1:]:
        material = material.intersection(section.footprint)
    return material


def _dimension_piece(xlo, ylo, xhi, yhi):
    if xlo == xhi and ylo == yhi:
        return Point(xlo, ylo)
    if xlo == xhi or ylo == yhi:
        return LineString(((xlo, ylo), (xhi, yhi)))
    return box(xlo, ylo, xhi, yhi)


def _event_cells(values):
    """Closed strata: vertex, interval, vertex; membership is constant inside each."""
    strata = []
    for index, value in enumerate(values):
        if index:
            strata.append((values[index-1], value))
        strata.append((value, value))
    return strata


def _orthogonal_domain(material, radius_x, radius_y, service_x, service_y, maximum_parts):
    """Exact closed rectangular erosion and one-axis Minkowski expansion.

    Events are every host vertex shifted by both kernel supports. A translated
    rectangular kernel cannot change containment between these events. Test one
    representative per stratum, including 1D/0D strata; this is exact for the
    validated orthogonal polygon, not a tunable mesh resolution.
    """
    if material.is_empty or material.area == 0:
        return GeometryCollection(), GeometryCollection(), 0
    xmin, ymin, xmax, ymax = material.bounds
    lo_x, hi_x = xmin+radius_x, xmax-radius_x
    lo_y, hi_y = ymin+radius_y, ymax-radius_y
    # Closed contact at decimal coordinates can produce lo > hi by one ULP.
    # Preserve that potential line instead of falsely declaring an empty domain.
    if 0 < lo_x-hi_x <= TOL:
        lo_x = hi_x = (lo_x+hi_x)/2
    if 0 < lo_y-hi_y <= TOL:
        lo_y = hi_y = (lo_y+hi_y)/2
    if lo_x > hi_x or lo_y > hi_y:
        return GeometryCollection(), GeometryCollection(), 0
    points = [point for polygon in _polygons(material)
              for ring in (polygon.exterior, *polygon.interiors) for point in ring.coords]
    xs = sorted({lo_x, hi_x, *(x+delta for x, _ in points for delta in (-radius_x, radius_x)
                              if lo_x <= x+delta <= hi_x)})
    ys = sorted({lo_y, hi_y, *(y+delta for _, y in points for delta in (-radius_y, radius_y)
                              if lo_y <= y+delta <= hi_y)})
    xcells, ycells = _event_cells(xs), _event_cells(ys)
    count = len(xcells)*len(ycells)
    if count > maximum_parts:
        raise HostSearchDomainLimitError(f"Orthogonal event strata {count} exceed limit {maximum_parts}; no truncation")
    # Vectorized exact polygon containment, not centre-in-host tests: every probe
    # is the ENTIRE anchorage/radius rectangle around the stratum representative.
    xx = np.repeat(np.array([(a+b)/2 for a, b in xcells]), len(ycells))
    yy = np.tile(np.array([(a+b)/2 for a, b in ycells]), len(xcells))
    probes = shapely.box(xx-radius_x, yy-radius_y, xx+radius_x, yy+radius_y)
    shapely.prepare(material)
    valid = shapely.covers(material, probes)
    # GEOS boundary predicates can reject a tangent rectangle after q +/- radius
    # roundoff. Use the same explicit area tolerance as the physical host checker.
    # This only makes the necessary domain optimistic, never falsely impossible.
    rejected = np.flatnonzero(~valid)
    valid[rejected] = shapely.area(shapely.difference(probes[rejected], material)) <= AREA_TOLERANCE_MM2
    axis_pieces, service_pieces = [], []
    for index in np.flatnonzero(valid):
        xlo, xhi = xcells[index//len(ycells)]
        ylo, yhi = ycells[index % len(ycells)]
        axis_pieces.append(_dimension_piece(xlo, ylo, xhi, yhi))
        service_pieces.append(_dimension_piece(xlo-service_x, ylo-service_y,
                                               xhi+service_x, yhi+service_y))
    return unary_union(axis_pieces), unary_union(service_pieces), count


def recipe_service_half_width_mm(recipe: ReinforcementRecipe) -> float:
    """Optimistic maximum half-gap of the actual common A101/STO axis factory.

    @300 -> 150; conditional @150 (100/200) ->100;
    @100 contact -> (100 + (d_background+d_addition)/2)/2, NOT 50.
    Symmetric use of this maximum deliberately relaxes each axis' asymmetry.
    """
    if (not isinstance(recipe, ReinforcementRecipe) or len(recipe.additions) != 1
            or recipe.background.step != 300):
        raise ValueError("One explicit addition and background @300 required")
    placement = a101_sto_279_slab_recipe_placement(recipe, background_origin_mm=0, contact_side="left")
    pattern = placement.additions[0].pattern
    offsets = pattern.offsets_mm
    gaps = [b-a for a, b in zip(offsets, (*offsets[1:], offsets[0]+pattern.period_mm))]
    return max(gaps)/2


def _validate_problem(problem, maximum_cells):
    if not isinstance(problem, LayoutProblem) or not isinstance(problem.demand.direction, Direction):
        raise ValueError("Typed LayoutProblem and direction required")
    if (not isinstance(problem.demand.direction.axis, Axis) or not isinstance(problem.demand.direction.layer, Layer)
            or not _finite(problem.constraints.anchorage_diameters)
            or problem.constraints.anchorage_diameters != 40):
        raise ValueError("This explicit bound requires typed axes and full40d constraints")
    demand = problem.demand
    if not demand.levels or len(demand.cells) > maximum_cells:
        raise HostSearchDomainLimitError("Missing scale or original FE budget exceeded")
    for index, level in enumerate(demand.levels):
        if type(level.index) is not int or level.index != index or level.recipe is None:
            raise ValueError("Explicit contiguous recipe scale required")
        recipe = level.recipe
        if recipe.background.step != 300 or len(recipe.additions) > 1:
            raise ValueError("Single-addition @300-background scale required")
        if recipe.additions:
            spec = recipe.additions[0]
            if not 6 <= spec.diameter <= 40 or spec.step not in (100, 150, 300):
                raise ValueError("Unsupported explicit diameter/nominal-step recipe")
            recipe_service_half_width_mm(recipe)
    if len({level.recipe.background for level in demand.levels}) != 1:
        raise ValueError("Common explicit background required")
    seen = set()
    for cell in demand.cells:
        if (type(cell.id) is not int or cell.id in seen or type(cell.level_index) is not int
                or not 0 <= cell.level_index < len(demand.levels)):
            raise ValueError("Unique real FE identifiers and known integer levels required")
        seen.add(cell.id)
        if (not isinstance(cell.poly, tuple) or len(cell.poly) < 3
                or any(not isinstance(point, (tuple, list)) or len(point) != 2
                       or any(not _finite(v) for v in point) for point in cell.poly)):
            raise ValueError("Finite original FE polygon required")
        poly = Polygon(cell.poly)
        if not poly.is_valid or poly.area <= 0:
            raise ValueError("Valid positive-area original FE polygon required")


def optimistic_host_reachability(
    problem: LayoutProblem, host: OrthogonalSolidHost, *, maximum_cells: int = 10000,
    maximum_geometry_parts: int = 250000,
) -> dict:
    """Necessary reachability bound over ALL axes and sufficient listed recipes.

    Never clips or rewrites problem.demand. The recipe catalogue is exactly the
    explicit original levels, with no thinner/no sparser monotone substitutions.
    Misses rule out ONLY this straight/full40d/common-section/service-width model.
    """
    _limit(maximum_cells, "maximum_cells")
    _limit(maximum_geometry_parts, "maximum_geometry_parts")
    _validate_problem(problem, maximum_cells)
    material = _common_material(host)
    axis = problem.demand.direction.axis
    recipes = tuple(dict.fromkeys(level.recipe for level in problem.demand.levels if level.recipe.additions))
    domains, recipe_rows = {}, []
    for recipe in recipes:
        spec = recipe.additions[0]
        along, across = 40*spec.diameter+host.side_cover_mm, spec.diameter/2+host.side_cover_mm
        half = recipe_service_half_width_mm(recipe)
        rx, ry = (along, across) if axis is Axis.X else (across, along)
        sx, sy = (0, half) if axis is Axis.X else (half, 0)
        axes, serving, count = _orthogonal_domain(material, rx, ry, sx, sy, maximum_geometry_parts)
        domains[recipe] = serving
        recipe_rows.append({"background": {"diameter_mm": recipe.background.diameter, "step_mm": 300},
            "additional": {"diameter_mm": spec.diameter, "nominal_step_mm": spec.step},
            "required_along_each_side_mm": along, "required_transverse_each_side_mm": across,
            "optimistic_service_half_width_mm": half, "event_strata_checked": count,
            "axis_domain": mapping(axes), "axis_domain_area_mm2": axes.area,
            "serving_domain": mapping(serving), "serving_domain_area_mm2": serving.area})
    sufficient = {}
    for level in problem.demand.levels:
        if level.recipe.additions:
            supplied = [recipe for recipe in recipes if monotone_single_recipe_covers(level.recipe, recipe)]
            sufficient[level.index] = (unary_union([domains[recipe] for recipe in supplied]),
                                       [recipes.index(recipe) for recipe in supplied])
    cells, missing_count = [], 0
    for cell in problem.demand.cells:
        if cell.level_index not in sufficient:
            continue
        poly = Polygon(cell.poly)
        offered, recipe_indexes = sufficient[cell.level_index]
        missing = poly.difference(offered)
        unreachable = missing.area > max(TOL, poly.area*1e-9)
        missing_count += unreachable
        cells.append({"cell_id": cell.id, "level_index": cell.level_index,
            "sufficient_recipe_indexes": recipe_indexes, "source_area_mm2": poly.area,
            "unreachable_area_mm2": missing.area, "unreachable": unreachable,
            "unreachable_geometry": mapping(missing)})
    return {"schema_version": "host-search-domain-bound/v1", "policy": POLICY,
        "mathematical_status": "necessary_optimistic_reachability_upper_bound_not_constructive_feasibility",
        "numerical_policy": "closed_orthogonal_event_erosion_with_optimistic_area_tolerance_expansion",
        "negative_result_scope": "only_listed_recipes_straight_full40d_finite_service_halfwidth_common_Z_material_model",
        "direction": str(problem.demand.direction),
        "status": "impossible_in_stated_model" if missing_count else "not_ruled_out",
        "source_demand_removed": False, "placement_eligible": False,
        "original_cell_count": len(problem.demand.cells), "demanded_cell_count": len(cells),
        "unreachable_cell_count": missing_count,
        "unreachable_area_mm2": math.fsum(row["unreachable_area_mm2"] for row in cells),
        "cells": cells, "recipes": recipe_rows, "material_area_mm2": material.area,
        "host_policy": "intersection_of_all_Z_sections_not_arbitrary_selected_depths",
        "recipe_policy": "one_sufficient_recipe_at_each_point_no_weak_As_summation",
        "coordinate_contact_tolerance_mm": TOL, "host_containment_area_tolerance_mm2": AREA_TOLERANCE_MM2,
        "not_checked": list(NOT_CHECKED)}


def _uniform_zone_check(problem, zone, host, material, maximum_bars):
    if not isinstance(zone, LayoutZone) or not isinstance(zone.rebar, Rebar):
        raise ValueError("Typed legacy uniform LayoutZone required")
    if (type(zone.bar_count) is not int or not 1 <= zone.bar_count <= maximum_bars
            or type(zone.rebar.step) is not int or zone.rebar.step <= 0
            or type(zone.rebar.diameter) is not int or not 6 <= zone.rebar.diameter <= 40):
        raise ValueError("Bounded positive physical bar count/step/diameter required")
    if (type(zone.level_index) is not int or not 0 <= zone.level_index < len(problem.demand.levels)
            or problem.demand.level(zone.level_index).additional != zone.rebar):
        raise ValueError("Zone recipe differs from its explicit source level")
    for bounds in (zone.bbox, zone.demand_bbox):
        if not isinstance(bounds, tuple) or len(bounds) != 4 or any(not _finite(v) for v in bounds):
            raise ValueError("Finite typed installed/source bounds required")
    values = (zone.first_bar_coordinate_mm, zone.width_mm, zone.required_length_mm,
              zone.anchored_length_mm, zone.installed_length_mm)
    if any(not _finite(v) for v in values):
        raise ValueError("Finite uniform physical dimensions required")
    along = 0 if problem.demand.direction.axis is Axis.X else 1
    across = 1-along
    lo, hi = zone.bbox[along], zone.bbox[along+2]
    req_lo, req_hi = zone.demand_bbox[along], zone.demand_bbox[along+2]
    first, last = zone.first_bar_coordinate_mm, zone.first_bar_coordinate_mm+(zone.bar_count-1)*zone.rebar.step
    expected_width = (zone.bar_count-1)*zone.rebar.step
    expected_required, expected_anchored = req_hi-req_lo, req_hi-req_lo+80*zone.rebar.diameter
    if (not lo < hi or not req_lo < req_hi
            or not zone.demand_bbox[across] < zone.demand_bbox[across+2]
            or any(abs(a-b) > TOL for a, b in (
                (zone.bbox[across], first), (zone.bbox[across+2], last),
                (zone.width_mm, expected_width), (zone.installed_length_mm, hi-lo),
                (zone.required_length_mm, expected_required), (zone.anchored_length_mm, expected_anchored)))
            or lo > req_lo-40*zone.rebar.diameter+TOL
            or hi < req_hi+40*zone.rebar.diameter-TOL):
        raise ValueError("Uniform axes/bbox/count/length or complete40d metadata is inconsistent")
    coords = first + np.arange(zone.bar_count)*zone.rebar.step
    radius = zone.rebar.diameter/2+host.side_cover_mm
    if along == 0:
        envelopes = shapely.box(lo-host.side_cover_mm, coords-radius, hi+host.side_cover_mm, coords+radius)
    else:
        envelopes = shapely.box(coords-radius, lo-host.side_cover_mm, coords+radius, hi+host.side_cover_mm)
    outside = shapely.area(shapely.difference(envelopes, material))
    failed = np.flatnonzero(outside > AREA_TOLERANCE_MM2)
    return {"valid": len(failed) == 0, "placement_eligible": False,
        "policy": "legacy_uniform_actual_axes_and_full40d_exact_common_host/v1",
        "zone_id": zone.id, "physical_bar_count_checked": zone.bar_count,
        "host_blocked_bar_count": len(failed), "outside_host_area_sum_mm2": float(np.sum(outside)),
        "host_containment_area_tolerance_mm2": AREA_TOLERANCE_MM2,
        "blocked_bars": [{"bar_index": int(i), "axis_mm": float(coords[i]),
                          "outside_host_area_mm2": float(outside[i])} for i in failed],
        "not_checked": ["STO_nonuniform_axis_pattern", *NOT_CHECKED]}


def prepare_uniform_zone_host_guard(
    problem: LayoutProblem, host: OrthogonalSolidHost, *, maximum_bars: int = 10000,
) -> Callable[[LayoutZone], bool]:
    """Prepare the immutable exact host once for a GA candidate guard.

    Invalid typed geometry raises; a checked host conflict returns False. This
    guard addresses legacy uniform zones only, not the STO conditional-step plan.
    """
    _limit(maximum_bars, "maximum_bars")
    if (not isinstance(problem, LayoutProblem) or not isinstance(problem.demand.direction, Direction)
            or not isinstance(problem.demand.direction.axis, Axis)
            or not isinstance(problem.demand.direction.layer, Layer)
            or not _finite(problem.constraints.anchorage_diameters)
            or problem.constraints.anchorage_diameters != 40):
        raise ValueError("Typed problem with explicit full40d required")
    material = _common_material(host)
    return lambda zone: _uniform_zone_check(problem, zone, host, material, maximum_bars)["valid"]


def uniform_zone_host_check(
    problem: LayoutProblem, zone: LayoutZone, host: OrthogonalSolidHost, *, maximum_bars: int = 10000,
) -> dict:
    """Check actual straight uniform bar envelopes, not just zone bbox corners."""
    _limit(maximum_bars, "maximum_bars")
    if (not isinstance(problem, LayoutProblem) or not isinstance(problem.demand.direction, Direction)
            or not isinstance(problem.demand.direction.axis, Axis)
            or not isinstance(problem.demand.direction.layer, Layer)
            or not _finite(problem.constraints.anchorage_diameters)
            or problem.constraints.anchorage_diameters != 40):
        raise ValueError("Typed problem with explicit full40d required")
    return _uniform_zone_check(problem, zone, host, _common_material(host), maximum_bars)
