"""Opt-in two-tier height hypothesis, independently checked but never approved.

Only top-X / bottom-Y straight bars may use baseline Z+16 mm. Original XY,
owners, diameter, true cut lengths and every U curve remain unchanged. This is
not the original rigid-placement certificate and does change effective depth.
"""
from dataclasses import replace
import math

from ..contracts.shaped_physical import Line3D
from .opening_relocation import lane_map
from .shaped_collisions import check_shaped_collisions
from .shaped_fe_repair import ACTUAL_CORE_SERVICE, ResearchLayerProfile, layer_elevations, shaped_service_offers
from .shaped_geometry import (
    check_edge_anchor_geometry, check_shaped_host, shaped_batch_metrics, shaped_cut_length_mm, shaped_cutting_schedule,
)
from .shaped_global_coverage import _offers, _problem, _shape_batch, _strict_coverage
from .stock_cutting import check_stock_cutting
from .tz_outer_scope import check_tz_outer_bar

PROFILE_ID = "top-X-plus16-bottom-Y-plus16-two-tier/research-hypothesis-2026-09-15"
ELIGIBLE_DIRECTIONS = frozenset(("top-X", "bottom-Y"))
MAXIMUM_CHANGES = 64


def bar_key(bar):
    return str(bar.direction), bar.id


def pair_keys(report, category):
    return {tuple(sorted((p["direction"], p["bar_id"]) for p in (row["first"], row["second"])))
            for row in report[category]}


def make_second_tier(bar):
    """Build only; this helper does not certify a whole party or its capacity."""
    shaped_cut_length_mm(bar)
    if (str(bar.direction) not in ELIGIBLE_DIRECTIONS or bar.shape_kind != "straight"
            or len(bar.segments) != 1 or bar.placement_profile_id != ResearchLayerProfile().id):
        raise ValueError("An eligible original-profile straight bar is required for the second tier")
    line = bar.segments[0]
    a, b = list(line.start_mm), list(line.end_mm)
    a[2] += 16.
    b[2] += 16.
    return replace(bar, segments=(Line3D(tuple(a), tuple(b)),), placement_profile_id=PROFILE_ID)


def validate_baseline(before, lanes, problem, actual_host):
    _problem(problem)
    sources = lane_map(lanes)
    _shape_batch(before, sources)
    for bar in before:
        zm, zr = layer_elevations(actual_host, bar.direction, bar.diameter_mm, ResearchLayerProfile())
        if (bar.placement_profile_id != ResearchLayerProfile().id
                or bar.segments[0].start_mm[2] != zm):
            raise ValueError("Original single-tier baseline profile/Z required; no repeated promotion")
        if bar.shape_kind == "U" and (bar.segments[-1].start_mm[2] != zr
                or not check_edge_anchor_geometry(bar, slab_thickness_mm=(actual_host.sections[-1].top_z_mm
                    - actual_host.sections[0].bottom_z_mm))["geometry_conditions_met"]):
            raise ValueError("Original U geometry and prescribed return tier must remain valid")
    coverage = _strict_coverage(problem, _offers(before, sources, longitudinal_service_policy=ACTUAL_CORE_SERVICE))
    if coverage["status"] != "pass":
        raise ValueError("Complete original FE must pass before the Z hypothesis")
    return sources


def outside_ids(bars, actual_host, *, actual=False):
    checker = check_shaped_host if actual else check_tz_outer_bar
    return [bar_key(bar) for bar in bars if checker(bar, actual_host)["status"] != "pass"]


def check_xy_inventory(before, after, sources):
    _shape_batch(after, sources)
    old = {bar_key(bar): bar for bar in before}
    if len(after) != len(before) or {bar_key(b) for b in after} != set(old):
        raise ValueError("The complete original physical identities must remain unchanged")
    changes, assignments = [], []
    for new in after:
        previous = old[bar_key(new)]
        changed = new != previous
        if changed:
            if new != make_second_tier(previous):
                raise ValueError("Only exact +16 Z with the explicit new tier profile is permitted")
            changes.append({"direction": bar_key(new)[0], "bar_id": new.id, "diameter_mm": new.diameter_mm,
                "z_before_mm": previous.segments[0].start_mm[2], "z_after_mm": new.segments[0].start_mm[2],
                "z_offset_mm": 16.})
        assignments.append({"direction": bar_key(new)[0], "bar_id": new.id, "tier": int(changed),
            "main_z_mm": new.segments[0].start_mm[2], "placement_profile_id": new.placement_profile_id})
        original = shaped_service_offers(previous, sources, longitudinal_service_policy=ACTUAL_CORE_SERVICE)
        current = shaped_service_offers(new, sources, longitudinal_service_policy=ACTUAL_CORE_SERVICE)
        if len(original) != len(current) or any(a[:2] != b[:2] or not a[2].equals_exact(b[2], 0)
                for a, b in zip(original, current)):
            raise ValueError("Every original sufficient FE service offer must remain exactly unchanged")
    if len(changes) > MAXIMUM_CHANGES:
        raise ValueError("The bounded two-tier profile permits at most 64 changed bars")
    if shaped_batch_metrics(before) != shaped_batch_metrics(after):
        raise ValueError("Physical quantities, material, true lengths, positions or mass changed")
    return changes, assignments


def main_branch_order(bars):
    result = []
    for face in ("top", "bottom"):
        xs = [b for b in bars if str(b.direction) == face+"-X"]
        ys = [b for b in bars if str(b.direction) == face+"-Y"]
        if not xs or not ys:
            result.append({"face": face, "status": "not_applicable_missing_direction_bars"})
            continue
        above, below = (xs, ys) if face == "top" else (ys, xs)
        gap = min(b.segments[0].start_mm[2]-b.diameter_mm/2 for b in above) - max(
            b.segments[0].start_mm[2]+b.diameter_mm/2 for b in below)
        if gap < 0:
            raise ValueError("The main-branch X-outer/Y-inner order is not preserved")
        result.append({"face": face, "status": "pass", "X_closer_to_face_than_Y": True,
            "minimum_main_body_vertical_clearance_mm": gap})
    return result


