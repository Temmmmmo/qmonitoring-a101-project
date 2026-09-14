"""Independent FE-based recertification of fixed-axis physical straight bars.

Legacy rectangles are NOT rewritten. The new longitudinal obligations are frozen
from every sufficient original FE intersection BEFORE search. A shortened bar's
service is clipped by its actual ends minus full 40d, then the complete original
demand is checked again. This is a physical research candidate, not rectangular
LayoutZone reconstruction, a normative anchorage calculation or a Revit packet.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import math

from shapely.geometry import Polygon, box
from shapely.strtree import STRtree

from rebar.models import Axis, Direction, Layer

from ..contracts.physical import PhysicalBar
from ..contracts.plate import PlateProblem
from .bar_schedule import BarScheduleGroup, build_bar_schedule
from .opening_relocation import (
    collision_pairs, contained, coverage_from_offers, lane_map, material_and_holes,
    service_boxes, source_geometry_ok,
)
from .physical_host_fit import _source_window
from .stock_cutting import check_stock_cutting

POLICY = "frozen-original-FE-service-fixed-axes-full40d/research-v1"
TOL = 1e-6
NOT_CHECKED = (
    "rectangular_LayoutZone_regrouping_and_minimum_width",
    "normative_STO_anchorage_instead_of_control40d",
    "actual_Z_and_cross_direction_3D_collisions",
    "existing_revit_reinforcement", "Revit_readback", "engineering_acceptance",
)


def _identity(bar):
    return bar.direction, bar.id


def _valid_bars(bars):
    if not isinstance(bars, tuple) or not 1 <= len(bars) <= 5000:
        raise ValueError("Complete bounded physical party required")
    found = set()
    for bar in bars:
        if (not isinstance(bar, PhysicalBar) or not isinstance(bar.direction, Direction)
                or not isinstance(bar.direction.axis, Axis) or not isinstance(bar.direction.layer, Layer)
                or not isinstance(bar.id, str) or not bar.id.strip()
                or not isinstance(bar.steel_class, str) or not bar.steel_class.strip()
                or not isinstance(bar.source_bar_ids, tuple) or not bar.source_bar_ids
                or any(not isinstance(key, str) or not key.strip() for key in bar.source_bar_ids)
                or len(set(bar.source_bar_ids)) != len(bar.source_bar_ids) or _identity(bar) in found):
            raise ValueError("Typed unique physical identities required")
        found.add(_identity(bar))
        if (not isinstance(bar.installed_interval_mm, tuple) or len(bar.installed_interval_mm) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                       or abs(v) > 1e9 for v in (*bar.installed_interval_mm, bar.transverse_axis_mm))
                or type(bar.diameter_mm) is not int or not 6 <= bar.diameter_mm <= 40
                or not 0 < bar.installed_length_mm <= 11700+TOL):
            raise ValueError("Finite positive straight bars up to stock length required")


def _installed_offers(bar, sources):
    """Preserve per-owner recipes/windows; never serve using bare legacy bbox."""
    low = bar.installed_interval_mm[0] + 40 * bar.diameter_mm
    high = bar.installed_interval_mm[1] - 40 * bar.diameter_mm
    if low >= high:
        return ()
    axis = 0 if str(bar.direction).endswith("X") else 1
    result = []
    for diameter, step, original in service_boxes(bar, sources):
        bounds = list(original.bounds)
        bounds[axis] = max(bounds[axis], low)
        bounds[axis+2] = min(bounds[axis+2], high)
        if bounds[axis] < bounds[axis+2]:
            result.append((diameter, step, box(*bounds)))
    return tuple(result)


def installed_fe_coverage(problem, bars, sources):
    if not isinstance(problem, PlateProblem):
        raise ValueError("Typed complete four-direction PlateProblem required")
    _valid_bars(bars)
    offers = {}
    for bar in bars:
        offers.setdefault(bar.direction, []).extend(_installed_offers(bar, sources))
    return coverage_from_offers(problem, offers, policy=POLICY)


def _baseline_sources(bars, lanes, problem):
    if not isinstance(problem, PlateProblem):
        raise ValueError("Typed complete four-direction PlateProblem required")
    _valid_bars(bars)
    sources = lane_map(lanes)
    used = Counter()
    for bar in bars:
        parents = tuple(sources[(bar.direction, key)].source for key in bar.source_bar_ids)
        if not parents:
            raise ValueError("Every bar needs original owners")
        # The input may already include a separately checked transverse bypass.
        # Validate original metadata at its old axis, then current axis separately.
        _source_window(replace(bar, transverse_axis_mm=parents[0].transverse_axis_mm), parents)
        if not source_geometry_ok(bar, sources):
            raise ValueError("Original complete source/background/full40d geometry required")
        used.update((bar.direction, key) for key in bar.source_bar_ids)
    if set(used) != set(sources) or any(value != 1 for value in used.values()):
        raise ValueError("Every original source owner must occur exactly once")
    if installed_fe_coverage(problem, bars, sources)["status"] != "pass":
        raise ValueError("Original full FE coverage required before recertification")
    return sources


def derive_fe_obligations(bars, lanes, problem):
    """Freeze ALL served FE portions per original owner, never from a proposal."""
    sources = _baseline_sources(bars, lanes, problem)
    trees = {}
    for original in problem.direction_problems:
        demand = original.demand
        cells = [cell for cell in demand.cells if demand.level(cell.level_index).recipe.additions]
        polygons = [Polygon(cell.poly) for cell in cells]
        specs = [demand.level(cell.level_index).recipe.additions[0] for cell in cells]
        trees[demand.direction] = STRtree(polygons), polygons, specs, cells
    result = {}
    for bar in bars:
        tree, polygons, specs, cells = trees[bar.direction]
        axis = 0 if str(bar.direction).endswith("X") else 1
        lows, highs, served, owners = [], [], set(), []
        for owner in bar.source_bar_ids:
            owner_ids, owner_lows, owner_highs = set(), [], []
            one_owner = replace(bar, source_bar_ids=(owner,))
            for diameter, step, shape in _installed_offers(one_owner, sources):
                for index in tree.query(shape):
                    if diameter < specs[index].diameter or step > specs[index].step:
                        continue
                    hit = shape.intersection(polygons[index])
                    if hit.area > 0:
                        owner_lows.append(hit.bounds[axis])
                        owner_highs.append(hit.bounds[axis+2])
                        owner_ids.add(cells[index].id)
            interval = (min(owner_lows), max(owner_highs)) if owner_lows else None
            owners.append({"source_id": owner, "required_interval_mm": interval,
                           "served_cell_ids": tuple(sorted(owner_ids))})
            lows.extend(owner_lows)
            highs.extend(owner_highs)
            served.update(owner_ids)
        result[_identity(bar)] = {
            "required_interval_mm": (min(lows), max(highs)) if lows else None,
            "served_cell_ids": tuple(sorted(served)), "owners": tuple(owners),
            "original_required_interval_mm": (
                min(sources[(bar.direction, key)].source.required_interval_mm[0] for key in bar.source_bar_ids),
                max(sources[(bar.direction, key)].source.required_interval_mm[1] for key in bar.source_bar_ids)),
        }
    return result


def check_fe_host_repair(before, after, lanes, problem, host, *, stock_time_limit_s=30):
    """Fresh independent full-party verification; solver success is not trusted.

    Retaining unresolved ORIGINAL host failures/pairs is permitted only in the
    explicitly blocked research output. Every changed bar must fit, be collision
    free and preserve its frozen FE obligations plus full40d. No source is erased.
    """
    obligations = derive_fe_obligations(before, lanes, problem)
    sources = lane_map(lanes)
    _valid_bars(after)
    if {_identity(b) for b in before} != {_identity(b) for b in after}:
        raise ValueError("Complete physical party identities/count must be preserved")
    old = {_identity(bar): bar for bar in before}
    material, _, _ = material_and_holes(host)
    changed, changes = set(), []
    for bar in after:
        previous = old[_identity(bar)]
        if replace(bar, installed_interval_mm=previous.installed_interval_mm) != previous:
            raise ValueError("Axes, diameter, material and per-owner source identity must remain fixed")
        if bar == previous:
            continue
        changed.add((str(bar.direction), bar.id))
        interval = obligations[_identity(bar)]["required_interval_mm"]
        if interval is None:
            raise ValueError("No-demand source bars are frozen, not removed or reassigned")
        if (bar.installed_interval_mm[0] > interval[0]-40*bar.diameter_mm+TOL
                or bar.installed_interval_mm[1] < interval[1]+40*bar.diameter_mm-TOL):
            raise ValueError("Frozen original FE portion or its full40d was lost")
        if not contained(bar, material, host.side_cover_mm):
            raise ValueError("Changed bar is not contained in actual host with side cover")
        changes.append({"direction": str(bar.direction), "bar_id": bar.id,
            "before_interval_mm": previous.installed_interval_mm,
            "after_interval_mm": bar.installed_interval_mm,
            "frozen_obligation": obligations[_identity(bar)]})
    def inventory(bars):
        return Counter((b.steel_class, b.diameter_mm, round(b.installed_length_mm, 6)) for b in bars)
    if inventory(before) != inventory(after):
        raise ValueError("Complete steel/diameter/length/count cutting inventory changed")
    old_pairs, new_pairs = collision_pairs(before), collision_pairs(after)
    changed_pairs = [p for p in new_pairs if (p[0], p[1]) in changed or (p[0], p[2]) in changed]
    if new_pairs-old_pairs or changed_pairs:
        raise ValueError("New or changed-bar same-direction body collision")
    coverage = installed_fe_coverage(problem, after, sources)
    if coverage["status"] != "pass":
        raise ValueError("Complete original demand not covered by actual installed cores")
    schedule = build_bar_schedule(tuple(BarScheduleGroup(f"{b.direction}/{b.id}", b.diameter_mm,
        b.installed_length_mm, 1, b.steel_class) for b in after))
    stock = check_stock_cutting(schedule, time_limit_s=stock_time_limit_s)
    before_count = sum(not contained(b, material, host.side_cover_mm) for b in before)
    after_count = sum(not contained(b, material, host.side_cover_mm) for b in after)
    return {"schema_version": "fe-host-repair-check/v1", "policy": POLICY,
        "placement_eligible": False, "structural_placement_supported": False,
        "engineering_approval": False, "source_demand_removed": False,
        "source_demand_values_changed": False, "legacy_source_certificate_reused": False,
        "not_checked": list(NOT_CHECKED), "physical_bar_count": len(after),
        "additional_mass_kg": math.fsum(item.total_mass_kg for item in schedule),
        "cutting_inventory_unchanged": True, "position_count": len(schedule),
        "host_blocked_before": before_count, "host_blocked_after": after_count,
        "same_direction_body_pairs_before": len(old_pairs), "same_direction_body_pairs_after": len(new_pairs),
        "new_body_pairs": len(new_pairs-old_pairs), "changed_bars_with_body_collisions": len(changed_pairs),
        "source_coverage": coverage, "stock_cutting": stock, "changed_bar_count": len(changed), "changes": changes,
        "status": "blocked_host" if after_count else "blocked_body_collisions" if new_pairs
            else "blocked_stock" if stock["status"] != "pass" else "research_checks_passed_not_placement_approved"}
