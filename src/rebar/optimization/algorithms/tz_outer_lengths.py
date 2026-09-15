"""Atomic catalogue-length exchanges for the external-boundary research scope.

A too-long bar can receive a shorter complete bar. Another same-material bar
receives its longer length only when that complete new geometry also fits and
serves all ORIGINAL demand. The physical length histogram (and stock problem)
stays identical. No discarded crop, spare cut, changed diameter or hidden FE.
This bounded neighbourhood is deliberately not claimed to be a global search.
"""
from __future__ import annotations

from collections import Counter
import math
from time import perf_counter

from shapely.ops import unary_union

from .shaped_global_repair import _axis_choices, _body_bounds, _candidate_clear, _required_without
from ..contracts.physical import PhysicalBar
from ..services.cutting import PLATE_11700_CUT_LENGTHS_MM
from ..services.opening_relocation import lane_map
from ..services.shaped_fe_repair import ACTUAL_CORE_SERVICE, SOURCE_REQUIRED_SERVICE, shaped_service_offers
from ..services.shaped_global_coverage import _offers, _problem, _shape_batch, _strict_coverage
from ..services.tz_outer_scope import (
    TZ_LENGTH_REASSIGNMENT, check_tz_outer_bar, outer_scope_domain, outer_start_intervals,
    reselect_straight_length, translate_straight_whole,
)