def check_tz_collision_tiers(before, after, lanes, problem, actual_host, *, stock_time_limit_s=30):
    """Fresh complete-party proof for THIS Z policy, not the old rigid checker.

    Retained exterior failures and old untouched conflicts remain visible. No
    new exterior failure ID or proven/uncertain pair is permitted. A changed bar
    cannot retain a conflict. True Revit cover regressions are informational in
    the user-selected TZ scope, explicitly enumerated, never hidden.
    """
    if (isinstance(stock_time_limit_s, bool) or not isinstance(stock_time_limit_s, (int, float))
            or not math.isfinite(stock_time_limit_s) or not 0 < stock_time_limit_s <= 60):
        raise ValueError("A finite bounded stock verification time is required")
    sources = validate_baseline(before, lanes, problem, actual_host)
    changes, assignments = check_xy_inventory(before, after, sources)
    changed = {(row["direction"], row["bar_id"]) for row in changes}
    order = main_branch_order(after)
    outer_before, outer_after = outside_ids(before, actual_host), outside_ids(after, actual_host)
    if set(outer_after)-set(outer_before):
        raise ValueError("Z tier introduced a new external-body failure")
    pairs_before, pairs_after = check_shaped_collisions(before), check_shaped_collisions(after)
    for category in ("proven_collision_pairs", "uncertain_pairs"):
        old_pairs, new_pairs = pair_keys(pairs_before, category), pair_keys(pairs_after, category)
        if new_pairs-old_pairs:
            raise ValueError("Z tier introduced a new proven or uncertain 3D pair")
        if any(changed.intersection(pair) for pair in new_pairs):
            raise ValueError("A changed bar still participates in a proven or uncertain 3D pair")
    coverage = _strict_coverage(problem, _offers(after, sources, longitudinal_service_policy=ACTUAL_CORE_SERVICE))
    if coverage["status"] != "pass":
        raise ValueError("The whole unchanged original FE field is not covered")
    minimum_gap = math.inf
    for bar in after:
        across = 1 if str(bar.direction).endswith("X") else 0
        for owner in bar.source_bar_ids:
            source = sources[bar.direction, owner].source
            gap = abs(math.remainder(bar.segments[0].start_mm[across]-source.background_origin_mm,
                source.background_step_mm))-(bar.diameter_mm+source.background_diameter_mm)/2
            minimum_gap = min(minimum_gap, gap)
    if minimum_gap < 0:
        raise ValueError("Source-prescribed background plan-axis gap is negative")
    stock = check_stock_cutting(shaped_cutting_schedule(after), time_limit_s=stock_time_limit_s)
    actual_before, actual_after = outside_ids(before, actual_host, actual=True), outside_ids(after, actual_host, actual=True)
    blockers = (["external_boundary"] if outer_after else [])
    if pairs_after["proven_collision_pair_count"] or pairs_after["uncertain_pair_count"]:
        blockers.append("remaining_additional_3D_conflicts")
    if stock["status"] != "pass":
        blockers.append("11700_cutting")
    return {"schema_version": "tz-collision-two-tier-check/v1", "profile_id": PROFILE_ID,
        "status": "blocked_remaining_TZ_checks" if blockers else "research_checks_passed_not_approved",
        "tz_blockers": blockers, "units": "mm", "changes": changes, "changed_bar_count": len(changes),
        "whole_plate_tier_assignment": assignments,
        "tiers": {"maximum_tiers": 2, "offsets_mm": [0, 16], "eligible_directions": sorted(ELIGIBLE_DIRECTIONS)},
        "main_branch_order": order, "source_coverage": coverage,
        "each_original_FE_service_offer_exactly_unchanged": True,
        "source_prescribed_background_XY_gap_unchanged": True,
        "minimum_source_prescribed_background_gap_mm": minimum_gap,
        "collisions_before": pairs_before, "collisions_after": pairs_after,
        "new_proven_or_uncertain_pairs": [], "changed_bar_3D_conflicts": 0,
        "external_boundary_failures_before": len(outer_before), "external_boundary_failures_after": len(outer_after),
        "external_boundary_failure_ids_after": outer_after, "new_external_failure_ids": [],
        "actual_host_informational_before": actual_before, "actual_host_informational_after": actual_after,
        "new_actual_host_informational_failure_ids": sorted(set(actual_after)-set(actual_before)),
        "actual_host_regression_is_not_hidden_or_placement_approved": True,
        "physical_metrics": shaped_batch_metrics(after), "stock_cutting": stock,
        "U_shapes_arcs_and_returns_unchanged_and_in_full_3D_check": True,
        "XY_lengths_diameters_owners_unchanged": True, "source_FE_geometry_or_values_changed": False,
        "rigid_Z_preserving_checker_reused_for_new_Z": False,
        "new_engineering_assumption": "Second straight-bar tier changes effective depth; top-X and bottom-Y only, +16 mm.",
        "excluded_from_TZ": ["closed_openings", "concrete_cover"],
        "not_checked": ["effective_depth_and_capacity", "approved_working_Revit_Z_profile",
            "actual_background_3D_inventory", "bent_anchorage_capacity", "Revit_readback"],
        "placement_eligible": False, "engineering_approval": False, "production_ready": False,
        "structural_placement_supported": False, "effective_depth_and_capacity_checked": False}
