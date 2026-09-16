"""Bounded deterministic post-trim lane repair, not another GA or global optimum."""
from __future__ import annotations

from dataclasses import replace
import math
import time

from shapely.ops import unary_union

from rebar.models import Axis
from ..contracts.physical import PhysicalBar
from ..services.cutting import PLATE_11700_CUT_LENGTHS_MM
from ..services.flat_trim_repair import (
    POLICY, check_flat_trim_repair, collision_keys, grown_collision_keys,
)
from ..services.opening_relocation import lane_map
from ..services.shaped_collisions import check_shaped_collisions
from ..services.shaped_geometry import check_shaped_host, shaped_mass_kg, straight_bar_from_physical
from ..services.trimmed_repair import batch_regions, demand_regions
from ..services.shaped_global_coverage import _strict_coverage
from ..services.tz_boundary_trim import geometry_presence_offers
from .trimmed_repair import _bar, _candidates, _parts, _separated


def _edge_positions(bar, sources, target, maximum_nudge):
    across = 1 if bar.direction.axis is Axis.X else 0
    original = bar.segments[0].start_mm[across]
    owners = [sources[bar.direction, key] for key in bar.source_bar_ids]
    values = {target.bounds[across]+lane.service_half_widths_mm[0] for lane in owners}
    values.update(target.bounds[across+2]-lane.service_half_widths_mm[1] for lane in owners)
    for q in sorted(values, key=lambda q: (abs(q-original), q)):
        if (q == original or abs(q-original) > maximum_nudge or
                any(not lane.axis_window_mm[0] <= q <= lane.axis_window_mm[1] for lane in owners) or
                any(abs(math.remainder(q-lane.source.background_origin_mm, 300)) < (
                    bar.diameter_mm+lane.source.background_diameter_mm)/2 for lane in owners)):
            continue
        yield q


def _templates(lanes, missing, host, elevations, profile):
    groups = {}
    for lane in lanes:
        groups.setdefault((lane.source.direction, lane.zone_id, lane.component_index,
                           lane.service_half_widths_mm), []).append(lane)
    result = []
    for key, members in groups.items():
        across = 1 if key[0].axis is Axis.X else 0
        targets = {members[0].source.transverse_axis_mm, members[-1].source.transverse_axis_mm}
        for (direction, _, _), geometry in missing.items():
            if direction == key[0] and not geometry.is_empty:
                targets.update((geometry.bounds[across], geometry.bounds[across+2]))
        selected = sorted({min(range(len(members)), key=lambda i: (
            abs(members[i].source.transverse_axis_mm-q), i)) for q in targets})
        for index in selected:
            source = members[index].source
            physical = PhysicalBar(source.id, source.direction, source.steel_class, source.diameter_mm,
                                   source.transverse_axis_mm, source.installed_interval_mm, (source.id,))
            result.append(straight_bar_from_physical(physical, axis_z_mm=elevations(
                host, source.direction, source.diameter_mm, profile)[0], placement_profile_id=profile.id))
    return tuple(result)