def propose_tz_outer_length_exchanges(before, lanes, problem, actual_host, *,
        maximum_candidates=50000, time_limit_s=180, allow_transverse=True,
        longitudinal_service_policy=SOURCE_REQUIRED_SERVICE):
    """Return candidates, not acceptance. Use check_tz_outer_batch independently."""
    if (type(maximum_candidates) is not int or not 1 <= maximum_candidates <= 100000
            or isinstance(time_limit_s, bool) or not isinstance(time_limit_s, (int, float))
            or not math.isfinite(time_limit_s) or not 0 < time_limit_s <= 600
            or type(allow_transverse) is not bool):
        raise ValueError("Explicit bounded length-exchange search settings required")
    if longitudinal_service_policy not in (SOURCE_REQUIRED_SERVICE, ACTUAL_CORE_SERVICE):
        raise ValueError("Explicit supported longitudinal service policy required")
    _problem(problem)
    sources = lane_map(lanes)
    _shape_batch(before, sources)
    if _strict_coverage(problem, _offers(before, sources,
            longitudinal_service_policy=longitudinal_service_policy))["status"] != "pass":
        raise ValueError("The complete original FE must pass before length exchange")
    current = {(b.direction, b.id): b for b in before}
    bounds = {key: _body_bounds(b) for key, b in current.items()}
    domain = outer_scope_domain(actual_host)
    started, checks, exhausted, truncated = perf_counter(), 0, False, False
    reasons, rows, operations = Counter(), [], []

    def remaining():
        nonlocal exhausted
        if checks >= maximum_candidates or perf_counter()-started >= time_limit_s:
            exhausted = True
            return False
        return True

    def proposals(old, length, party, envelopes, *, required_override=None):
        nonlocal checks, truncated
        along = 0 if str(old.direction).endswith("X") else 1
        a, b = old.segments[0].start_mm, old.segments[0].end_mm
        proxy = PhysicalBar(old.id, old.direction, old.steel_class, old.diameter_mm,
                            a[1-along], (a[along], b[along]), old.source_bar_ids)
        required = (_required_without(old, party, sources, problem.problem(old.direction),
                    longitudinal_service_policy=longitudinal_service_policy)
                    if required_override is None else required_override)
        axes = (proxy.transverse_axis_mm,)
        if allow_transverse:
            axes, cut = _axis_choices(proxy, sources, domain, 300., 128)
            truncated |= cut
        resized = reselect_straight_length(old, length)
        for q in axes:
            if not remaining():
                return
            for lower, upper in outer_start_intervals(resized, actual_host, transverse_axis_mm=q):
                if required:
                    lower = max(lower, *(p.bounds[along+2]+40*old.diameter_mm-length for _, _, p in required))
                    upper = min(upper, *(p.bounds[along]-40*old.diameter_mm for _, _, p in required))
                if lower > upper:
                    reasons["new_length_cannot_fit_required_40d_interval"] += 1
                    continue
                for start in dict.fromkeys((min(max(a[along], lower), upper), lower, upper, (lower+upper)/2)):
                    if not remaining():
                        return
                    checks += 1
                    candidate = translate_straight_whole(resized, start_mm=start, transverse_axis_mm=q)
                    if check_tz_outer_bar(candidate, actual_host)["status"] != "pass":
                        reasons["external_body_not_contained"] += 1
                        continue
                    offered = shaped_service_offers(candidate, sources, longitudinal_service_policy=longitudinal_service_policy)
                    if any(p.difference(unary_union([o for d, s, o in offered if d >= dia and s <= step])).area > 0
                           for dia, step, p in required):
                        reasons["original_FE_not_supplied_with_retained_40d"] += 1
                        continue
                    if not _candidate_clear(candidate, party, envelopes):
                        reasons["changed_bar_3D_conflict"] += 1
                        continue
                    yield candidate

    for original in before:
        key = original.direction, original.id
        target = current[key]
        if target.shape_kind != "straight" or check_tz_outer_bar(target, actual_host)["status"] == "pass":
            continue
        if not remaining():
            break
        changed = False
        own_before = checks
        for length in PLATE_11700_CUT_LENGTHS_MM:
            if length >= target.selected_cut_length_mm:
                continue
            donors = [b for b in current.values() if b.shape_kind == "straight"
                      and b.steel_class == target.steel_class and b.diameter_mm == target.diameter_mm
                      and b.selected_cut_length_mm == length and (b.direction, b.id) != key]
            if not donors:
                continue
            for candidate in proposals(target, length, current, bounds):
                intermediate = {**current, key: candidate}
                envelopes = {**bounds, key: _body_bounds(candidate)}
                for donor in donors:
                    if not remaining():
                        break
                    for longer in proposals(donor, target.selected_cut_length_mm, intermediate, envelopes):
                        donor_key = donor.direction, donor.id
                        trial = {**intermediate, donor_key: longer}
                        if _strict_coverage(problem, _offers(tuple(trial.values()), sources,
                                longitudinal_service_policy=longitudinal_service_policy))["status"] != "pass":
                            reasons["atomic_pair_full_original_FE_loss"] += 1
                            continue
                        # Donor proposal checked against candidate and all others;
                        # target was checked before donor, so neither introduces a pair.
                        current = trial
                        bounds = {**envelopes, donor_key: _body_bounds(longer)}
                        operations.append({"target": {"direction": str(target.direction), "id": target.id},
                            "donor": {"direction": str(donor.direction), "id": donor.id},
                            "target_before_length_mm": target.selected_cut_length_mm,
                            "target_after_length_mm": length,
                            "donor_before_length_mm": donor.selected_cut_length_mm,
                            "donor_after_length_mm": longer.selected_cut_length_mm})
                        changed = True
                        break
                    if changed or exhausted:
                        break
                if changed or exhausted:
                    break
            if changed or exhausted:
                break
        rows.append({"direction": str(target.direction), "bar_id": target.id,
                     "outcome": "atomic_length_exchange" if changed else "retained_unresolved",
                     "candidate_checks": checks-own_before})
        if exhausted:
            break
    # A second, genuinely JOINT neighbourhood. The first bar is no longer
    # required to preserve responsibility alone before the donor is lengthened.
    # Exclude the OLD donor from target collision checking; then check the NEW
    # donor against target+all others and independently recheck all original FE.
    joint_rows = []
    joint_targets = [b for b in current.values() if b.shape_kind == "straight"
                     and check_tz_outer_bar(b, actual_host)["status"] != "pass"]
    # First address the explicit too-long-bar group rather than consume the
    # whole bounded joint budget on the first unavoidable boundary40d witness.
    joint_targets.sort(key=lambda b: (bool(outer_start_intervals(b, actual_host)),
                                     -b.selected_cut_length_mm, str(b.direction), b.id))
    for original in joint_targets:
        key = original.direction, original.id
        target = current[key]
        if target.shape_kind != "straight" or check_tz_outer_bar(target, actual_host)["status"] == "pass":
            continue
        if not remaining():
            break
        across = 1 if str(target.direction).endswith("X") else 0
        donors = [b for b in current.values() if b.shape_kind == "straight"
                  and b.direction == target.direction and b.steel_class == target.steel_class
                  and b.diameter_mm == target.diameter_mm
                  and b.selected_cut_length_mm < target.selected_cut_length_mm]
        donors.sort(key=lambda b: (abs(b.segments[0].start_mm[across]-target.segments[0].start_mm[across]),
                                  -b.selected_cut_length_mm, b.id))
        # These are SEARCH limits only, never a cropped inventory or obligation.
        limited = len(donors) > 24
        changed, attempts = False, 0
        for donor in donors[:24]:
            if not remaining():
                break
            attempts += 1
            donor_key = donor.direction, donor.id
            without_donor = {k: b for k, b in current.items() if k != donor_key}
            pair_needed = _required_without(target, without_donor, sources, problem.problem(target.direction),
                                             longitudinal_service_policy=longitudinal_service_policy)
            for index, candidate in enumerate(proposals(target, donor.selected_cut_length_mm,
                    without_donor, bounds, required_override=())):
                if index >= 8:
                    limited = True
                    break
                target_offers = shaped_service_offers(candidate, sources,
                                                      longitudinal_service_policy=longitudinal_service_policy)
                needed = []
                for diameter, step, polygon in pair_needed:
                    residual = polygon.difference(unary_union([p for d, s, p in target_offers
                                                               if d >= diameter and s <= step]))
                    if not residual.is_empty:
                        needed.append((diameter, step, residual))
                intermediate = {**current, key: candidate}
                envelopes = {**bounds, key: _body_bounds(candidate)}
                for longer in proposals(donor, target.selected_cut_length_mm, intermediate, envelopes,
                                        required_override=tuple(needed)):
                    trial = {**intermediate, donor_key: longer}
                    if _strict_coverage(problem, _offers(tuple(trial.values()), sources,
                            longitudinal_service_policy=longitudinal_service_policy))["status"] != "pass":
                        reasons["joint_pair_full_original_FE_loss"] += 1
                        continue
                    current = trial
                    bounds = {**envelopes, donor_key: _body_bounds(longer)}
                    operations.append({"target": {"direction": str(target.direction), "id": target.id},
                        "donor": {"direction": str(donor.direction), "id": donor.id},
                        "target_before_length_mm": target.selected_cut_length_mm,
                        "target_after_length_mm": donor.selected_cut_length_mm,
                        "donor_before_length_mm": donor.selected_cut_length_mm,
                        "donor_after_length_mm": longer.selected_cut_length_mm,
                        "joint_FE_responsibility_transfer": True})
                    changed = True
                    break
                if changed or exhausted:
                    break
            if changed or exhausted:
                break
        joint_rows.append({"direction": str(target.direction), "bar_id": target.id,
            "outcome": "joint_length_exchange" if changed else "retained_unresolved",
            "pair_attempts": attempts, "finite_neighbourhood_limited": limited})
        if exhausted:
            break
    after = tuple(current[b.direction, b.id] for b in before)
    def hist(bars):
        return Counter((b.steel_class, b.diameter_mm, b.selected_cut_length_mm) for b in bars)
    if hist(before) != hist(after):
        raise ValueError("Internal error: an atomic length exchange changed the cutting inventory")
    return after, {"policy": TZ_LENGTH_REASSIGNMENT, "search_kind": "atomic-same-material-length-exchanges",
        "longitudinal_service_policy": longitudinal_service_policy,
        "candidate_checks": checks, "maximum_candidates": maximum_candidates,
        "runtime_s": perf_counter()-started, "budget_exhausted": exhausted,
        "candidate_axes_truncated": truncated, "allow_transverse": allow_transverse,
        "operations": operations, "changed_bar_count": sum(a != b for a, b in zip(before, after)),
        "joint_neighbourhood": {"maximum_donors_per_bar": 24, "maximum_target_proposals_per_pair": 8,
                                "bars": joint_rows},
        "bars": rows, "rejections": dict(reasons), "exact_cutting_inventory_preserved": True,
        "global_optimality_proven": False, "placement_eligible": False}
