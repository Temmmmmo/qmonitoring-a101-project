"""Explain exact XY host failures without changing demand or acceptance rules.

Geometry of all Solid height sections is intersected, as in the current physical
checker. These diagnostics distinguish outer contours, holes and cover; they are
not a normative anchorage calculation or permission to remove an offending FE.
"""
from __future__ import annotations

from collections import Counter
import math

from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

from rebar.models import Axis

from ..contracts.plate import PlateProblem
from .fe_host_repair import _valid_bars
from .physical_host_fit import _polygons, _validate_host
from .solid_host import AREA_TOLERANCE_MM2


def _material_parts(host):
    _validate_host(host, 20000)
    material = host.sections[0].footprint
    for section in host.sections[1:]:
        material = material.intersection(section.footprint)
    if material.is_empty or material.area <= AREA_TOLERANCE_MM2:
        raise ValueError("No common XY material across host sections; cannot classify a usable slab")
    # Use actual exterior rings, NOT the bounding rectangle or convex hull. A
    # recess connected to the exterior is an edge, not an enclosed opening.
    outer = unary_union([Polygon(part.exterior) for part in _polygons(material)])
    return material, outer, outer.difference(material)


def _footprints(bar, cover):
    lo, hi = bar.installed_interval_mm
    q, radius = bar.transverse_axis_mm, bar.diameter_mm / 2
    body = (lo, q-radius, hi, q+radius)
    envelope = (lo-cover, q-radius-cover, hi+cover, q+radius+cover)
    line = ((lo, q), (hi, q))
    if bar.direction.axis is Axis.Y:
        body = body[1], body[0], body[3], body[2]
        envelope = envelope[1], envelope[0], envelope[3], envelope[2]
        line = tuple((y, x) for x, y in line)
    return box(*body), box(*envelope), LineString(line)


def diagnose_physical_host_failures(bars, host):
    """Return every failing physical ID with disjoint, directly measurable causes.

    Full inventory is retained; the output cannot authorize placement even when
    all bodies fit. Diameter and length validation follows the straight-bar
    checker, including the bounded 5000-bar/11700-mm research scope.
    """
    if not isinstance(bars, tuple):
        raise ValueError("A typed tuple of physical bars is required")
    if bars:
        _valid_bars(bars)
    material, outer, holes = _material_parts(host)
    failures, categories, directions = [], Counter(), Counter()
    for bar in bars:
        body, envelope, line = _footprints(bar, host.side_cover_mm)
        missing = envelope.difference(material).area
        if missing <= AREA_TOLERANCE_MM2:
            continue
        outer_area = envelope.difference(outer).area
        holes_area = envelope.intersection(holes).area
        # Classify each positive component, rather than discarding two small
        # components whose combined area exceeds the accepted tolerance.
        category = "both" if outer_area > 0 and holes_area > 0 else "outer_edge" if outer_area > 0 else "opening"
        body_missing = body.difference(material).area
        categories[category] += 1
        directions[str(bar.direction)] += 1
        failures.append({"direction": str(bar.direction), "bar_id": bar.id,
            "source_bar_ids": list(bar.source_bar_ids), "category": category,
            "diameter_mm": bar.diameter_mm, "axis_mm": bar.transverse_axis_mm,
            "installed_interval_mm": list(bar.installed_interval_mm),
            "outer_envelope_area_mm2": outer_area, "opening_envelope_area_mm2": holes_area,
            "outside_material_envelope_area_mm2": missing,
            "outside_material_body_area_mm2": body_missing,
            "centreline_outside_material_length_mm": line.difference(material).length,
            "cover_only": body_missing <= AREA_TOLERANCE_MM2})
    return {"schema_version": "physical-host-failure-diagnostics/v1", "units": "mm",
        "scope": "common-material-of-all-height-sections/straight-additional-bars",
        "placement_eligible": False, "engineering_approval": False, "source_demand_removed": False,
        "physical_bar_count": len(bars), "host_failed_bar_count": len(failures),
        "host_contained_bar_count": len(bars)-len(failures),
        "categories": {key: categories[key] for key in ("outer_edge", "opening", "both")},
        "failures_by_direction": dict(sorted(directions.items())),
        "cover_only_failure_count": sum(row["cover_only"] for row in failures),
        "failures": failures, "area_tolerance_mm2": AREA_TOLERANCE_MM2,
        "side_cover_mm": host.side_cover_mm,
        "not_checked": ["anchorage", "source_coverage", "stock_cutting", "bar_collisions",
            "actual_Z", "existing_revit_reinforcement", "engineering_acceptance"]}