def repair_flat_trimmed_bars(before, lanes, problem, host, *, elevations, profile,
                            maximum_mass_increase_pct=5., maximum_axis_nudge_mm=.01,
                            maximum_candidates=30000, maximum_additions=40,
                            time_limit_s=60., stock_time_limit_s=10):
    """Add lowest local gain/mass-cost candidates, then exact FE-edge axis corrections.

Original trimmed bars are not removed or shortened. Added redundancy alone may
be deleted after full presence is obtained, preserving INITIAL anchored subsets.
All limits and exhaustion are recorded; failure leaves explicit remaining demand.
"""
    for value, lo, hi in ((maximum_mass_increase_pct, 0, 100), (maximum_axis_nudge_mm, 0, .01),
                          (time_limit_s, .001, 600)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError("Explicit finite flat repair budget required")
    if (type(maximum_candidates) is not int or not 1 <= maximum_candidates <= 100000
            or type(maximum_additions) is not int or not 0 <= maximum_additions <= 128):
        raise ValueError("Bounded integer candidate/addition budgets required")
    started = time.monotonic()
    sources, regions = lane_map(lanes), demand_regions(problem)
    offered = batch_regions(before, sources, regions)
    baseline_anchor = batch_regions(before, sources, regions, anchored=True)
    baseline_presence = offered
    missing = {key: value.difference(offered[key]) for key, value in regions.items()}
    maximum_mass = math.fsum(shaped_mass_kg(b) for b in before)*(1+maximum_mass_increase_pct/100)
    templates = _templates(lanes, missing, host, elevations, profile)
    pool, seen, generated, exhausted = [], set(), 0, False
    for candidate in _candidates(templates, sources, missing, host, 300., PLATE_11700_CUT_LENGTHS_MM):
        if generated >= maximum_candidates or time.monotonic()-started > time_limit_s*2/3:
            exhausted = True
            break
        generated += 1
        identity = candidate.direction, candidate.diameter_mm, candidate.segments, candidate.source_bar_ids
        if identity in seen:
            continue
        seen.add(identity)
        candidate = replace(candidate, id=f"flat-repair-{generated}")
        if any(b.direction == candidate.direction and b.id == candidate.id for b in before):
            raise ValueError("Reserved repair identity already exists")
        if check_shaped_host(candidate, host)["status"] != "pass" or not _separated(candidate, before):
            continue
        supply = batch_regions((candidate,), sources, regions)
        gain = math.fsum(supply[k].difference(offered[k]).area for k in regions)
        if gain > 0:
            pool.append((candidate, supply, gain/shaped_mass_kg(candidate)))
    current, added, actions = list(before), [], []
    mass = math.fsum(shaped_mass_kg(b) for b in before)
    for candidate, supply, rank in sorted(pool, key=lambda row: (-row[2], row[0].id)):
        if len(added) >= maximum_additions or time.monotonic()-started > time_limit_s:
            exhausted = True
            break
        gain = math.fsum(supply[k].difference(offered[k]).area for k in regions)
        if gain <= 0 or mass+shaped_mass_kg(candidate) > maximum_mass or not _separated(candidate, current):
            continue
        current.append(candidate)
        added.append((candidate, gain))
        mass += shaped_mass_kg(candidate)
        offered = {k: unary_union((offered[k], supply[k])) for k in regions}
        actions.append({"kind": "added", "direction": str(candidate.direction), "bar_id": candidate.id,
                        "gain_mm2": gain, "gain_per_kg": rank})
    # Derive a REAL transverse coordinate from deficit boundaries and original
    # half-widths. No rounding, snapping, epsilon buffering or checker relaxation.
    prior_pairs = check_shaped_collisions(tuple(current))
    for key, region in regions.items():
        for target in _parts(region.difference(offered[key])):
            for index, old in enumerate(tuple(current)):
                if old.direction != key[0] or old.diameter_mm < key[1]:
                    continue
                if time.monotonic()-started > time_limit_s:
                    exhausted = True
                    break
                along = 0 if old.direction.axis is Axis.X else 1
                for q in _edge_positions(old, sources, target, maximum_axis_nudge_mm):
                    if q == old.segments[0].start_mm[1-along]:
                        continue
                    trial = _bar(old, q, old.segments[0].start_mm[along], old.segments[0].end_mm[along], old.id)
                    if check_shaped_host(trial, host)["status"] != "pass":
                        continue
                    proposal = tuple(trial if i == index else b for i, b in enumerate(current))
                    presence = batch_regions(proposal, sources, regions)
                    anchor = batch_regions(proposal, sources, regions, anchored=True)
                    if (math.fsum(region.difference(presence[key]).area for key, region in regions.items()) >=
                            math.fsum(region.difference(offered[key]).area for key, region in regions.items())
                            or any(baseline_presence[k].difference(presence[k]).area > 0 or
                                   baseline_anchor[k].difference(anchor[k]).area > 0 for k in regions)):
                        continue
                    pairs = check_shaped_collisions(proposal)
                    if collision_keys(pairs)-collision_keys(prior_pairs) or grown_collision_keys(
                            tuple(current), proposal, prior_pairs, pairs):
                        continue
                    current, offered, prior_pairs = list(proposal), presence, pairs
                    actions.append({"kind": "modified", "direction": str(old.direction), "bar_id": old.id,
                        "original_coordinate_mm": old.segments[0].start_mm[1-along], "coordinate_mm": q,
                        "reason": "exact-deficit-FE-edge-minus-original-service-half-width"})
                    break
    removed = []
    if all(region.difference(offered[key]).area == 0 for key, region in regions.items()):
        for candidate, _ in sorted(added, key=lambda item: (item[1], item[0].id)):
            if time.monotonic()-started > time_limit_s:
                exhausted = True
                break
            proposal = tuple(b for b in current if (b.direction, b.id) != (candidate.direction, candidate.id))
            presence = batch_regions(proposal, sources, regions)
            anchor = batch_regions(proposal, sources, regions, anchored=True)
            if any(region.difference(presence[key]).area > 0 or baseline_anchor[key].difference(anchor[key]).area > 0
                   for key, region in regions.items()):
                continue
            if _strict_coverage(problem, geometry_presence_offers(proposal, sources))["status"] != "pass":
                continue
            current, offered = list(proposal), presence
            removed.append({"direction": str(candidate.direction), "bar_id": candidate.id})
    result = tuple(current)
    checks = check_flat_trim_repair(before, result, lanes, problem, host, elevations=elevations,
        profile=profile, maximum_mass_kg=maximum_mass, maximum_axis_nudge_mm=maximum_axis_nudge_mm,
        stock_time_limit_s=stock_time_limit_s)
    checks["search"] = {"policy_id": POLICY, "generated_count": generated, "candidate_count": len(pool),
        "maximum_candidates": maximum_candidates, "maximum_additions": maximum_additions,
        "time_limit_s": time_limit_s, "elapsed_s": time.monotonic()-started,
        "budget_exhausted": exhausted, "maximum_mass_increase_pct": maximum_mass_increase_pct,
        "actions": actions, "removed_redundant_additions": removed,
        "ranking": "descending-local-newly-covered-area-per-kg; deterministic-id-tie", "global_optimality_claimed": False}
    if not checks["accepted_nonregression"]:
        rejected = checks
        checks = check_flat_trim_repair(before, before, lanes, problem, host, elevations=elevations,
            profile=profile, maximum_mass_kg=maximum_mass, maximum_axis_nudge_mm=maximum_axis_nudge_mm,
            stock_time_limit_s=stock_time_limit_s)
        checks["search"] = rejected["search"]
        checks["rejected_proposal"] = rejected
        return before, checks
    return result, checks
