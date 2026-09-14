"""Finite fixed-axis host repair under frozen original FE obligations.

This solver never certifies the derivation of obligations. The application must
independently derive them BEFORE search, then run services.fe_host_repair's full
FE/source/stock checker. Original blocked bars are retained as explicit fallback,
not omitted or relabelled as placed. No Revit transport or engineering approval.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from functools import reduce
import math
from time import perf_counter

from shapely.geometry import box

from rebar.models import Axis, Direction, Layer

from ..contracts.physical import PhysicalBar
from ..services.physical_host_fit import _envelope, _host_intervals, _validate_host
from ..services.solid_host import AREA_TOLERANCE_MM2

TOL = 1e-6


class _BudgetReached(Exception):
    pass


@dataclass(frozen=True)
class _Candidate:
    owner: int
    bar: PhysicalBar
    original: bool
    contained: bool


def _number(value, low, high, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high
            or integer and type(value) is not int):
        raise ValueError("Invalid finite FE host-repair input or resource bound")


def _identity(bar):
    return bar.direction, bar.id


def _stock_key(bar):
    # Same sub-micron inventory representation as the independent stock checker.
    return bar.steel_class, bar.diameter_mm, round(bar.installed_length_mm, 6)


def _contained(bar, material, cover):
    return box(*_envelope(bar, cover)).difference(material).area <= AREA_TOLERANCE_MM2


def _collides(left, right):
    return (left.direction == right.direction
        and abs(left.transverse_axis_mm-right.transverse_axis_mm) < (left.diameter_mm+right.diameter_mm)/2-TOL
        and min(left.installed_interval_mm[1], right.installed_interval_mm[1])
            > max(left.installed_interval_mm[0], right.installed_interval_mm[0])+TOL)


def _validate(bars, obligations, host, reassign_lengths, limits):
    if type(reassign_lengths) is not bool:
        raise ValueError("Explicit boolean reassign_lengths required")
    if not isinstance(bars, tuple) or not 1 <= len(bars) <= 5000:
        raise ValueError("Complete bounded tuple of original PhysicalBars required")
    _number(limits[0], 0.001, 600)
    for value, ceiling in zip(limits[1:], (4096, 250000, 50000000), strict=True):
        _number(value, 1, ceiling, integer=True)
    _validate_host(host, 100000)
    seen = set()
    for bar in bars:
        if (not isinstance(bar, PhysicalBar) or not isinstance(bar.direction, Direction)
                or not isinstance(bar.direction.axis, Axis) or not isinstance(bar.direction.layer, Layer)
                or not isinstance(bar.id, str) or not bar.id.strip() or _identity(bar) in seen
                or not isinstance(bar.steel_class, str) or not bar.steel_class.strip()
                or not isinstance(bar.source_bar_ids, tuple) or not bar.source_bar_ids
                or any(not isinstance(value, str) or not value.strip() for value in bar.source_bar_ids)
                or len(set(bar.source_bar_ids)) != len(bar.source_bar_ids)):
            raise ValueError("Typed unique bars with preserved material and source owners required")
        seen.add(_identity(bar))
        _number(bar.diameter_mm, 6, 40, integer=True)
        _number(bar.transverse_axis_mm, -1e9, 1e9)
        if not isinstance(bar.installed_interval_mm, tuple) or len(bar.installed_interval_mm) != 2:
            raise ValueError("A positive original longitudinal interval is required")
        for value in bar.installed_interval_mm:
            _number(value, -1e9, 1e9)
        _number(bar.installed_length_mm, 1e-6, 11700+TOL)
    if not isinstance(obligations, dict) or set(obligations) != seen:
        raise ValueError("Exactly one frozen obligation for EVERY original bar; no unknown or omitted identity")
    for bar in bars:
        item = obligations[_identity(bar)]
        allowed = {"required_interval_mm", "served_cell_ids", "owners", "original_required_interval_mm"}
        if (not isinstance(item, dict) or not {"required_interval_mm", "served_cell_ids"} <= set(item)
                or set(item)-allowed or not isinstance(item["served_cell_ids"], tuple)):
            raise ValueError("Unknown obligation fields or missing original FE references")
        required = item["required_interval_mm"]
        if required is None:
            continue
        if not isinstance(required, tuple) or len(required) != 2:
            raise ValueError("Frozen FE obligation must be a positive finite tuple interval or None")
        for value in required:
            _number(value, -1e9, 1e9)
        margin = 40*bar.diameter_mm
        if (required[0] >= required[1] or not item["served_cell_ids"]
                or bar.installed_interval_mm[0] > required[0]-margin+TOL
                or bar.installed_interval_mm[1] < required[1]+margin-TOL):
            raise ValueError("Original bar does not preserve its frozen FE obligation plus full 40d")


def _neighbours(bars, count_pair):
    """Only bars whose fixed transverse body strips can meet share constraints."""
    result = [[] for _ in bars]
    groups = defaultdict(list)
    for index, bar in enumerate(bars):
        groups[bar.direction].append(index)
    for indices in groups.values():
        ordered = sorted(indices, key=lambda i: (bars[i].transverse_axis_mm, bars[i].id))
        max_radius = max(bars[i].diameter_mm for i in ordered)/2
        for offset, left_id in enumerate(ordered):
            left = bars[left_id]
            for right_id in ordered[offset+1:]:
                right = bars[right_id]
                if right.transverse_axis_mm-left.transverse_axis_mm >= left.diameter_mm/2+max_radius:
                    break
                count_pair()
                if abs(left.transverse_axis_mm-right.transverse_axis_mm) < (left.diameter_mm+right.diameter_mm)/2-TOL:
                    result[left_id].append(right_id)
                    result[right_id].append(left_id)
    return result


def _windows(bar, lengths, required, material, cover, tick):
    result = []
    if required is None:
        return result
    for length in lengths:
        tick()
        source_low = required[1]+40*bar.diameter_mm-length
        source_high = required[0]-40*bar.diameter_mm
        if source_low > source_high:
            continue
        query = replace(bar, installed_interval_mm=(bar.installed_interval_mm[0], bar.installed_interval_mm[0]+length))
        for left, right in _host_intervals(query, material, cover, 4096):
            lower, upper = max(left, source_low), min(right, source_high)
            if lower <= upper:
                result.append((length, lower, upper))
    return result


def _rank(original, candidate):
    return (candidate.installed_length_mm != original.installed_length_mm,
            abs(candidate.installed_interval_mm[0]-original.installed_interval_mm[0])
                + abs(candidate.installed_interval_mm[1]-original.installed_interval_mm[1]),
            candidate.installed_interval_mm)


def _make_candidates(bars, obligations, material, cover, reassign, neighbours,
                     maximum_per_bar, maximum_total, telemetry, tick):
    inventory = defaultdict(set)
    for bar in bars:
        inventory[(bar.steel_class, bar.diameter_mm)].add(round(bar.installed_length_mm, 6))
    windows = []
    for bar in bars:
        lengths = (sorted(inventory[(bar.steel_class, bar.diameter_mm)],
            key=lambda length: (abs(length-bar.installed_length_mm), length))
            if reassign else (bar.installed_length_mm,))
        windows.append(_windows(bar, lengths, obligations[_identity(bar)]["required_interval_mm"], material, cover, tick))
    # Finite event pool: direct old contacts and one-hop contacts to all interval
    # endpoints/clamped old starts. It is not an exhaustive continuous packing solver.
    anchors = []
    for bar, domains in zip(bars, windows, strict=True):
        values = {bar.installed_interval_mm}
        for length, lower, upper in domains:
            for start in (lower, upper, min(upper, max(lower, bar.installed_interval_mm[0]))):
                values.add((start, start+length))
        anchors.append(sorted(values))
    flat, by_bar = [], []
    for index, original in enumerate(bars):
        tick()
        available = min(maximum_per_bar, maximum_total-len(flat)-(len(bars)-index-1))
        original_candidate = _Candidate(index, original, True, _contained(original, material, cover))
        variants = {}
        old_signature = original.installed_interval_mm
        for length, lower, upper in windows[index]:
            starts = {lower, upper, min(upper, max(lower, original.installed_interval_mm[0]))}
            for neighbour in neighbours[index]:
                tick()
                for start, end in anchors[neighbour]:
                    for event in (end, start-length):
                        if lower <= event <= upper:
                            starts.add(event)
            for start in sorted(starts):
                tick()
                candidate = replace(original, installed_interval_mm=(start, start+length))
                if candidate.installed_interval_mm == old_signature or candidate.installed_interval_mm in variants:
                    continue
                # Independent polygon containment, not trusting interval construction.
                if not _contained(candidate, material, cover):
                    telemetry["candidates_rejected_independent_host_check"] += 1
                    continue
                variants[candidate.installed_interval_mm] = candidate
                telemetry["generated_nonoriginal_candidates"] += 1
                if len(variants) > max(4096, maximum_per_bar*64):
                    raise _BudgetReached("per_bar_event_generation_budget")
        selected = sorted(variants.values(), key=lambda b: _rank(original, b))[:max(0, available-1)]
        dropped = len(variants)-len(selected)
        if dropped:
            telemetry["candidate_pool_truncated"] = True
            telemetry["discarded_by_candidate_bounds"] += dropped
        # Always include the original as candidate zero, even if outside the host.
        ids = [len(flat)]
        flat.append(original_candidate)
        for candidate in selected:
            ids.append(len(flat))
            flat.append(_Candidate(index, candidate, False, True))
        by_bar.append(tuple(ids))
    telemetry["candidate_count"] = len(flat)
    telemetry["candidate_counts_by_bar"] = [len(indices) for indices in by_bar]
    return flat, by_bar


def _score(selected, original):
    return (sum(not item.contained for item in selected),
        sum(not item.original for item in selected),
        math.fsum(abs(item.bar.installed_interval_mm[0]-original[item.owner].installed_interval_mm[0])
            + abs(item.bar.installed_interval_mm[1]-original[item.owner].installed_interval_mm[1]) for item in selected))


def _check_selection(original, selected, obligations, material, cover, neighbours, inventory, count_pair):
    """Verify full reconstructed geometry/inventory independently of MILP rows."""
    if len(selected) != len(original) or {item.owner for item in selected} != set(range(len(original))):
        raise ValueError("Incomplete or duplicate integer bar selection")
    ordered = sorted(selected, key=lambda item: item.owner)
    for item, before in zip(ordered, original, strict=True):
        after = item.bar
        if replace(after, installed_interval_mm=before.installed_interval_mm) != before:
            raise ValueError("A fixed axis, source owner, material or identity was changed")
        if item.original != (before == after):
            raise ValueError("Original-candidate identity flag disagrees with actual geometry")
        if item.original:
            continue
        required = obligations[_identity(before)]["required_interval_mm"]
        if (required is None or after.installed_interval_mm[0] > required[0]-40*after.diameter_mm+TOL
                or after.installed_interval_mm[1] < required[1]+40*after.diameter_mm-TOL
                or not 0 < after.installed_length_mm <= 11700+TOL
                or not _contained(after, material, cover)):
            raise ValueError("Changed bar lost frozen FE obligation, 40d, stock limit or host containment")
    if Counter(_stock_key(item.bar) for item in ordered) != inventory:
        raise ValueError("The complete steel/diameter/length/count inventory changed")
    for i, adjacent in enumerate(neighbours):
        for j in adjacent:
            if j <= i or ordered[i].original and ordered[j].original:
                continue
            count_pair()
            if _collides(ordered[i].bar, ordered[j].bar):
                raise ValueError("Changed candidate intersects a selected same-direction bar")
    return tuple(item.bar for item in ordered)


def solve_fe_host_repair(bars, obligations, host, *, reassign_lengths=False, time_limit_s=60,
                        maximum_candidates_per_bar=96, maximum_total_candidates=50000,
                        maximum_pair_checks=5000000):
    """Return a complete independently screened finite-pool incumbent + telemetry.

    Lexicographic MILP stages: minimum remaining host failures, minimum changed
    bars, minimum total endpoint movement. Exact integer stage scores are frozen
    before the next stage; a timed-out tie-break retains the last checked result.
    Original+original collisions may remain explicitly unresolved, but any pair
    containing a modified bar is forbidden. Counts and stock lengths are conserved
    globally across directions, always within a fixed steel/diameter inventory.
    """
    started = perf_counter()
    limits = (time_limit_s, maximum_candidates_per_bar, maximum_total_candidates, maximum_pair_checks)
    _validate(bars, obligations, host, reassign_lengths, limits)
    material = reduce(lambda a, b: a.intersection(b), (s.footprint for s in host.sections))
    telemetry = {"policy_id": "finite-frozen-FE-fixed-axis-stock-conserving-host-repair/research-v1",
        "status": "original_incumbent", "placement_eligible": False, "engineering_approval": False,
        "global_optimality_claimed": False, "reassign_lengths": reassign_lengths,
        "source_demand_removed": False, "physical_bar_count": len(bars),
        "candidate_pool_truncated": False, "discarded_by_candidate_bounds": 0,
        "generated_nonoriginal_candidates": 0, "candidates_rejected_independent_host_check": 0,
        "candidate_count": len(bars), "pair_checks": 0, "incompatible_candidate_pair_count": 0,
        "solver_executed": False, "stages": [], "all_lexicographic_stages_optimal_in_pool": False,
        "candidate_event_policy": "domain endpoints, clamped original start, old contacts and one-hop neighbour-domain contacts",
        "inventory_length_rounding_decimal_places": 6,
        "limits": {"time_limit_s": time_limit_s, "maximum_candidates_per_bar": maximum_candidates_per_bar,
            "maximum_total_candidates": maximum_total_candidates, "maximum_pair_checks": maximum_pair_checks},
        "not_checked": ["obligation_derivation_requires_independent_service", "full_FE_coverage_requires_independent_checker",
            "rectangular_LayoutZone_regrouping", "actual_Z_cross_direction_3D_existing_revit_reinforcement",
            "normative_anchorage", "Revit_placement", "engineering_acceptance"]}
    original_checks = [_Candidate(i, bar, True, _contained(bar, material, host.side_cover_mm)) for i, bar in enumerate(bars)]
    inventory = Counter(_stock_key(bar) for bar in bars)
    best, best_items = bars, original_checks
    telemetry["host_blocked_before"] = _score(original_checks, bars)[0]

    def finish(status, reason=None):
        telemetry.update(status=status, reason=reason, elapsed_s=perf_counter()-started,
            host_blocked_after=_score(best_items, bars)[0], changed_bar_count=_score(best_items, bars)[1],
            total_endpoint_shift_mm=_score(best_items, bars)[2],
            host_failures_resolved=telemetry["host_blocked_before"]-_score(best_items, bars)[0],
            cutting_inventory_unchanged=Counter(_stock_key(bar) for bar in best) == inventory)
        return best, telemetry

    def tick():
        if perf_counter()-started >= time_limit_s:
            raise _BudgetReached("time_limit")

    def count_pair():
        tick()
        telemetry["pair_checks"] += 1
        if telemetry["pair_checks"] > maximum_pair_checks:
            raise _BudgetReached("pair_check_limit")

    if len(bars) > maximum_total_candidates:
        return finish("original_incumbent_budget", "candidate_budget_cannot_hold_all_originals")
    try:
        tick()
        neighbours = _neighbours(bars, count_pair)
        candidates, by_bar = _make_candidates(bars, obligations, material, host.side_cover_mm, reassign_lengths,
            neighbours, maximum_candidates_per_bar, maximum_total_candidates, telemetry, tick)
        if len(candidates) == len(bars):
            return finish("original_incumbent_no_alternatives")
        import numpy as np
        from scipy.optimize import Bounds, LinearConstraint, milp
        from scipy.sparse import coo_matrix

        row_indices, column_indices, coefficients, lower, upper = [], [], [], [], []

        def row(indices, minimum, maximum):
            row_id = len(lower)
            row_indices.extend([row_id]*len(indices))
            column_indices.extend(indices)
            coefficients.extend([1.0]*len(indices))
            lower.append(minimum)
            upper.append(maximum)

        for indices in by_bar:
            row(indices, 1, 1)
        stock_options = defaultdict(list)
        for index, item in enumerate(candidates):
            stock_options[_stock_key(item.bar)].append(index)
        for key, count in inventory.items():
            row(stock_options[key], count, count)
        if set(stock_options) != set(inventory):
            raise ValueError("Generated a length absent from the original stock inventory")
        for i, adjacent in enumerate(neighbours):
            for j in adjacent:
                if j <= i:
                    continue
                for a in by_bar[i]:
                    for b in by_bar[j]:
                        left, right = candidates[a], candidates[b]
                        if left.original and right.original:
                            continue
                        count_pair()
                        if _collides(left.bar, right.bar):
                            row((a, b), 0, 1)
                            telemetry["incompatible_candidate_pair_count"] += 1
        costs = [np.array([float(not item.contained) for item in candidates]),
                 np.array([float(not item.original) for item in candidates]),
                 np.array([abs(item.bar.installed_interval_mm[0]-bars[item.owner].installed_interval_mm[0])
                    + abs(item.bar.installed_interval_mm[1]-bars[item.owner].installed_interval_mm[1])
                    for item in candidates])]
        for stage, name in enumerate(("minimum_host_failures", "minimum_changed_bars", "minimum_endpoint_movement")):
            tick()
            matrix = coo_matrix((coefficients, (row_indices, column_indices)), shape=(len(lower), len(candidates))).tocsc()
            constraint = LinearConstraint(matrix, np.array(lower, dtype=float), np.array(upper, dtype=float))
            left = time_limit_s-(perf_counter()-started)
            telemetry["solver_executed"] = True
            result = milp(costs[stage], integrality=np.ones(len(candidates)), bounds=Bounds(0, 1),
                constraints=constraint, options={"time_limit": max(0.001, left), "mip_rel_gap": 0.0})
            data = {"objective": name, "status": int(result.status), "message": str(result.message),
                    "integer_incumbent_checked": False}
            telemetry["stages"].append(data)
            if result.status != 0 or result.x is None:
                return finish("checked_incumbent_solver_stopped" if best is not bars else "original_incumbent_solver_stopped",
                              "solver_did_not_finish_lexicographic_stage")
            values = np.asarray(result.x, dtype=float)
            if values.shape != (len(candidates),) or not np.all(np.isfinite(values)):
                raise ValueError("MILP returned malformed or nonfinite solution values")
            rounded = np.rint(values)
            if (np.max(np.abs(values-rounded)) > 1e-5 or np.any(rounded < 0) or np.any(rounded > 1)
                    or np.any(matrix@rounded < np.array(lower)-1e-7)
                    or np.any(matrix@rounded > np.array(upper)+1e-7)):
                raise ValueError("MILP incumbent failed independent binary/equality/inequality verification")
            selected = [item for item, bit in zip(candidates, rounded, strict=True) if bit == 1]
            # Final independent checks do not rely on the solver's matrix or float objective.
            candidate_bars = _check_selection(bars, selected, obligations, material, host.side_cover_mm,
                neighbours, inventory, count_pair)
            old_score, new_score = _score(best_items, bars), _score(selected, bars)
            if new_score[:stage+1] > old_score[:stage+1]:
                raise ValueError("Solver proposed a worse optimized objective prefix than the checked incumbent")
            # An unoptimized later tie-break may be worse at this stage. Keep
            # the better complete incumbent, but continue optimizing the prefix.
            if new_score < old_score:
                best, best_items = candidate_bars, selected
            data.update(integer_incumbent_checked=True, score=list(new_score))
            if stage < 2:
                indices = [i for i, value in enumerate(costs[stage]) if value == 1]
                row(indices, new_score[stage], new_score[stage])
        telemetry["all_lexicographic_stages_optimal_in_pool"] = True
        return finish("checked_finite_pool_solution")
    except _BudgetReached as exc:
        return finish("checked_incumbent_budget" if best is not bars else "original_incumbent_budget", str(exc))
    except ImportError:
        return finish("original_incumbent_solver_unavailable", "optional_scipy_not_installed")
    except (ValueError, RuntimeError) as exc:
        return finish("checked_incumbent_rejected_proposal" if best is not bars else "original_incumbent_rejected_proposal", str(exc))