def inspect_source_material_mismatch(problem, host):
    """Locate original demanded FE over real voids; do not clip or average them.

    A FE over an opening is a source/model discrepancy, not proof that it may be
    deleted. Project-specific redistribution or fixed engineered reinforcement
    must be a separate, explicitly checked input/policy.
    """
    if not isinstance(problem, PlateProblem):
        raise ValueError("Complete typed four-direction PlateProblem is required")
    if sum(len(p.demand.cells) for p in problem.direction_problems) > 20000:
        raise ValueError("At most 20000 source cells; no truncation")
    vertex_count = 0
    for item in problem.direction_problems:
        for cell in item.demand.cells:
            if not isinstance(cell.poly, tuple):
                raise ValueError("Source polygons must be tuples")
            vertex_count += len(cell.poly)
            if vertex_count > 200000:
                raise ValueError("Total source vertex budget exceeded; no truncation")
    material, outer, holes = _material_parts(host)
    rows, demanded_count = [], 0
    for item in problem.direction_problems:
        seen = set()
        for cell in item.demand.cells:
            if type(cell.id) is not int or cell.id in seen:
                raise ValueError("Unique integer FE IDs required within each direction")
            if type(cell.level_index) is not int or not 0 <= cell.level_index < len(item.demand.levels):
                raise ValueError("Known integer source level index required")
            seen.add(cell.id)
            if (not isinstance(cell.poly, tuple) or not 3 <= len(cell.poly) <= 4096
                    or any(not isinstance(point, tuple) or len(point) != 2 for point in cell.poly)
                    or any(isinstance(v, bool) or not isinstance(v, (int, float))
                        or not math.isfinite(v) or abs(v) > 1e9 for point in cell.poly for v in point)):
                raise ValueError("Finite bounded source polygons required")
            polygon = Polygon(cell.poly)
            if not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
                raise ValueError("Positive simple source polygons required")
            recipe = item.demand.level(cell.level_index).recipe
            if recipe is None:
                raise ValueError("Explicit source recipes required; no implicit background")
            if not recipe.additions:
                continue
            demanded_count += 1
            outer_area, holes_area = polygon.difference(outer).area, polygon.intersection(holes).area
            outside = polygon.difference(material).area
            if outside <= AREA_TOLERANCE_MM2:
                continue
            rows.append({"direction": str(item.demand.direction), "cell_id": cell.id,
                "source_cell_area_mm2": polygon.area, "outside_material_area_mm2": outside,
                "outside_outer_area_mm2": outer_area, "over_openings_area_mm2": holes_area,
                "source_polygon_unchanged": True, "source_value_unchanged": True})
    return {"schema_version": "source-material-mismatch/v1", "units": "mm",
        "scope": "common-material-of-all-height-sections/source-polygons/no-cover",
        "placement_eligible": False, "engineering_approval": False,
        "source_demand_removed": False, "source_demand_values_changed": False,
        "required_cell_count": demanded_count, "mismatched_direction_cell_count": len(rows),
        "outside_outer_direction_cell_count": sum(row["outside_outer_area_mm2"] > 0 for row in rows),
        "over_openings_direction_cell_count": sum(row["over_openings_area_mm2"] > 0 for row in rows),
        "both_direction_cell_count": sum(row["outside_outer_area_mm2"] > 0 and row["over_openings_area_mm2"] > 0
            for row in rows),
        "cells": rows, "area_tolerance_mm2": AREA_TOLERANCE_MM2,
        "coverage_acceptance_changed": False,
        "warning": "Do not delete, crop or downgrade these FE; any redistribution needs its own original-source proof."}


