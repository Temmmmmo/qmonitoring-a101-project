"""Joint, inventory-preserving translations of complete parametric STO zones.

This finite greedy search does not crop the original FE demand or normalize /
merge source bars. Host checks use the common material of ALL Solid sections;
collision checks assume coplanar additional bars within each direction, not an
approved height profile. Even a completely clear result is review-only.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
from time import perf_counter

from shapely.geometry import box

from rebar.models import Axis
from rebar.optimization.contracts import PlateProblem
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.contracts.placement import CompositeLayoutZone
from rebar.optimization.contracts.plate import _canonical_direction_items
from rebar.optimization.services.bar_schedule import build_bar_schedule, composite_schedule_groups
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_host_fit import fit_composite_zone_to_solid_host
from rebar.optimization.services.composite_zone_translation import propose_transverse_zone_translations
from rebar.optimization.services.cutting import PLATE_11700_BATCH_PROFILE, PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.patterned_conflicts import (
    _decode_bars, _locator_key, check_patterned_same_plane_conflicts,
)
from rebar.optimization.services.physical_host_fit import _validate_host
from rebar.optimization.services.solid_host import AREA_TOLERANCE_MM2, OrthogonalSolidHost
from rebar.optimization.services.stock_cutting import check_stock_cutting
from rebar.reporting.serialization import to_jsonable

from .analyze_composite_plate import CompositeDirectionSettings, _placements, _same_mesh

MAX_ZONES = 512
MAX_BARS = 5000
MAX_PROPOSAL_CALLS = 50000
TOL_MM = 1e-6


@dataclass(frozen=True)
class AsymmetricZoneShiftResult:
    report: dict
    direction_zones: tuple[tuple[CompositeLayoutZone, ...], ...]


@dataclass(frozen=True)
class _State:
    coverage: tuple
    schedule: tuple
    metrics: dict
    host: dict
    conflicts: object
    pairs: frozenset
    collision_footprints: dict


def _flat(groups):
    return tuple(zone for group in groups for zone in group)


def _replace_zone(groups, direction_index, zone_index, zone):
    changed = (*groups[direction_index][:zone_index], zone, *groups[direction_index][zone_index+1:])
    return (*groups[:direction_index], changed, *groups[direction_index+1:])


def _pair_keys(report):
    return frozenset(tuple(sorted((_locator_key(pair.first), _locator_key(pair.second))))
                     for pair in report.body_intersection_pairs)


def _collision_footprints(report):
    """Actual positive XY body overlap, not the zone or radius-plus-cover bbox."""
    footprints = {}
    for pair in report.body_intersection_pairs:
        first, second = pair.first, pair.second
        along_lo, along_hi = pair.longitudinal_overlap_interval_mm
        across_lo = max(first.transverse_coordinate_mm-first.diameter_mm/2,
                        second.transverse_coordinate_mm-second.diameter_mm/2)
        across_hi = min(first.transverse_coordinate_mm+first.diameter_mm/2,
                        second.transverse_coordinate_mm+second.diameter_mm/2)
        bounds = along_lo, across_lo, along_hi, across_hi
        if first.direction.axis is Axis.Y:
            bounds = bounds[1], bounds[0], bounds[3], bounds[2]
        footprints[tuple(sorted((_locator_key(first), _locator_key(second))))] = box(*bounds)
    return footprints


def _worsened_collisions(before, after):
    # New IDs are rejected separately. Equal IDs must not acquire overlap in
    # another physical location or gain penetration hidden by unchanged counts.
    return tuple(sorted(key for key, footprint in after.items() if key in before
                        and footprint.difference(before[key]).area > AREA_TOLERANCE_MM2))


def _conflicts(groups):
    return check_patterned_same_plane_conflicts(_flat(groups), assume_same_depth_per_direction=True,
        maximum_bars=MAX_BARS, maximum_pairs=200000, maximum_pair_checks=2000000)


def _host_check(groups, host, material):
    """Expand all actual axes afresh, including blocked and duplicate source bars."""
    bars, _ = _decode_bars(_flat(groups), MAX_BARS)
    blocked, by_direction = [], {}
    for bar in bars:
        start, end = bar.longitudinal_interval_mm
        radius_cover = bar.diameter_mm/2 + host.side_cover_mm
        transverse = bar.transverse_coordinate_mm
        envelope = (start-host.side_cover_mm, transverse-radius_cover,
                    end+host.side_cover_mm, transverse+radius_cover)
        if bar.direction.axis is Axis.Y:
            envelope = envelope[1], envelope[0], envelope[3], envelope[2]
        area = box(*envelope).difference(material).area
        direction = str(bar.direction)
        by_direction.setdefault(direction, {"bar_count": 0, "blocked_bar_count": 0})["bar_count"] += 1
        if area > AREA_TOLERANCE_MM2:
            blocked.append({"bar_key": _locator_key(bar), "outside_area_mm2": area})
            by_direction[direction]["blocked_bar_count"] += 1
    return {"physical_bar_count": len(bars), "blocked_bar_count": len(blocked),
            "blocked_bars": blocked, "by_direction": by_direction,
            "outside_area_sum_mm2": math.fsum(row["outside_area_mm2"] for row in blocked),
            "policy": "common_material_intersection_of_all_Z_sections",
            "checked_section_indexes": tuple(range(len(host.sections))),
            "area_tolerance_mm2": AREA_TOLERANCE_MM2, "actual_z_checked": False}


def _snapshot(problem, groups, configs, constraints, host, material):
    coverage = tuple(evaluate_composite_coverage(original.demand, zones,
        policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY, constraints=rule)
        for original, zones, rule in zip(problem.direction_problems, groups, constraints, strict=True))
    if any(item.status != "pass" for item in coverage):
        raise ValueError("Full original FE coverage/geometry failed; no partial demand is accepted")
    schedule = build_bar_schedule(group for zones, config in zip(groups, configs, strict=True)
        for group in composite_schedule_groups(zones, steel_class=config.steel_class))
    metrics = {"zone_count": sum(map(len, groups)), "position_count": len(schedule),
               "physical_bar_count": sum(row.physical_bar_count for row in schedule),
               "additional_mass_kg": math.fsum(row.total_mass_kg for row in schedule)}
    if (sum(row.physical_bar_count for row in coverage) != metrics["physical_bar_count"]
            or not math.isclose(math.fsum(row.additional_mass_kg for row in coverage),
                                metrics["additional_mass_kg"], rel_tol=0, abs_tol=TOL_MM)):
        raise ValueError("Fresh full coverage and complete schedule disagree on inventory/mass")
    host_check = _host_check(groups, host, material)
    conflicts = _conflicts(groups)
    if host_check["physical_bar_count"] != metrics["physical_bar_count"] or conflicts.bars_checked != metrics["physical_bar_count"]:
        raise ValueError("Actual all-bar expansion does not reproduce the complete schedule")
    return _State(coverage, schedule, metrics, host_check, conflicts, _pair_keys(conflicts),
                  _collision_footprints(conflicts))


def _assert_same_inventory(original_groups, groups):
    """Permit only fixed-width transverse windows and ONE common along shift."""
    if len(original_groups) != len(groups):
        raise ValueError("Source direction inventory changed")
    for originals, zones in zip(original_groups, groups, strict=True):
        if len(originals) != len(zones):
            raise ValueError("A source zone was removed or added")
        for old, new in zip(originals, zones, strict=True):
            if (not isinstance(new, CompositeLayoutZone) or len(new.components) != len(old.components)
                    or len(new.demand_bbox) != 4
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not math.isfinite(value) for value in new.demand_bbox)):
                raise ValueError("Source zone identity/complete finite geometry changed")
            across = 1 if old.direction.axis is Axis.X else 0
            along = 1-across
            shift = new.demand_bbox[across]-old.demand_bbox[across]
            if (new.demand_bbox[along] != old.demand_bbox[along]
                    or new.demand_bbox[along+2] != old.demand_bbox[along+2]
                    or not math.isclose(new.demand_bbox[across+2]-old.demand_bbox[across+2], shift,
                                        rel_tol=0, abs_tol=TOL_MM)):
                raise ValueError("Source demand length or selection width changed")
            restored, shifts = [], []
            for before, after in zip(old.components, new.components, strict=True):
                if not 0 < after.installed_length_mm <= 11700:
                    raise ValueError("Every original full bar must retain a length in (0,11700]")
                if any(not math.isclose(b-a, shift, rel_tol=0, abs_tol=TOL_MM)
                       for a, b in zip(before.axis_window_mm, after.axis_window_mm, strict=True)):
                    raise ValueError("Transverse component window did not follow the whole zone")
                deltas = tuple(b-a for a, b in zip(before.longitudinal_interval_mm,
                                                   after.longitudinal_interval_mm, strict=True))
                if not math.isclose(*deltas, rel_tol=0, abs_tol=TOL_MM):
                    raise ValueError("Source bar was clipped instead of translated")
                shifts.append(deltas[0])
                restored.append(replace(after, axis_window_mm=before.axis_window_mm,
                                        longitudinal_interval_mm=before.longitudinal_interval_mm))
            if any(not math.isclose(value, shifts[0], rel_tol=0, abs_tol=TOL_MM) for value in shifts):
                raise ValueError("All source components require ONE shared longitudinal shift")
            if replace(new, demand_bbox=old.demand_bbox, components=tuple(restored)) != old:
                raise ValueError("Source identity, recipe, phases, count, length or mass changed")


def _change(before, after):
    across = 1 if before.direction.axis is Axis.X else 0
    return {"direction": str(before.direction), "zone_id": before.id,
            "demand_bbox_before_mm": before.demand_bbox, "demand_bbox_after_mm": after.demand_bbox,
            "transverse_window_shift_mm": after.demand_bbox[across]-before.demand_bbox[across],
            "longitudinal_shift_mm": after.components[0].longitudinal_interval_mm[0]
                - before.components[0].longitudinal_interval_mm[0],
            "components": [{"component_index": old.component_index,
                "axis_window_before_mm": old.axis_window_mm, "axis_window_after_mm": new.axis_window_mm,
                "installed_interval_before_mm": old.longitudinal_interval_mm,
                "installed_interval_after_mm": new.longitudinal_interval_mm,
                "physical_bar_count": new.bar_count, "diameter_mm": new.rebar.diameter,
                "installed_length_mm": new.installed_length_mm}
                for old, new in zip(before.components, after.components, strict=True)]}


def shift_composite_plate_to_host(
    problem: PlateProblem,
    direction_zones: tuple[tuple[CompositeLayoutZone, ...], ...],
    settings: tuple[CompositeDirectionSettings, ...],
    host: OrthogonalSolidHost,
    *,
    maximum_transverse_shift_mm: float = 600,
    maximum_candidates_per_zone: int = 32,
    maximum_passes: int = 2,
) -> AsymmetricZoneShiftResult:
    """Translate complete source zones; keep every source when no move improves.

    Per-zone finite transverse proposals preserve their original sufficient FE
    fragments, then exact host intervals select the nearest common along shift.
    Greedy moves strictly improve (host-blocked bars, same-direction body pairs)
    lexicographically and introduce NO new pair IDs or new positive overlap area
    for existing pair IDs. This does not enumerate all
    possible longitudinal collision-free alternatives or coordinated multi-zone
    moves, and does not prove global infeasibility when blocked zones remain.
    """
    started = perf_counter()
    if (not isinstance(problem, PlateProblem) or not isinstance(direction_zones, tuple)
            or len(direction_zones) != 4 or any(not isinstance(group, tuple) for group in direction_zones)
            or not isinstance(settings, tuple) or any(not isinstance(item, CompositeDirectionSettings) for item in settings)):
        raise ValueError("Typed complete four-direction problem/zones/settings required")
    if (isinstance(maximum_transverse_shift_mm, bool) or not isinstance(maximum_transverse_shift_mm, (int, float))
            or not math.isfinite(maximum_transverse_shift_mm) or not 0 <= maximum_transverse_shift_mm <= 1200):
        raise ValueError("maximum_transverse_shift_mm must be finite in 0..1200")
    for value, lo, hi, name in ((maximum_candidates_per_zone, 1, 128, "maximum_candidates_per_zone"),
                               (maximum_passes, 1, 8, "maximum_passes")):
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError(f"{name} must be an integer in {lo}..{hi}")
    zone_count = sum(map(len, direction_zones))
    if zone_count > MAX_ZONES or zone_count*maximum_candidates_per_zone*maximum_passes > MAX_PROPOSAL_CALLS:
        raise ValueError("Complete zone/proposal budget exceeded; no source is truncated")
    configs = _canonical_direction_items(settings, lambda config: config.direction, item_name="zone shift settings")
    if any(not config.steel_class.strip() for config in configs):
        raise ValueError("Every direction requires an explicit steel class")
    _same_mesh(tuple(original.demand for original in problem.direction_problems))
    for original, zones, config in zip(problem.direction_problems, direction_zones, configs, strict=True):
        if (len(original.demand.cells) > 10000
                or any(not isinstance(zone, CompositeLayoutZone) or zone.direction != original.demand.direction for zone in zones)
                or original.constraints.cutting_profile not in ("plate-11700", PLATE_11700_BATCH_PROFILE)
                or original.constraints.allowed_cut_lengths_mm != PLATE_11700_CUT_LENGTHS_MM
                or isinstance(original.constraints.anchorage_diameters, bool)
                or original.constraints.anchorage_diameters != 40):
            raise ValueError("Explicit original full40d/plate-11700 constraints and matching bounded direction sources required")
        placements = dict(_placements(original.demand, config))
        if any(zone.placement != placements.get(zone.level_index) for zone in zones):
            raise ValueError("Source zone phases/profile provenance disagree with the explicit direction settings")
    _validate_host(host, 20000)
    material = host.sections[0].footprint
    for section in host.sections[1:]:
        material = material.intersection(section.footprint)
    constraints = tuple(replace(original.constraints, cutting_profile=PLATE_11700_BATCH_PROFILE)
                        for original in problem.direction_problems)
    _assert_same_inventory(direction_zones, direction_zones)
    before = current = _snapshot(problem, direction_zones, configs, constraints, host, material)
    stock_before = check_stock_cutting(before.schedule, time_limit_s=5)
    groups = direction_zones
    rejected, pools, accepted = [], [], []
    fit_cache = {}
    proposal_count = full_checks = 0
    passes = 0
    for pass_index in range(maximum_passes):
        passes += 1
        changes_this_pass = 0
        for direction_index, original in enumerate(problem.direction_problems):
            for zone_index in range(len(groups[direction_index])):
                zone = groups[direction_index][zone_index]
                context = {"pass_index": pass_index, "direction": str(zone.direction), "zone_id": zone.id}
                try:
                    pool = propose_transverse_zone_translations(original.demand, zone,
                        constraints=constraints[direction_index], maximum_shift_mm=maximum_transverse_shift_mm,
                        maximum_candidates=maximum_candidates_per_zone)
                    candidates = pool.candidates
                    pools.append({**context, "returned_candidates": len(candidates),
                        "evaluated_event_count": pool.evaluated_event_count,
                        "accepted_before_limit": pool.accepted_before_limit, "truncated": pool.truncated})
                except ValueError as error:
                    # No omission of the zone: along-only fitting is still tried.
                    candidates = (zone,)
                    pools.append({**context, "returned_candidates": 1, "truncated": False,
                                  "generation_error": str(error), "fallback": "original_zone_along_only"})
                local_before = _host_check(((zone,),), host, material)["blocked_bar_count"]
                ranked = []
                seen = set()
                for candidate_index, candidate in enumerate(candidates):
                    proposal_count += 1
                    record = {**context, "candidate_index": candidate_index}
                    try:
                        cache_key = direction_index, repr(candidate)
                        if cache_key not in fit_cache:
                            fit_cache[cache_key] = fit_composite_zone_to_solid_host(original.demand, candidate, host,
                                constraints=constraints[direction_index])
                        fitted = fit_cache[cache_key]
                        proposal = fitted.fitted_zone
                        signature = (proposal.demand_bbox, tuple((c.axis_window_mm, c.longitudinal_interval_mm)
                                                                for c in proposal.components))
                        if signature in seen:
                            continue
                        seen.add(signature)
                        changed = _replace_zone(groups, direction_index, zone_index, proposal)
                        _assert_same_inventory(direction_zones, changed)
                        if abs(_change(direction_zones[direction_index][zone_index], proposal)[
                                "transverse_window_shift_mm"]) > maximum_transverse_shift_mm+TOL_MM:
                            rejected.append({**record, "reason": "cumulative_transverse_shift_limit"})
                            continue
                        local_after = _host_check(((proposal,),), host, material)["blocked_bar_count"]
                        predicted_host = current.host["blocked_bar_count"]-local_before+local_after
                        if predicted_host > current.host["blocked_bar_count"]:
                            rejected.append({**record, "reason": "host_blocked_bar_count_would_increase"})
                            continue
                        conflict_check = _conflicts(changed)
                        pairs = _pair_keys(conflict_check)
                        new_pairs = pairs-current.pairs
                        if new_pairs:
                            rejected.append({**record, "reason": "new_same_direction_body_pairs",
                                             "new_pair_count": len(new_pairs), "new_pairs": sorted(new_pairs)})
                            continue
                        worsened = _worsened_collisions(current.collision_footprints,
                                                       _collision_footprints(conflict_check))
                        if worsened:
                            rejected.append({**record, "reason": "existing_same_direction_collision_worsened",
                                             "worsened_pair_count": len(worsened), "worsened_pairs": worsened})
                            continue
                        score = predicted_host, len(pairs)
                        if score >= (current.host["blocked_bar_count"], len(current.pairs)):
                            rejected.append({**record, "reason": "no_strict_joint_improvement",
                                "host_fit_status": fitted.status, "host_fit_reason": fitted.blocked_reason})
                            continue
                        change = _change(zone, proposal)
                        movement = abs(change["transverse_window_shift_mm"])+abs(change["longitudinal_shift_mm"])
                        ranked.append(((*score, movement, candidate_index), changed, record, fitted))
                    except ValueError as error:
                        rejected.append({**record, "reason": "proposal_validation_failed", "error": str(error)})
                for score, changed, record, fitted in sorted(ranked, key=lambda item: item[0]):
                    try:
                        full_checks += 1
                        checked = _snapshot(problem, changed, configs, constraints, host, material)
                        if (checked.schedule != before.schedule or checked.metrics != before.metrics
                                or checked.pairs-current.pairs
                                or _worsened_collisions(current.collision_footprints, checked.collision_footprints)
                                or (checked.host["blocked_bar_count"], len(checked.pairs)) != score[:2]):
                            raise ValueError("Fresh whole-plate coverage/schedule/stock-inventory/host/pairs disagree")
                    except ValueError as error:
                        rejected.append({**record, "reason": "full_plate_recheck_failed", "error": str(error)})
                        continue
                    moved = changed[direction_index][zone_index]
                    accepted.append({**record, **_change(zone, moved),
                        "host_blocked_before": current.host["blocked_bar_count"],
                        "host_blocked_after": checked.host["blocked_bar_count"],
                        "body_pairs_before": len(current.pairs), "body_pairs_after": len(checked.pairs),
                        "admissible_along_shift_windows_mm": fitted.admissible_shift_windows_mm,
                        "all_original_FE_coverage": "pass", "complete_schedule_and_stock_inventory_equal": True})
                    groups, current = changed, checked
                    changes_this_pass += 1
                    break
        if not changes_this_pass:
            break
    _assert_same_inventory(direction_zones, groups)
    after = _snapshot(problem, groups, configs, constraints, host, material)
    if (after.schedule != before.schedule or after.metrics != before.metrics or after.pairs-before.pairs
            or _worsened_collisions(before.collision_footprints, after.collision_footprints)
            or (after.host["blocked_bar_count"], len(after.pairs)) > (before.host["blocked_bar_count"], len(before.pairs))):
        raise ValueError("Final independent full-source verification failed; no result is returned")
    stock_after = check_stock_cutting(after.schedule, time_limit_s=5)
    changed_zones = [_change(old, new) for originals, zones in zip(direction_zones, groups, strict=True)
                     for old, new in zip(originals, zones, strict=True) if old != new]
    if any(abs(row["transverse_window_shift_mm"]) > maximum_transverse_shift_mm+TOL_MM for row in changed_zones):
        raise ValueError("Final total transverse translation exceeds the original-source shift limit")
    report = {"schema_version": "asymmetric-zone-shift/v1", "units": "mm", "case_id": problem.case_id,
        "status": "improved" if changed_zones else "unchanged", "placement_eligible": False,
        "source_demand_preserved": True, "source_zones_removed": 0, "source_bars_removed": 0,
        "coverage_policy": MONOTONE_SINGLE_STO_COVERAGE_POLICY,
        "coverage_before": [to_jsonable(item) for item in before.coverage],
        "coverage_after": [to_jsonable(item) for item in after.coverage],
        "metrics_before": before.metrics, "metrics_after": after.metrics,
        "bar_schedule_before": to_jsonable(before.schedule), "bar_schedule_after": to_jsonable(after.schedule),
        "stock_before": stock_before, "stock_after": stock_after, "stock_inventory_identical": after.schedule == before.schedule,
        "host_before": before.host, "host_after": after.host,
        "collisions_before": {"pair_count": len(before.pairs), "pairs": sorted(before.pairs)},
        "collisions_after": {"pair_count": len(after.pairs), "pairs": sorted(after.pairs)},
        "new_same_direction_pair_count": len(after.pairs-before.pairs),
        "worsened_existing_collision_footprint_count": len(_worsened_collisions(
            before.collision_footprints, after.collision_footprints)),
        "existing_collision_rule": "each_remaining_pair_XY_body_overlap_is_subset_of_original_with_area_tolerance",
        "changed_zones": changed_zones, "accepted_moves": accepted, "candidate_pools": pools,
        "rejected_proposals": rejected, "passes_executed": passes, "proposal_count": proposal_count,
        "accepted_candidate_full_plate_rechecks": full_checks, "final_full_plate_rechecked": True,
        "search_scope": "finite_bounded_greedy_transverse_windows_then_nearest_shared_along_translation",
        "search_completed_without_errors": not any("generation_error" in row for row in pools)
            and not any("error" in row for row in rejected),
        "distinct_exact_fit_calls": len(fit_cache),
        "global_optimum_proven": False, "global_infeasibility_proven": False,
        "candidate_pool_truncated": any(row["truncated"] for row in pools),
        "limits": {"maximum_transverse_shift_mm": maximum_transverse_shift_mm,
            "maximum_candidates_per_zone": maximum_candidates_per_zone, "maximum_passes": maximum_passes,
            "maximum_total_zones": MAX_ZONES, "maximum_total_bars": MAX_BARS,
            "maximum_proposal_calls": MAX_PROPOSAL_CALLS, "stock_check_time_limit_s": 5},
        "not_checked": ["actual_Z_and_cross_direction_3D_collisions", "existing_and_background_revit_reinforcement",
            "alternative_longitudinal_windows_for_avoiding_joint_collisions", "simultaneous_multi_zone_moves",
            "engineering_acceptance", "stock_cutting_manufacturing_assumptions", "Revit_readback"],
        "warning": "Полный исходный спрос и все исходные наборы сохранены; остаточные host-конфликты не скрыты. "
            "Это ограниченный геометрический поиск в исследовательской модели, не инженерное разрешение.",
        "runtime_ms": (perf_counter()-started)*1000}
    return AsymmetricZoneShiftResult(report, groups)
