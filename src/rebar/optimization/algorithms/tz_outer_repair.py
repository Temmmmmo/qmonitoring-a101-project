"""Whole-bar inward fitting in the explicit, reduced TZ boundary domain.

Geometry-first probes do not prefilter away a move because of 40d. The checked
repair independently rejects FE loss/new collisions. Both results are reported.
"""
from __future__ import annotations

from collections import Counter
from time import perf_counter

from shapely.ops import unary_union

from rebar.models import Axis
from .shaped_global_repair import _axis_choices, _body_bounds, _candidate_clear, _required_without
from ..contracts.physical import PhysicalBar
from ..services.opening_relocation import lane_map
from ..services.shaped_collisions import check_shaped_collisions
from ..services.shaped_fe_repair import shaped_service_offers
from ..services.shaped_global_coverage import _offers, _problem, _shape_batch, _strict_coverage
from ..services.tz_outer_scope import (
    TZ_OUTER_SCOPE, check_tz_outer_bar, outer_scope_domain, outer_start_intervals,
    translate_straight_whole,
)


def propose_tz_outer_repair(before, lanes, problem, actual_host, *,
                            mode="preserve-demand", allow_transverse=False,
                            repair_collisions=False,
                            maximum_candidates=20000, time_limit_s=120):
    if (mode not in ("geometry-first", "preserve-demand")
            or type(allow_transverse) is not bool or type(repair_collisions) is not bool):
        raise ValueError("Explicit TZ fitting mode and transverse boolean required")
    if repair_collisions and (mode != "preserve-demand" or not allow_transverse):
        raise ValueError("Collision repair requires preserve-demand with transverse search")
    if (type(maximum_candidates) is not int or not 1 <= maximum_candidates <= 100000
            or isinstance(time_limit_s, bool) or not isinstance(time_limit_s, (int, float))
            or not 0 < time_limit_s <= 600):
        raise ValueError("Bounded search budgets required")
    _problem(problem)
    sources = lane_map(lanes)
    _shape_batch(before, sources)
    current = {(b.direction, b.id): b for b in before}
    bounds = {key: _body_bounds(bar) for key, bar in current.items()}
    domain = outer_scope_domain(actual_host)
    started = perf_counter()
    checked, changed = 0, 0
    exhausted, truncated = False, False
    rows, reasons = [], Counter()
    paired = set()
    if repair_collisions:
        collisions = check_shaped_collisions(before)
        paired = {(row["direction"], row["bar_id"]) for pair in (
            *collisions["proven_collision_pairs"], *collisions["uncertain_pairs"])
            for row in (pair["first"], pair["second"])}
    for original in before:
        key = original.direction, original.id
        outer_failure = check_tz_outer_bar(current[key], actual_host)["status"] != "pass"
        pair_failure = ((str(original.direction), original.id) in paired
                        and not _candidate_clear(current[key], current, bounds))
        if not outer_failure and not pair_failure:
            continue
        if checked >= maximum_candidates or perf_counter()-started >= time_limit_s:
            exhausted = True
            break
        if original.shape_kind != "straight":
            rows.append({"direction": str(original.direction), "bar_id": original.id,
                         "outcome": "nonstraight_outer_failure_not_modified"})
            continue
        along = 0 if original.direction.axis is Axis.X else 1
        segment = original.segments[0]
        q0, start = segment.start_mm[1-along], segment.start_mm[along]
        proxy = PhysicalBar(original.id, original.direction, original.steel_class,
            original.diameter_mm, q0, (start, segment.end_mm[along]), original.source_bar_ids)
        axes = (q0,)
        if allow_transverse:
            axes, cut = _axis_choices(proxy, sources, domain, 300., 128)
            truncated |= cut
        required = (_required_without(current[key], current, sources, problem.problem(original.direction))
                    if mode == "preserve-demand" else ())
        own_reasons, attempts, any_geometry = Counter(), 0, False
        moved = None
        for q in axes:
            intervals = outer_start_intervals(original, actual_host, transverse_axis_mm=q)
            for lower, upper in intervals:
                # Closest literal inward shift is tried BEFORE an FE-compatible
                # start. Thus the user's geometric idea is actually exercised.
                proposals = [min(max(start, lower), upper), lower, upper, (lower+upper)/2]
                if required:
                    length = proxy.installed_length_mm
                    fe_low = max(shape.bounds[along+2]+40*original.diameter_mm-length
                                 for _, _, shape in required)
                    fe_high = min(shape.bounds[along]-40*original.diameter_mm
                                  for _, _, shape in required)
                    lo, hi = max(lower, fe_low), min(upper, fe_high)
                    if lo <= hi:
                        proposals.extend((lo, hi, min(max(start, lo), hi)))
                for position in dict.fromkeys(proposals):
                    if checked >= maximum_candidates or perf_counter()-started >= time_limit_s:
                        exhausted = True
                        break
                    candidate = translate_straight_whole(original, start_mm=position, transverse_axis_mm=q)
                    checked += 1
                    attempts += 1
                    if check_tz_outer_bar(candidate, actual_host)["status"] != "pass":
                        own_reasons["external_body_not_contained"] += 1
                        continue
                    any_geometry = True
                    if mode == "preserve-demand":
                        offered = shaped_service_offers(candidate, sources)
                        if any(shape.difference(unary_union([p for d, s, p in offered
                            if d >= diameter and s <= step])).area > 0
                            for diameter, step, shape in required):
                            own_reasons["original_FE_loss_with_retained_40d"] += 1
                            continue
                        if not _candidate_clear(candidate, current, bounds):
                            own_reasons["new_or_changed_bar_3d_conflict"] += 1
                            continue
                        trial = {**current, key: candidate}
                        if _strict_coverage(problem, _offers(tuple(trial.values()), sources))["status"] != "pass":
                            own_reasons["full_original_FE_loss"] += 1
                            continue
                    moved = candidate
                    break
                if moved is not None or exhausted:
                    break
            if moved is not None or exhausted:
                break
        if moved is not None:
            current[key] = moved
            bounds[key] = _body_bounds(moved)
            changed += 1
        reasons.update(own_reasons)
        rows.append({"direction": str(original.direction), "bar_id": original.id,
            "target_external_boundary": outer_failure, "target_existing_collision": pair_failure,
            "outcome": "translated_whole" if moved is not None else "retained_unresolved",
            "candidate_checks": attempts, "geometry_fit_found": any_geometry,
            "rejected_proposals": dict(own_reasons),
            "no_longitudinal_fit_at_original_axis": not bool(outer_start_intervals(original, actual_host))})
        if exhausted:
            break
    return tuple(current[b.direction, b.id] for b in before), {
        "policy": TZ_OUTER_SCOPE, "mode": mode, "allow_transverse": allow_transverse,
        "repair_existing_collisions": repair_collisions,
        "transverse_limit_mm": 300 if allow_transverse else 0,
        "candidate_checks": checked, "maximum_candidates": maximum_candidates,
        "budget_exhausted": exhausted, "candidate_axes_truncated": truncated,
        "search_runtime_s": perf_counter()-started, "changed_bar_count": changed,
        "rejections": dict(reasons), "bars": rows, "global_optimality_proven": False,
        "geometry_first_is_not_an_accepted_layout": mode == "geometry-first",
    }
