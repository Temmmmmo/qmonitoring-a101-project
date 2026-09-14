"""Full ORIGINAL FE union coverage with replaceable, not frozen, service owners.

This opt-in policy does not transfer/average demand. Another individually
sufficient source offer may cover a region previously served by a different
bar. Original finite source windows/recipes remain fixed; weak offers never sum.
U anchorage remains the separate, explicit geometric-node research assumption.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import math

from shapely.geometry import Polygon
from shapely.ops import unary_union

from rebar.models import Axis, Direction
from ..contracts.plate import PLATE_DIRECTIONS, PlateProblem
from ..contracts.shaped_physical import K09_U_RETURN_50D_PROFILE, ShapedPhysicalBar
from .collision_replacement import freeze_owner_fe_service
from .fe_transverse_repair import _baseline
from .opening_relocation import coverage_from_offers, lane_map
from .shaped_collisions import check_shaped_collisions
from .shaped_fe_repair import (
    ResearchLayerProfile, exterior_edge_choices, layer_elevations,
    owner_fragments_preserved, shaped_service_offers,
)
from .shaped_geometry import (
    build_u_edge_bar, check_edge_anchor_geometry, check_shaped_host, curve_point,
    shaped_batch_metrics, shaped_cut_length_mm, shaped_cutting_schedule,
    straight_bar_from_physical,
)
from .stock_cutting import check_stock_cutting

POLICY = "global-original-FE-union-finite-source-lanes-qshift-exterior-U/research-v1"
_COLLISION_OPTIONS = frozenset((
    "maximum_chord_error_mm", "maximum_refinements", "maximum_bars",
    "maximum_candidate_pairs", "maximum_chord_pair_checks", "maximum_chords_per_bar",
))


@dataclass(frozen=True)
class NecessarySourceRegion:
    direction: Direction
    cell_id: int
    level_index: int
    required_diameter_mm: int
    required_nominal_step_mm: int
    geometry: object


def _problem(problem):
    if (not isinstance(problem, PlateProblem) or len(problem.direction_problems) != 4
            or {p.demand.direction for p in problem.direction_problems} != set(PLATE_DIRECTIONS)
            or sum(len(p.demand.cells) for p in problem.direction_problems) > 10000
            or sum(len(c.poly) for p in problem.direction_problems for c in p.demand.cells) > 100000):
        raise ValueError("Bounded complete ORIGINAL four-direction FE problem required")
    # Shared validation inspects even background FE polygons, all IDs and levels.
    coverage_from_offers(problem, {}, policy=POLICY)


def _shape_batch(bars, sources):
    if not isinstance(bars, tuple) or not 1 <= len(bars) <= 5000:
        raise ValueError("Complete bounded immutable shaped batch required")
    identities, owners = set(), Counter()
    for bar in bars:
        shaped_cut_length_mm(bar)
        key = (bar.direction, bar.id)
        if key in identities:
            raise ValueError("Unique shaped physical identities required")
        identities.add(key)
        q = bar.segments[0].start_mm[1 if bar.direction.axis is Axis.X else 0]
        for owner in bar.source_bar_ids:
            lane = sources.get((bar.direction, owner))
            if lane is None:
                raise ValueError("Unknown original source owner")
            if (bar.steel_class != lane.source.steel_class or bar.diameter_mm < lane.source.diameter_mm
                    or not lane.axis_window_mm[0] <= q <= lane.axis_window_mm[1]):
                raise ValueError("Original source material/diameter/finite lane window violated")
            owners[(bar.direction, owner)] += 1
    if set(owners) != set(sources) or any(n != 1 for n in owners.values()):
        raise ValueError("Every original owner label must remain exactly once in the physical batch")


def _offers(bars, sources):
    return {direction: [offer for bar in bars if bar.direction == direction
                        for offer in shaped_service_offers(bar, sources)] for direction in PLATE_DIRECTIONS}


def _strict_coverage(problem, offers):
    report = coverage_from_offers(problem, offers, policy=POLICY)
    legacy = report["status"]
    missing = 0
    for direction in report["directions"]:
        for cell in direction["cells"]:
            cell["covered"] = cell["uncovered_area_mm2"] == 0
        direction["uncovered_cell_count"] = sum(not c["covered"] for c in direction["cells"])
        missing += direction["uncovered_cell_count"]
    report.update(status="pass" if missing == 0 else "fail", uncovered_cell_count=missing,
                  shared_tolerance_coverage_status=legacy, positive_area_loss_tolerance_mm2=0,
                  demand_transfer_used=False, original_FE_geometry_changed=False,
                  weak_offer_As_summation=False)
    return report


def _budget(value):
    if type(value) is not int or not 1 <= value <= 1000000:
        raise ValueError("Bounded positive overlay-operation budget required")
    return value


def necessary_source_regions(
    current_bars: tuple[ShapedPhysicalBar, ...], lanes, problem: PlateProblem, *,
    bar_key: tuple[Direction, str], maximum_overlay_operations: int = 100000,
) -> tuple[NecessarySourceRegion, ...]:
    """Original demanded FE regions minus ALL other sufficient current offers.

    The result is a necessary service obligation for this one-bar substitution,
    not the old owner's footprint, not a bbox and not an engineering approval.
    Even currently uncovered original demand is retained in this obligation.
    """
    _problem(problem)
    if not isinstance(lanes, tuple) or not 1 <= len(lanes) <= 5000:
        raise ValueError("Bounded immutable original service lanes required")
    sources = lane_map(lanes)
    _shape_batch(current_bars, sources)
    budget = _budget(maximum_overlay_operations)
    if (not isinstance(bar_key, tuple) or len(bar_key) != 2
            or bar_key not in {(b.direction, b.id) for b in current_bars}):
        raise ValueError("Target must identify exactly one current physical bar")
    demand = problem.problem(bar_key[0]).demand
    offered = [offer for bar in current_bars if bar.direction == bar_key[0]
               and (bar.direction, bar.id) != bar_key for offer in shaped_service_offers(bar, sources)]
    remaining, unions, operations = [], {}, 0
    for cell in demand.cells:
        recipe = demand.level(cell.level_index).recipe
        if not recipe.additions:
            continue
        spec = recipe.additions[0]
        if cell.level_index not in unions:
            operations += len(offered)
            if operations > budget:
                raise ValueError("Necessary-region overlay budget exceeded; no truncated obligation")
            unions[cell.level_index] = unary_union([p for d, s, p in offered
                if d >= spec.diameter and s <= spec.step])
        operations += 1
        if operations > budget:
            raise ValueError("Necessary-region overlay budget exceeded; no truncated obligation")
        needed = Polygon(cell.poly).difference(unions[cell.level_index])
        if needed.area > 0:
            remaining.append(NecessarySourceRegion(demand.direction, cell.id, cell.level_index,
                spec.diameter, spec.step, needed))
    return tuple(remaining)


def necessary_regions_covered(candidate, regions, sources) -> bool:
    """Fast proposal filter; final checker independently re-reads the ORIGINAL FE."""
    shaped_cut_length_mm(candidate)
    if not isinstance(regions, tuple) or len(regions) > 10000:
        raise ValueError("Bounded immutable necessary regions required")
    offered = shaped_service_offers(candidate, sources)
    unions = {}
    for region in regions:
        if (not isinstance(region, NecessarySourceRegion) or region.direction != candidate.direction
                or type(region.cell_id) is not int or type(region.level_index) is not int
                or type(region.required_diameter_mm) is not int or region.required_diameter_mm <= 0
                or type(region.required_nominal_step_mm) is not int or region.required_nominal_step_mm <= 0
                or not hasattr(region.geometry, "area") or not region.geometry.is_valid
                or region.geometry.is_empty or region.geometry.area <= 0):
            raise ValueError("Valid same-direction typed necessary FE regions required")
        key = region.required_diameter_mm, region.required_nominal_step_mm
        if key not in unions:
            unions[key] = unary_union([p for d, s, p in offered if d >= key[0] and s <= key[1]])
        if region.geometry.difference(unions[key]).area > 0:
            return False
    return True


def _pair_key(pair):
    return tuple(sorted((value["direction"], value["bar_id"]) for value in (pair["first"], pair["second"])))


def check_shaped_global_repair(
    before, after, lanes, problem, host, *, layer_profile=ResearchLayerProfile(),
    maximum_shift_mm: float = 300., stock_time_limit_s: float = 30,
    collision_options: dict | None = None, maximum_longitudinal_shift_mm: float = 0.,
    allow_length_reassignment: bool = False,
) -> dict:
    """Independent final proof: global source union, canonical geometry and full 3D.

    Retained original host/collision failures remain blocked in the research
    report. Every changed bar must be host-contained and free of both proven and
    uncertain inter-bar conflicts, even if its identity had an old conflict.
    """
    if type(allow_length_reassignment) is not bool:
        raise ValueError("Explicit boolean length-reassignment policy required")
    for label, value, lo, hi in (("maximum_shift_mm", maximum_shift_mm, 0, 300),
                                ("maximum_longitudinal_shift_mm", maximum_longitudinal_shift_mm, 0, 11700),
                                ("stock_time_limit_s", stock_time_limit_s, .001, 60)):
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not lo <= value <= hi):
            raise ValueError(f"{label} must be finite and within {lo}..{hi}")
    if collision_options is None:
        collision_options = {}
    if not isinstance(collision_options, dict) or not set(collision_options) <= _COLLISION_OPTIONS:
        raise ValueError("Only bounded collision resource settings may be passed; no cached proof or skip")
    _problem(problem)
    sources, original_coverage = _baseline(before, lanes, problem)
    _shape_batch(after, sources)
    old = {(bar.direction, bar.id): bar for bar in before}
    old_inventory = Counter((str(b.direction), b.steel_class, b.diameter_mm, round(b.installed_length_mm, 6)) for b in before)
    if len(after) != len(before) or {(b.direction, b.id) for b in after} != set(old):
        raise ValueError("Same complete unique physical inventory required")
    baseline, failures_before, failures_after, changed, substitutions = [], [], [], set(), []
    moves, minimum_background_gap = [], math.inf
    for bar in after:
        previous = old[bar.direction, bar.id]
        target_length = bar.selected_cut_length_mm
        if (bar.diameter_mm != previous.diameter_mm or bar.steel_class != previous.steel_class
                or bar.source_bar_ids != previous.source_bar_ids
                or not allow_length_reassignment and abs(shaped_cut_length_mm(bar)-previous.installed_length_mm) > 1e-7):
            raise ValueError("Material/diameter/owner labels/true cut length must remain fixed")
        if (str(bar.direction), bar.steel_class, bar.diameter_mm, round(target_length, 6)) not in old_inventory:
            raise ValueError("Selected cut length has no original stock in the same direction/material/diameter")
        along, across = (0, 1) if bar.direction.axis is Axis.X else (1, 0)
        q = bar.segments[0].start_mm[across]
        if abs(q-previous.transverse_axis_mm) > maximum_shift_mm:
            raise ValueError("Transverse shift exceeds original-baseline bound")
        for owner in previous.source_bar_ids:
            source = sources[bar.direction, owner].source
            gap = abs(math.remainder(q-source.background_origin_mm, source.background_step_mm)) - (
                bar.diameter_mm+source.background_diameter_mm)/2
            minimum_background_gap = min(minimum_background_gap, gap)
            if gap < 0:
                raise ValueError("Corrected q penetrates a source-prescribed background axis")
        zm, zr = layer_elevations(host, bar.direction, bar.diameter_mm, layer_profile)
        original_shape = straight_bar_from_physical(previous, axis_z_mm=zm,
                                                    placement_profile_id=layer_profile.id)
        baseline.append(original_shape)
        identity = (str(bar.direction), bar.id)
        if not check_shaped_host(original_shape, host)["whole_body_with_cover_contained"]:
            failures_before.append(identity)
        if bar.shape_kind == "straight":
            shift = bar.segments[0].start_mm[along]-previous.installed_interval_mm[0]
            if abs(shift) > maximum_longitudinal_shift_mm:
                raise ValueError("Straight longitudinal translation exceeds its explicit source-baseline bound")
            start = bar.segments[0].start_mm[along]
            interval = ((start, start+target_length) if allow_length_reassignment
                        else tuple(value+shift for value in previous.installed_interval_mm))
            rebuilt = straight_bar_from_physical(replace(previous, transverse_axis_mm=q,
                installed_interval_mm=interval), axis_z_mm=zm,
                                                 placement_profile_id=layer_profile.id)
            if bar != rebuilt:
                raise ValueError("Straight change must preserve its exact interval and declared layer profile")
        elif bar.shape_kind == "U":
            inward = 1 if bar.segments[0].start_mm[along] > bar.segments[0].end_mm[along] else -1
            arc_tip = curve_point(bar.segments[1], 1)
            recovered_edge = arc_tip[along]-inward*(host.side_cover_mm+bar.diameter_mm/2)
            matched = False
            for edge, sign in exterior_edge_choices(host, bar.direction, q, bar.diameter_mm):
                if sign != inward or abs(edge-recovered_edge) > 1e-7:
                    continue
                result = build_u_edge_bar(bar_id=bar.id, direction=bar.direction,
                    steel_class=previous.steel_class, diameter_mm=previous.diameter_mm,
                    transverse_axis_mm=q, edge_coordinate_mm=edge, inward_sign=inward,
                    main_axis_z_mm=zm, return_axis_z_mm=zr,
                    slab_thickness_mm=host.sections[-1].top_z_mm-host.sections[0].bottom_z_mm,
                    side_cover_mm=host.side_cover_mm, cut_length_mm=(target_length if allow_length_reassignment
                                                                 else previous.installed_length_mm),
                    source_bar_ids=previous.source_bar_ids, placement_profile_id=layer_profile.id)
                if result.status == "geometry_conditions_met" and result.bar == bar:
                    matched = True
                    break
            if not matched or not check_edge_anchor_geometry(bar,
                    slab_thickness_mm=host.sections[-1].top_z_mm-host.sections[0].bottom_z_mm)["geometry_conditions_met"]:
                raise ValueError("U is not the canonical sourced form on an actual exterior edge")
            substitutions.append(identity)
        else:
            raise ValueError("Only straight and sourced exterior-U forms are allowed")
        is_changed = bar != original_shape
        if is_changed:
            changed.add(identity)
            moves.append({"direction": identity[0], "bar_id": bar.id, "shape_kind": bar.shape_kind,
                          "original_q_mm": previous.transverse_axis_mm, "after_q_mm": q,
                          "q_shift_mm": q-previous.transverse_axis_mm,
                          "original_cut_length_mm": previous.installed_length_mm,
                          "selected_cut_length_mm": target_length,
                          "longitudinal_start_shift_mm": (bar.segments[0].start_mm[along]
                              -previous.installed_interval_mm[0] if bar.shape_kind == "straight" else None)})
        if not check_shaped_host(bar, host)["whole_body_with_cover_contained"]:
            failures_after.append(identity)
            if is_changed:
                raise ValueError("Every changed bar must pass complete host body+cover proof")
    baseline = tuple(baseline)
    coverage = _strict_coverage(problem, _offers(after, sources))
    if coverage["status"] != "pass":
        raise ValueError("Complete original FE positive-area demand is not covered by sufficient offers")
    baseline_coverage = _strict_coverage(problem, _offers(baseline, sources))
    before_pairs = check_shaped_collisions(baseline, **collision_options)
    after_pairs = check_shaped_collisions(after, **collision_options)
    for category in ("proven_collision_pairs", "uncertain_pairs"):
        old_pairs = {_pair_key(pair) for pair in before_pairs[category]}
        new_pairs = {_pair_key(pair) for pair in after_pairs[category]}
        if new_pairs-old_pairs:
            raise ValueError("New proven or uncertain 3D body pair introduced")
        if any(changed.intersection(pair) for pair in new_pairs):
            raise ValueError("A changed bar participates in a proven or uncertain 3D body pair")
    # Diagnostic only: these old-owner fragments are deliberately NOT a gate.
    # The independent global source check above prevents another owner from
    # covering them with weaker reinforcement or silently deleting the demand.
    frozen = freeze_owner_fe_service(before, lanes, problem)
    reassigned = [(str(bar.direction), bar.id) for bar in after
                  if not owner_fragments_preserved(bar, frozen, sources)]
    stock = check_stock_cutting(shaped_cutting_schedule(after), time_limit_s=stock_time_limit_s)
    metrics = shaped_batch_metrics(after)
    new_inventory = Counter((str(b.direction), b.steel_class, b.diameter_mm, round(shaped_cut_length_mm(b), 6)) for b in after)
    if old_inventory != new_inventory:
        raise ValueError("Complete physical stock inventory changed")
    blocked_pairs = after_pairs["proven_collision_pair_count"]+after_pairs["uncertain_pair_count"]
    return {
        "schema_version": "shaped-global-original-FE-repair-check/v1", "policy": POLICY,
        "maximum_longitudinal_shift_mm": maximum_longitudinal_shift_mm,
        "length_reassignment_allowed": allow_length_reassignment,
        "per_bar_cut_lengths_preserved": all(abs(shaped_cut_length_mm(b)-old[b.direction, b.id].installed_length_mm)
                                              <= 1e-7 for b in after),
        "status": "blocked_host" if failures_after else "blocked_body_collisions" if blocked_pairs
        else "blocked_stock" if stock["status"] != "pass" else "research_checks_passed_not_placement_approved",
        "source_coverage": coverage, "source_coverage_before_strict": baseline_coverage,
        "legacy_baseline_coverage": original_coverage,
        "source_demand_removed": False, "original_source_values_changed": False,
        "original_FE_geometry_changed": False, "demand_transfer_used": False,
        "weak_offer_As_summation": False, "per_owner_FE_retention_required": False,
        "per_owner_FE_certificate_reused": False,
        "bars_losing_some_old_owner_FE_service": reassigned,
        "old_owner_service_loss_bar_count": len(reassigned),
        "physical_stock_inventory_preserved": True, "physical_metrics": metrics,
        "stock_cutting": stock, "changed_bar_count": len(changed), "changes": moves,
        "U_substitution_count": len(substitutions), "U_substitution_ids": substitutions,
        "shaped_host_not_proven_before": len(failures_before), "shaped_host_not_proven_after": len(failures_after),
        "host_failure_ids": failures_after, "host_failure_counts": dict(Counter(d for d, _ in failures_after)),
        "3d_collisions_before": before_pairs, "3d_collisions_after": after_pairs,
        "new_proven_or_uncertain_3d_pairs": 0, "changed_bar_3d_conflict_count": 0,
        "source_prescribed_background_plan_gap_status": "pass",
        "minimum_source_prescribed_background_gap_mm": minimum_background_gap,
        "old_background_contact_retention_required": False,
        "original_STO_phase_certificate": "not_reused_not_claimed",
        "all_U_geometries_rebuilt_independently": True,
        "arc_bridge_and_return_demand_credit": False,
        "bent_anchorage_profile": K09_U_RETURN_50D_PROFILE.id,
        "bent_end_anchorage_capacity": "not_checked", "placement_eligible": False,
        "engineering_approval": False, "production_ready": False,
        "not_checked": ["normative_bent_anchorage_resistance_and_compression_zone",
            "existing_background_and_Revit_reinforcement_inventory", "approved_XY_order",
            "regrouped_rectangular_zone_and_STO_phase_certificates", "Revit_readback"],
    }
