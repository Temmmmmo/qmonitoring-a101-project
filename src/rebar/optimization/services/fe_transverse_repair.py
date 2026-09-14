"""Independent joint certification of pure transverse FE-preserving bar moves.

The baseline can already contain checked longitudinal FE-based repairs. Its
actual installed cores, not legacy zone bounding boxes, freeze each owner's FE
service before any move. This broader research profile does not claim the old
small-hole-only permission or authorize structural Revit placement.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import math

from shapely.geometry import Polygon
from shapely.strtree import STRtree

from ..contracts.opening_relocation import SourceServiceLane
from ..contracts.physical import PhysicalBar, PhysicalSourceBar
from ..contracts.plate import PLATE_DIRECTIONS, PlateProblem
from .bar_schedule import BarScheduleGroup, build_bar_schedule
from .fe_host_repair import _installed_offers, _valid_bars, installed_fe_coverage
from .opening_relocation import collision_pairs, contained, lane_map, material_and_holes
from .physical_host_fit import _source_window
from .solid_host import OrthogonalSolidHost
from .stock_cutting import check_stock_cutting

POLICY = "frozen-current-installed-FE-pure-transverse-full40d/research-v1"
TOL = 1e-6
MAX_TOTAL_CELLS = 10000
MAX_TOTAL_FE_VERTICES = 100000
MAX_FE_INTERSECTION_CHECKS = 100000


def _identity(bar):
    return bar.direction, bar.id


def _background_and_window(bar, parents):
    for lane in parents:
        source = lane.source
        if (bar.steel_class != source.steel_class or bar.diameter_mm < source.diameter_mm
                or not lane.axis_window_mm[0]-TOL <= bar.transverse_axis_mm <= lane.axis_window_mm[1]+TOL):
            raise ValueError("Original material, diameter and finite source windows must be preserved")
        old_gap = abs(math.remainder(source.transverse_axis_mm-source.background_origin_mm, 300)) - (
            source.diameter_mm+source.background_diameter_mm)/2
        new_gap = abs(math.remainder(bar.transverse_axis_mm-source.background_origin_mm, 300)) - (
            bar.diameter_mm+source.background_diameter_mm)/2
        if new_gap < -TOL or abs(old_gap) <= TOL < abs(new_gap):
            raise ValueError("Original background contact or no-penetration rule was lost")


def _baseline(before, lanes, problem):
    if (not isinstance(problem, PlateProblem) or len(problem.direction_problems) != 4
            or {p.demand.direction for p in problem.direction_problems} != set(PLATE_DIRECTIONS)
            or sum(len(p.demand.cells) for p in problem.direction_problems) > MAX_TOTAL_CELLS
            or sum(len(cell.poly) for p in problem.direction_problems for cell in p.demand.cells) > MAX_TOTAL_FE_VERTICES):
        raise ValueError("Bounded complete four-direction original FE problem required")
    _valid_bars(before)
    if not isinstance(lanes, tuple) or not 1 <= len(lanes) <= 5000:
        raise ValueError("Bounded immutable source lane tuple required")
    for lane in lanes:
        if not isinstance(lane, SourceServiceLane) or not isinstance(lane.source, PhysicalSourceBar):
            raise ValueError("Typed original source lanes required")
        # Validate the ORIGINAL source's own geometry and old contact, not the
        # already recertified physical bar against an obsolete zone-wide40d box.
        source = lane.source
        carrier = PhysicalBar(source.id, source.direction, source.steel_class,
            source.diameter_mm, source.transverse_axis_mm, source.installed_interval_mm, (source.id,))
        _source_window(carrier, (source,))
    sources = lane_map(lanes)
    used = Counter()
    for bar in before:
        try:
            parents = tuple(sources[bar.direction, key] for key in bar.source_bar_ids)
        except KeyError as error:
            raise ValueError("Unknown original source owner") from error
        _background_and_window(bar, parents)
        used.update((bar.direction, key) for key in bar.source_bar_ids)
    if set(used) != set(sources) or any(count != 1 for count in used.values()):
        raise ValueError("Every original source owner must occur exactly once")
    coverage = installed_fe_coverage(problem, before, sources)
    if coverage["status"] != "pass":
        raise ValueError("Complete original FE coverage by actual baseline cores required")
    return sources, coverage


def check_fe_transverse_repair(
    before: tuple[PhysicalBar, ...], after: tuple[PhysicalBar, ...],
    lanes: tuple[SourceServiceLane, ...], problem: PlateProblem, host: OrthogonalSolidHost, *,
    maximum_shift_mm: float = 300.0, stock_time_limit_s: float = 30,
) -> dict:
    """Validate a whole party; no search and no trust in candidate success flags.

    Every installed end, source owner, length, diameter and steel class is fixed.
    Every sufficient FE portion served by each baseline owner must remain served
    by that same owner. Only genuinely changed bars must clear the host and every
    same-direction body collision; unchanged unresolved failures stay visible.
    """
    for label, value, lower, upper in (("maximum_shift_mm", maximum_shift_mm, 0, 300),
            ("stock_time_limit_s", stock_time_limit_s, .001, 60)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not lower <= value <= upper):
            raise ValueError(f"{label} must be a finite number in {lower}..{upper}")
    sources, before_coverage = _baseline(before, lanes, problem)
    _valid_bars(after)
    old = {_identity(bar): bar for bar in before}
    if set(old) != {_identity(bar) for bar in after}:
        raise ValueError("Complete physical party identities/count must be preserved")
    material, _, _ = material_and_holes(host)
    changed = set()
    for bar in after:
        previous = old[_identity(bar)]
        if replace(bar, transverse_axis_mm=previous.transverse_axis_mm) != previous:
            raise ValueError("Only transverse axes may change; installed ends and full inventory are fixed")
        if abs(bar.transverse_axis_mm-previous.transverse_axis_mm) > maximum_shift_mm+TOL:
            raise ValueError("Transverse displacement exceeds the original-baseline bound")
        _background_and_window(bar, tuple(sources[bar.direction, key] for key in bar.source_bar_ids))
        if bar != previous:
            changed.add((str(bar.direction), bar.id))
            if not contained(bar, material, host.side_cover_mm):
                raise ValueError("Changed bar is outside actual host material with side cover")
    trees = {}
    for original in problem.direction_problems:
        demand = original.demand
        cells = [cell for cell in demand.cells if demand.level(cell.level_index).recipe.additions]
        polygons = [Polygon(cell.poly) for cell in cells]
        specs = [demand.level(cell.level_index).recipe.additions[0] for cell in cells]
        trees[demand.direction] = STRtree(polygons), polygons, specs, cells
    obligations, moves, intersection_checks = [], [], 0
    for bar in after:
        previous = old[_identity(bar)]
        tree, polygons, specs, cells = trees[bar.direction]
        along = 0 if str(bar.direction).endswith("X") else 1
        owners, any_demand = [], False
        for owner in bar.source_bar_ids:
            old_offers = _installed_offers(replace(previous, source_bar_ids=(owner,)), sources)
            new_offers = _installed_offers(replace(bar, source_bar_ids=(owner,)), sources)
            if len(old_offers) != len(new_offers):
                raise ValueError("Original finite service window was lost")
            lows, highs, ids = [], [], set()
            for (diameter, step, before_shape), (_, _, after_shape) in zip(old_offers, new_offers, strict=True):
                for index in tree.query(before_shape):
                    intersection_checks += 1
                    if intersection_checks > MAX_FE_INTERSECTION_CHECKS:
                        raise ValueError("Frozen original FE intersection resource budget exceeded")
                    if diameter < specs[index].diameter or step > specs[index].step:
                        continue
                    hit = before_shape.intersection(polygons[index])
                    if hit.area <= 0:
                        continue
                    if hit.difference(after_shape).area > 0:
                        raise ValueError("A frozen original owner FE portion was lost")
                    lows.append(hit.bounds[along])
                    highs.append(hit.bounds[along+2])
                    ids.add(cells[index].id)
            interval = (min(lows), max(highs)) if lows else None
            if interval is not None:
                any_demand = True
                if (bar.installed_interval_mm[0] > interval[0]-40*bar.diameter_mm+TOL
                        or bar.installed_interval_mm[1] < interval[1]+40*bar.diameter_mm-TOL):
                    raise ValueError("Full40d around a frozen original FE portion was lost")
            owners.append({"source_id": owner, "required_FE_interval_mm": interval,
                           "served_cell_ids": tuple(sorted(ids))})
        obligations.append({"direction": str(bar.direction), "bar_id": bar.id, "owners": tuple(owners)})
        if previous != bar:
            if not any_demand:
                raise ValueError("No-demand bars are frozen; not silently removed or repositioned")
            moves.append({"direction": str(bar.direction), "bar_id": bar.id,
                "original_axis_mm": previous.transverse_axis_mm, "after_axis_mm": bar.transverse_axis_mm,
                "axis_shift_mm": bar.transverse_axis_mm-previous.transverse_axis_mm,
                "installed_interval_mm": bar.installed_interval_mm})
    old_pairs, new_pairs = collision_pairs(before), collision_pairs(after)
    changed_pairs = [pair for pair in new_pairs if (pair[0], pair[1]) in changed or (pair[0], pair[2]) in changed]
    if new_pairs-old_pairs or changed_pairs:
        raise ValueError("New or changed-bar same-direction body collision")
    after_coverage = installed_fe_coverage(problem, after, sources)
    if after_coverage["status"] != "pass":
        raise ValueError("Complete original demand not covered by actual installed cores")
    schedule = build_bar_schedule(tuple(BarScheduleGroup(f"{bar.direction}/{bar.id}", bar.diameter_mm,
        bar.installed_length_mm, 1, bar.steel_class) for bar in after))
    stock = check_stock_cutting(schedule, time_limit_s=stock_time_limit_s)
    def counts(bars):
        return {str(direction): sum(bar.direction == direction and not contained(
            bar, material, host.side_cover_mm) for bar in bars) for direction in PLATE_DIRECTIONS}
    before_host, after_host = counts(before), counts(after)
    blocked = sum(after_host.values())
    return {"schema_version": "fe-transverse-repair-check/v1", "policy": POLICY,
        "placement_eligible": False, "structural_placement_supported": False, "engineering_approval": False,
        "source_demand_removed": False, "source_demand_values_changed": False,
        "legacy_source_certificate_reused": False, "physical_bar_count": len(after),
        "additional_mass_kg": math.fsum(row.total_mass_kg for row in schedule), "position_count": len(schedule),
        "all_installed_intervals_unchanged": True, "cutting_inventory_unchanged": True,
        "all_original_owner_FE_pieces_preserved": True, "full40d_from_frozen_FE_portions": "pass",
        "source_background_contacts_and_no_penetration": "pass", "maximum_shift_mm": maximum_shift_mm,
        "source_coverage_before": before_coverage, "source_coverage": after_coverage,
        "frozen_original_owner_FE_obligations": obligations, "stock_cutting": stock,
        "host_blocked_before": sum(before_host.values()), "host_blocked_after": blocked,
        "host_counts_before": before_host, "host_counts_after": after_host,
        "same_direction_body_pairs_before": len(old_pairs), "same_direction_body_pairs_after": len(new_pairs),
        "new_body_pairs": len(new_pairs-old_pairs), "changed_bars_with_body_collisions": len(changed_pairs),
        "frozen_owner_FE_loss_tolerance_mm2": 0,
        "frozen_FE_intersection_checks": intersection_checks,
        "resource_limits": {"total_FE_cells": MAX_TOTAL_CELLS, "total_FE_vertices": MAX_TOTAL_FE_VERTICES,
            "FE_intersection_checks": MAX_FE_INTERSECTION_CHECKS, "physical_bars": 5000, "source_lanes": 5000},
        "moved_bar_count": len(moves), "moves": moves,
        "status": "blocked_host" if blocked else "blocked_body_collisions" if new_pairs else
            "blocked_stock" if stock["status"] != "pass" else "research_checks_passed_not_placement_approved",
        "not_checked": ["rectangular_LayoutZone_regrouping_and_minimum_width",
            "normative_anchorage_instead_of_control40d", "actual_Z_and_existing_reinforcement",
            "Revit_readback", "engineering_acceptance"],
        "warning": "Broader FE-preserving axis research, not the old narrow-hole-only profile or a Revit permission."}
