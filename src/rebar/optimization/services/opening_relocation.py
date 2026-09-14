"""Independent geometry/coverage checks for a narrowly scoped opening bypass.

Opening width means its projection ACROSS the bar, strictly below 300 mm.
Only closed rectangular interior holes are eligible, never outer recesses.
No holes are filled in the material and no FE demand is clipped or averaged.
The original finite service widths are translated, NOT enlarged to neighbours:
keeping the count alone cannot certify coverage after a nonuniform relocation.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
import math

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from rebar.models import Axis

from ..contracts.opening_relocation import OpeningRelocationLimitError, SourceServiceLane
from ..contracts.physical import PhysicalBar, PhysicalSourceBar
from ..contracts.plate import PlateProblem
from .detailing import rebar_mass_kg
from .physical_host_fit import _envelope, _polygons, _source_window, _validate_host
from .solid_host import AREA_TOLERANCE_MM2

POLICY = "closed-small-opening-transverse-translation/research-v1"
TOL = 1e-6


def material_and_holes(host):
    """Conservative material shared by every actual height section; no assumed Z."""
    _validate_host(host, 20000)
    material = host.sections[0].footprint
    for section in host.sections[1:]:
        material = material.intersection(section.footprint)
    outer = unary_union([Polygon(p.exterior) for p in _polygons(material)])
    holes = tuple(sorted((Polygon(ring) for p in _polygons(material) for ring in p.interiors),
                         key=lambda p: p.bounds))
    return material, outer, holes


def contained(bar, material, cover):
    return box(*_envelope(bar, cover)).difference(material).area <= AREA_TOLERANCE_MM2


def eligible_holes(bar, material, outer, holes, cover):
    """All current exclusions must be small holes; a small one cannot excuse a large one."""
    envelope = box(*_envelope(bar, cover))
    if envelope.difference(outer).area > AREA_TOLERANCE_MM2:
        return (), "outer_boundary"
    if contained(bar, material, cover):
        return (), "already_contained"
    across = 1 if bar.direction.axis is Axis.X else 0
    touched = tuple(i for i, hole in enumerate(holes) if envelope.intersection(hole).area > 0)
    if not touched:
        return (), "unclassified_exclusion"
    for i in touched:
        hole = holes[i]
        if not hole.equals(box(*hole.bounds)):
            return (), "nonrectangular_hole"
        if hole.bounds[across + 2] - hole.bounds[across] >= 300.0 - TOL:
            return (), "opening_width_not_below_300"
    return touched, "eligible"


def lane_map(lanes):
    result, pattern_options = {}, {}
    for lane in lanes:
        if not isinstance(lane, SourceServiceLane) or not isinstance(lane.source, PhysicalSourceBar):
            raise ValueError("Typed source service lanes required")
        key = (lane.source.direction, lane.source.id)
        if key in result:
            raise ValueError("Duplicate source service lane")
        if (not isinstance(lane.axis_window_mm, tuple) or len(lane.axis_window_mm) != 2
                or not isinstance(lane.service_half_widths_mm, tuple) or len(lane.service_half_widths_mm) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                       for v in (*lane.axis_window_mm, *lane.service_half_widths_mm))):
            raise ValueError("Finite typed source service intervals required")
        a, b = lane.axis_window_mm
        if (lane.source.background_step_mm != 300 or lane.nominal_step_mm not in (100, 150, 300) or not a < b
                or not a <= lane.source.transverse_axis_mm <= b
                or any(not math.isfinite(v) or v <= 0 or v > 150 for v in lane.service_half_widths_mm)):
            raise ValueError("Invalid original finite service window")
        if (type(lane.bar_index) is not int or lane.bar_index < 0
                or type(lane.component_index) is not int or lane.component_index != 0
                or not isinstance(lane.zone_id, str) or not lane.zone_id
                or lane.source.id != f"{lane.zone_id}/{lane.component_index}/{lane.bar_index}"):
            raise ValueError("Explicit single-component lane ownership required")
        q = (lane.source.transverse_axis_mm - lane.source.background_origin_mm) % 300
        delta = (lane.source.diameter_mm + lane.source.background_diameter_mm)/2
        group = (lane.source.direction, lane.zone_id, lane.component_index)
        patterns = {300: ((q,),), 150: ((100.0, 200.0),),
                    100: ((100.0, 200.0, 300-delta), (delta, 100.0, 200.0))}[lane.nominal_step_mm]
        if lane.nominal_step_mm == 300 and group in pattern_options:
            patterns = tuple(pattern for pattern in pattern_options[group]
                             if len(pattern) == 1 and abs(math.remainder(q-pattern[0], 300)) <= TOL)
        possible = set()
        for pattern in patterns:
            if not any(abs(math.remainder(q-offset, 300)) <= TOL for offset in pattern):
                continue
            half_widths = []
            for before in (True, False):
                gaps = [((q-offset) if before else (offset-q)) % 300 for offset in pattern]
                half_widths.append(min(300 if min(v, 300-v) <= TOL else v for v in gaps)/2)
            if all(abs(a-b) <= TOL for a, b in zip(half_widths, lane.service_half_widths_mm)):
                possible.add(pattern)
        possible &= pattern_options.get(group, possible)
        if not possible:
            raise ValueError("Source finite service widths do not reproduce one consistent original STO pattern")
        pattern_options[group] = possible
        result[key] = lane
    return result


def source_geometry_ok(bar, lanes_by_id):
    """Check every owner, full NEW 40d, original contacts and all owner zone windows."""
    parents = [lanes_by_id[(bar.direction, key)] for key in bar.source_bar_ids]
    for lane in parents:
        source = lane.source
        if (bar.steel_class != source.steel_class or bar.diameter_mm < source.diameter_mm
                or not lane.axis_window_mm[0] - TOL <= bar.transverse_axis_mm <= lane.axis_window_mm[1] + TOL
                or bar.installed_interval_mm[0] > source.required_interval_mm[0] - 40 * bar.diameter_mm + TOL
                or bar.installed_interval_mm[1] < source.required_interval_mm[1] + 40 * bar.diameter_mm - TOL):
            return False
        old_gap = abs(math.remainder(source.transverse_axis_mm - source.background_origin_mm, 300)) - (
            source.diameter_mm + source.background_diameter_mm) / 2
        gap = abs(math.remainder(bar.transverse_axis_mm - source.background_origin_mm, 300)) - (
            bar.diameter_mm + source.background_diameter_mm) / 2
        if gap < -TOL or abs(old_gap) <= TOL < abs(gap):
            return False
    return True


def service_boxes(bar, lanes_by_id):
    """Each rectangle retains ONE sufficient original recipe; weak As never sums."""
    along = 0 if bar.direction.axis is Axis.X else 1
    result = []
    for key in bar.source_bar_ids:
        lane = lanes_by_id[(bar.direction, key)]
        low, high = lane.source.required_interval_mm
        left = max(lane.axis_window_mm[0], bar.transverse_axis_mm - lane.service_half_widths_mm[0])
        right = min(lane.axis_window_mm[1], bar.transverse_axis_mm + lane.service_half_widths_mm[1])
        if right <= left:
            continue
        rectangle = (low, left, high, right) if along == 0 else (left, low, right, high)
        result.append((lane.source.diameter_mm, lane.nominal_step_mm, box(*rectangle)))
    return result


def collision(first, second):
    return (first.direction == second.direction
            and min(first.installed_interval_mm[1], second.installed_interval_mm[1])
                - max(first.installed_interval_mm[0], second.installed_interval_mm[0]) > TOL
            and abs(first.transverse_axis_mm - second.transverse_axis_mm)
                < (first.diameter_mm + second.diameter_mm) / 2 - TOL)


def collision_pairs(bars, maximum_checks=2000000):
    result, checks = set(), 0
    grouped = defaultdict(list)
    for bar in bars:
        grouped[bar.direction].append(bar)
    for direction, members in grouped.items():
        ordered = sorted(members, key=lambda b: (b.transverse_axis_mm, b.id))
        for i, first in enumerate(ordered):
            for second in ordered[i+1:]:
                if second.transverse_axis_mm - first.transverse_axis_mm >= 40:
                    break
                checks += 1
                if checks > maximum_checks:
                    raise OpeningRelocationLimitError("Complete collision check exceeds budget")
                if collision(first, second):
                    result.add((str(direction), *sorted((first.id, second.id))))
    return result


def coverage(problem, bars, lanes_by_id):
    """Fresh original FE polygons, geometric union; no centres, hole masks or averaging."""
    offered = {original.demand.direction: [entry for bar in bars if bar.direction == original.demand.direction
               for entry in service_boxes(bar, lanes_by_id)] for original in problem.direction_problems}
    return coverage_from_offers(problem, offered,
        policy="translated-original-finite-service-widths; no enlargement or weak-zone summation")


def coverage_from_offers(problem, offers_by_direction, *, policy):
    """Shared full-polygon demand check for independently constructed service areas.

    Every offer is (diameter, nominal step, geometry); weak offers never sum.
    Callers must derive these geometries from their own validated physical model.
    """
    directions, total_missing = [], 0
    for original in problem.direction_problems:
        demand = original.demand
        if any(type(level.index) is not int or level.index != index or level.recipe is None
               for index, level in enumerate(demand.levels)):
            raise ValueError("Explicit contiguous original demand levels required")
        offered = offers_by_direction.get(demand.direction, ())
        levels = {}
        for level in demand.levels:
            if not level.recipe.additions:
                continue
            if len(level.recipe.additions) != 1:
                raise ValueError("Opening correction requires the explicit single-addition source policy")
            spec = level.recipe.additions[0]
            levels[level.index] = unary_union([poly for d, s, poly in offered
                                               if d >= spec.diameter and s <= spec.step])
        cells, seen_ids = [], set()
        for cell in demand.cells:
            if type(cell.level_index) is not int or not 0 <= cell.level_index < len(demand.levels):
                raise ValueError("Unknown original FE demand level")
            if type(cell.id) is not int or cell.id in seen_ids:
                raise ValueError("Unique integer original FE identities required")
            seen_ids.add(cell.id)
            if (len(cell.poly) < 3 or any(len(point) != 2 or any(
                    isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                    for v in point) for point in cell.poly)):
                raise ValueError("Finite original FE polygon required")
            poly = Polygon(cell.poly)
            if not poly.is_valid or poly.is_empty or poly.area <= 0:
                raise ValueError("Valid positive-area original FE polygon required")
            if cell.level_index not in levels:
                continue
            missing = poly.difference(levels[cell.level_index]).area
            cells.append({"cell_id": cell.id, "uncovered_area_mm2": missing,
                          "covered": missing <= max(1e-6, poly.area * 1e-9)})
        failed = sum(not row["covered"] for row in cells)
        total_missing += failed
        directions.append({"direction": str(demand.direction), "demanded_cell_count": len(cells),
            "uncovered_cell_count": failed, "uncovered_area_mm2": math.fsum(c["uncovered_area_mm2"] for c in cells),
            "cells": cells})
    return {"status": "pass" if total_missing == 0 else "fail", "uncovered_cell_count": total_missing,
            "directions": directions, "source_demand_removed": False,
            "policy": policy}


def validate_inputs(bars, lanes, problem):
    if (not isinstance(problem, PlateProblem) or not isinstance(bars, tuple) or not 1 <= len(bars) <= 5000
            or not isinstance(lanes, tuple) or not 1 <= len(lanes) <= 5000):
        raise ValueError("Complete typed bounded physical plan and original demand required")
    sources = lane_map(lanes)
    used, identities = Counter(), set()
    for bar in bars:
        if not isinstance(bar, PhysicalBar):
            raise ValueError("Typed physical bars required")
        identity = (bar.direction, bar.id)
        if identity in identities:
            raise ValueError("Duplicate physical identity")
        identities.add(identity)
        # Strict old-axis validator, independently validates lengths, background and owners.
        _source_window(bar, tuple(sources[(bar.direction, key)].source for key in bar.source_bar_ids))
        if type(bar.diameter_mm) is not int or not 6 <= bar.diameter_mm <= 40:
            raise ValueError("Unsupported diameter")
        used.update((bar.direction, key) for key in bar.source_bar_ids)
        if not source_geometry_ok(bar, sources):
            raise ValueError("Original source geometry invalid")
    if set(used) != set(sources) or any(count != 1 for count in used.values()):
        raise ValueError("Complete once-only source ownership required")
    before = coverage(problem, bars, sources)
    if before["status"] != "pass":
        raise ValueError("Original finite service coverage failed; correction cannot hide source gaps")
    return sources, before


def check_relocation(before, after, lanes, problem, host, *, maximum_shift_mm=300.0):
    """Recompute ALL checks without using the solver's candidates or success flags."""
    sources, original_coverage = validate_inputs(before, lanes, problem)
    if (isinstance(maximum_shift_mm, bool) or not isinstance(maximum_shift_mm, (int, float))
            or not math.isfinite(maximum_shift_mm) or not 0 < maximum_shift_mm <= 300):
        raise ValueError("Explicit finite transverse search bound in (0, 300] required")
    material, outer, holes = material_and_holes(host)
    old = {(b.direction, b.id): b for b in before}
    if (not isinstance(after, tuple) or any(not isinstance(b, PhysicalBar) for b in after)
            or len(after) != len(before) or {(b.direction, b.id) for b in after} != set(old)):
        raise ValueError("Relocation changed physical inventory")
    moves = []
    for bar in after:
        prior = old[(bar.direction, bar.id)]
        if type(bar.diameter_mm) is not int:
            raise ValueError("Corrected physical diameter must remain an integer")
        if (isinstance(bar.transverse_axis_mm, bool) or not isinstance(bar.transverse_axis_mm, (int, float))
                or not math.isfinite(bar.transverse_axis_mm)
                or not isinstance(bar.installed_interval_mm, tuple) or len(bar.installed_interval_mm) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                       for v in bar.installed_interval_mm)):
            raise ValueError("Corrected physical axes and endpoints must be finite numbers")
        if replace(bar, transverse_axis_mm=prior.transverse_axis_mm,
                   installed_interval_mm=prior.installed_interval_mm) != prior:
            raise ValueError("Relocation changed diameter, steel, identity or source owners")
        if (abs(bar.installed_length_mm - prior.installed_length_mm) > TOL
                or not source_geometry_ok(bar, sources)):
            raise ValueError("Relocation changed length, full 40d, source windows or background contact")
        if bar == prior:
            continue
        touched, status = eligible_holes(prior, material, outer, holes, host.side_cover_mm)
        if status != "eligible" or not contained(bar, material, host.side_cover_mm):
            raise ValueError("Relocation is not a contained bypass of eligible small interior holes")
        delta = bar.transverse_axis_mm - prior.transverse_axis_mm
        if not TOL < abs(delta) <= maximum_shift_mm + TOL:
            raise ValueError("Relocation is not a bounded transverse correction")
        moves.append({"direction": str(bar.direction), "bar_id": bar.id, "axis_shift_mm": delta,
            "longitudinal_shift_mm": bar.installed_interval_mm[0] - prior.installed_interval_mm[0],
            "opening_indexes": list(touched), "opening_bboxes_mm": [list(holes[i].bounds) for i in touched],
            "original_axis_mm": prior.transverse_axis_mm, "corrected_axis_mm": bar.transverse_axis_mm})
    previous_pairs, new_pairs = collision_pairs(before), collision_pairs(after)
    moved_ids = {(row["direction"], row["bar_id"]) for row in moves}
    changed_pairs = {p for p in new_pairs if (p[0], p[1]) in moved_ids or (p[0], p[2]) in moved_ids}
    if new_pairs - previous_pairs or changed_pairs:
        raise ValueError("Relocation introduced same-direction body intersections")
    checked = coverage(problem, after, sources)
    if checked["status"] != "pass":
        raise ValueError("Relocation lost original FE coverage")
    counts = Counter((str(b.direction), b.steel_class, b.diameter_mm, round(b.installed_length_mm, 6)) for b in before)
    if counts != Counter((str(b.direction), b.steel_class, b.diameter_mm, round(b.installed_length_mm, 6)) for b in after):
        raise ValueError("Relocation changed the cutting inventory")
    mass = math.fsum(rebar_mass_kg(b.diameter_mm, b.installed_length_mm, 1) for b in after)
    return {"policy_id": POLICY, "status": "checked_under_explicit_profile", "placement_eligible": False,
        "engineering_approval": False, "source_demand_removed": False, "moved_bar_count": len(moves), "moves": moves,
        "physical_bar_count": len(after), "additional_mass_kg": mass, "mass_delta_kg": 0.0,
        "lengths_diameters_counts_owners_and_cutting_inventory_preserved": True,
        "source_coverage_before": original_coverage, "source_coverage_after": checked,
        "background_contact_and_penetration_status": "pass", "full_new_diameter_40d_status": "pass",
        "same_direction_body_pairs_before": len(previous_pairs), "same_direction_body_pairs_after": len(new_pairs),
        "new_same_direction_body_pairs": 0,
        "host_blocked_before": sum(not contained(b, material, host.side_cover_mm) for b in before),
        "host_blocked_after": sum(not contained(b, material, host.side_cover_mm) for b in after),
        "host_mode": "conservative_common_footprint_of_every_height_section",
        "opening_width_definition": "projection transverse to the bar; strictly less than 300 mm",
        "not_checked": ["actual_Z_and_cross_direction_3D_collisions", "existing_revit_reinforcement",
                        "engineering_acceptance_of_finite_service_model", "revit_readback", "permanent_placement"]}
