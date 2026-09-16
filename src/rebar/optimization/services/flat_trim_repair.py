"""Independent whole-batch proof for a finite, explicit flat post-trim repair.

No original FE, lane width, background phase or checker tolerance is modified.
Repeated lane ownership denotes separate physical bars, never summed weak As.
"""
from __future__ import annotations

import math
from shapely.geometry import box

from rebar.models import Axis
from .opening_relocation import lane_map
from .shaped_collisions import check_shaped_collisions
from .shaped_fe_repair import ACTUAL_CORE_SERVICE
from .shaped_geometry import shaped_batch_metrics, shaped_cutting_schedule
from .shaped_global_coverage import _offers, _strict_coverage
from .stock_cutting import check_stock_cutting
from .trimmed_repair import batch_regions, demand_regions, validate_rebuilt_bars
from .tz_boundary_trim import geometry_presence_offers

POLICY = "flat-post-trim-deficit-lane-additions/separate-control40d/v1"


def collision_keys(report):
    """Stable unordered physical identities, not just an equal pair count."""
    return {tuple(sorted((row[key]["direction"], row[key]["bar_id"]) for key in ("first", "second")))
            for row in (*report.get("proven_collision_pairs", ()), *report.get("uncertain_pairs", ()))}


def body_footprint(bar):
    along = 0 if bar.direction.axis is Axis.X else 1
    a, b, radius = bar.segments[0].start_mm, bar.segments[0].end_mm, bar.diameter_mm/2
    return box(a[0]-(radius if along else 0), a[1]-(0 if along else radius),
               b[0]+(radius if along else 0), b[1]+(0 if along else radius))


def grown_collision_keys(before, after, prior_pairs, pairs):
    old = {(str(b.direction), b.id): b for b in before}
    final = {(str(b.direction), b.id): b for b in after}
    return {key for key in collision_keys(pairs) & collision_keys(prior_pairs)
            if body_footprint(final[key[0]]).intersection(body_footprint(final[key[1]])).difference(
                body_footprint(old[key[0]]).intersection(body_footprint(old[key[1]]))).area > 0}


def check_flat_trim_repair(before, after, lanes, problem, host, *, elevations, profile,
                          maximum_mass_kg, maximum_axis_nudge_mm, stock_time_limit_s=10):
    """Re-read all geometry and original demand; no caller-supplied pass flags."""
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (maximum_mass_kg, maximum_axis_nudge_mm, stock_time_limit_s)):
        raise ValueError("Finite explicit flat repair limits required")
    if maximum_mass_kg <= 0 or not 0 <= maximum_axis_nudge_mm <= .01 or not 0 < stock_time_limit_s <= 60:
        raise ValueError("Bounded flat repair limits required")
    sources, regions = lane_map(lanes), demand_regions(problem)
    if validate_rebuilt_bars(before, sources, host):
        raise ValueError("Repair baseline must be completely host-contained")
    failures = validate_rebuilt_bars(after, sources, host)
    old = {(b.direction, b.id): b for b in before}
    if not set(old) <= {(b.direction, b.id) for b in after}:
        raise ValueError("This repair cannot remove baseline bars")
    for bar in after:
        along = 0 if bar.direction.axis is Axis.X else 1
        line = bar.segments[0]
        if line.start_mm[2] != elevations(host, bar.direction, bar.diameter_mm, profile)[0]:
            raise ValueError("Bar differs from the explicitly declared flat layer")
        previous = old.get((bar.direction, bar.id))
        if previous is None and not 100 <= line.end_mm[along]-line.start_mm[along] <= 11700:
            raise ValueError("New bar length violates explicit repair policy")
        if previous is not None and bar != previous:
            original = previous.segments[0]
            if (bar.diameter_mm != previous.diameter_mm or bar.steel_class != previous.steel_class
                    or bar.source_bar_ids != previous.source_bar_ids
                    or line.start_mm[along] != original.start_mm[along]
                    or line.end_mm[along] != original.end_mm[along]
                    or abs(line.start_mm[1-along]-original.start_mm[1-along]) > maximum_axis_nudge_mm):
                raise ValueError("Existing bars permit bounded axis-only operations")
        if previous != bar:
            for owner in bar.source_bar_ids:
                source = sources[bar.direction, owner].source
                if bar.diameter_mm != source.diameter_mm:
                    raise ValueError("Repair does not permit diameter substitution")
                if abs(math.remainder(line.start_mm[1-along]-source.background_origin_mm, 300)) < (
                        bar.diameter_mm+source.background_diameter_mm)/2:
                    raise ValueError("Repair intersects the source-prescribed background")
    losses = {}
    for label, anchored in (("geometric_presence", False), ("control_40d", True)):
        prior = batch_regions(before, sources, regions, anchored=anchored)
        final = batch_regions(after, sources, regions, anchored=anchored)
        losses[label] = math.fsum(prior[k].difference(final[k]).area for k in regions)
    prior_pairs, pairs = check_shaped_collisions(before), check_shaped_collisions(after)
    new_pairs = collision_keys(pairs)-collision_keys(prior_pairs)
    grown_pairs = grown_collision_keys(before, after, prior_pairs, pairs)
    presence = _strict_coverage(problem, geometry_presence_offers(after, sources))
    anchor = _strict_coverage(problem, _offers(after, sources, longitudinal_service_policy=ACTUAL_CORE_SERVICE))
    metrics = shaped_batch_metrics(after)
    accepted = not failures and not any(losses.values()) and not new_pairs and not grown_pairs and metrics["mass_kg"] <= maximum_mass_kg
    stock = check_stock_cutting(shaped_cutting_schedule(after), time_limit_s=stock_time_limit_s)
    return {"schema_version": "flat-trim-repair-check/v1", "policy_id": POLICY,
        "accepted_nonregression": accepted, "physical_metrics_before": shaped_batch_metrics(before),
        "physical_metrics": metrics, "geometric_presence": presence, "coverage_with_control_40d": anchor,
        "material_boundary_failures_after": len(failures), "host_failures": failures,
        "previously_covered_area_lost_mm2": losses, "new_collision_pair_count": len(new_pairs),
        "grown_collision_pair_count": len(grown_pairs),
        "collisions": pairs, "stock_cutting": stock, "maximum_mass_kg": maximum_mass_kg,
        "maximum_axis_nudge_mm": maximum_axis_nudge_mm, "diameter_increase_permitted": False,
        "minimum_new_length_mm": 100, "maximum_new_length_mm": 11700,
        "original_FE_geometry_changed": False, "source_demand_removed": False,
        "weak_As_summation": False, "actual_Revit_checked": False,
        "placement_eligible": False, "engineering_approval": False}