def inspect_straight_40d_bbox_obstruction(problem, host):
    """Necessary impossibility witness for the CURRENT straight/full40d model.

    Even an unlimited number of bars in the bounding box of the UNION of all
    host slices cannot serve an FE portion closer than cover+40d to an along
    extreme. We deliberately ignore holes, stock, phases, source ownership and
    transverse service width: relaxing them cannot repair this obstruction.
    This is not a normative calculation or a claim about bent/redistributed
    reinforcement. An empty witness is NOT proof of feasibility.
    """
    # Reuse strict source/host validation and resource bounds, without mutation.
    inspect_source_material_mismatch(problem, host)
    bounds = unary_union([section.footprint for section in host.sections]).bounds
    rows, count = [], 0
    for item in problem.direction_problems:
        axis = 0 if item.demand.direction.axis is Axis.X else 1
        for cell in item.demand.cells:
            additions = item.demand.level(cell.level_index).recipe.additions
            if not additions:
                continue
            if len(additions) != 1:
                raise ValueError("Obstruction proof currently requires single-addition source recipes")
            diameter = additions[0].diameter
            if type(diameter) is not int or not 6 <= diameter <= 40:
                raise ValueError("Explicit supported source diameter required")
            count += 1
            # Axial endpoints are flat in the existing straight-bar model; end
            # cover applies, not an invented extra axial radius.
            # Account optimistically for the actual host area tolerance: an
            # endpoint may cross a global extreme by at most area/envelope
            # width. Larger sufficient diameters can only tighten this bound.
            endpoint_slack = AREA_TOLERANCE_MM2/(diameter+2*host.side_cover_mm)
            lo = bounds[axis]+host.side_cover_mm+40*diameter-endpoint_slack
            hi = bounds[axis+2]-host.side_cover_mm-40*diameter+endpoint_slack
            polygon = Polygon(cell.poly)
            if lo >= hi:
                missing = polygon.area
            else:
                transverse = 1-axis
                lower, upper = polygon.bounds[transverse], polygon.bounds[transverse+2]
                core = box(lo, lower, hi, upper) if axis == 0 else box(lower, lo, upper, hi)
                missing = polygon.difference(core).area
            coverage_tolerance = max(1e-6, polygon.area*1e-9)
            if missing > max(AREA_TOLERANCE_MM2, coverage_tolerance):
                rows.append({"direction": str(item.demand.direction), "cell_id": cell.id,
                    "required_minimum_diameter_mm": diameter,
                    "optimistic_along_core_interval_mm": [lo, hi],
                    "host_tolerance_endpoint_slack_mm": endpoint_slack,
                    "source_coverage_tolerance_mm2": coverage_tolerance,
                    "source_polygon_bounds_mm": list(polygon.bounds),
                    "outside_optimistic_core_area_mm2": missing,
                    "source_cell_area_mm2": polygon.area})
    return {"schema_version": "straight-full40d-bbox-obstruction/v1", "units": "mm",
        "placement_eligible": False, "engineering_approval": False,
        "source_demand_removed": False, "source_demand_values_changed": False,
        "policy_changed": False, "full40d_is_normatively_required": "not_asserted",
        "scope": "current-pointwise-single-addition-straight-core-40d-policy",
        "optimistic_host_union_bbox_mm": list(bounds), "side_cover_mm": host.side_cover_mm,
        "required_cell_count": count, "obstructed_direction_cell_count": len(rows),
        "obstructions_by_direction": dict(sorted(Counter(row["direction"] for row in rows).items())),
        "cells": rows, "area_tolerance_mm2": AREA_TOLERANCE_MM2,
        "status": "infeasible_under_stated_straight_40d_policy" if rows else "no_bbox_obstruction_not_a_feasibility_proof",
        "assumptions": ["straight bars remain inside host with existing end cover",
            "both ends retain full40d outside every served FE portion",
            "sufficient individual offer has diameter at least the source diameter",
            "source FE polygons and values remain unchanged"],
        "relaxed_constraints": ["holes", "stock_and_catalogue", "bar_count", "collisions",
            "source_ownership", "STO_phases", "transverse_service_width", "height_slice_assignment"],
        "warning": "A policy-specific geometric witness, not permission to drop demand or shorten anchorage."}
