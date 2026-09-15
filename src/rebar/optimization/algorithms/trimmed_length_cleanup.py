"""Bounded deterministic pruning; never shorten, merge through a hole or round."""
from collections import Counter
import time

from shapely.ops import unary_union
from shapely.strtree import STRtree

from ..services.shaped_geometry import shaped_cut_length_mm, shaped_mass_kg
from ..services.trimmed_length_cleanup import (
    _all_offers, _bounded_time, _covered_regions, _demand_regions,
    _validate_inputs, check_trimmed_length_cleanup,
)


def prune_redundant_trimmed_bars(before, lanes, problem, actual_host, *, maximum_candidate_checks=2048,
        maximum_removed_bars=256, time_limit_s=60, stock_time_limit_s=10):
    """Try exact duplicate bodies first, then shorter pieces, preserving ALL old FE coverage.

The length order is a search priority, not a minimum manufacturing length. A bar
may vanish only if the union of other independently sufficient offers preserves
its previously covered ORIGINAL demand for BOTH presence and control40d.
The time limit bounds candidate search; independent full checks run afterwards.
"""
    for name, value, maximum in (("maximum_candidate_checks", maximum_candidate_checks, 10000),
                                 ("maximum_removed_bars", maximum_removed_bars, 10000)):
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError(f"{name} must be an explicit integer in 0..{maximum}")
    _bounded_time(time_limit_s, "time_limit_s", 120)
    _bounded_time(stock_time_limit_s, "stock_time_limit_s")
    sources, _domain = _validate_inputs(before, lanes, problem, actual_host)
    demanded = _demand_regions(problem)
    required = _covered_regions(_all_offers(before, sources), demanded)
    # Cache each physical contribution once. A removal can only lose geometry
    # supplied by that bar; spatial queries reduce the equivalent exact-union
    # check to neighbours. The independent service still checks the whole batch.
    individual = [_all_offers((bar,), sources) for bar in before]
    contributions, trees, indexes = {}, {}, {}
    for key in required:
        policy, direction, level = key
        diameter, step, _geometry = demanded[direction, level]
        members = []
        for index, bar in enumerate(before):
            if bar.direction != direction:
                continue
            supplied = unary_union([shape for d, s, shape in individual[index][policy].get(direction, ())
                                    if d >= diameter and s <= step])
            if not supplied.is_empty:
                contributions[index, key] = supplied
                members.append((index, supplied))
        if len(contributions) > 1000000:
            raise ValueError("Cleanup contribution budget exceeded; no partial proof")
        trees[key] = STRtree([shape for _, shape in members])
        indexes[key] = members
    body_counts = Counter((bar.direction, bar.steel_class, bar.diameter_mm, bar.segments) for bar in before)
    ordered = sorted(enumerate(before), key=lambda item: (
        body_counts[item[1].direction, item[1].steel_class, item[1].diameter_mm, item[1].segments] < 2,
        shaped_cut_length_mm(item[1]), -shaped_mass_kg(item[1]), str(item[1].direction), item[1].id))
    current, active, attempted, removed, rejected = before, set(range(len(before))), 0, 0, 0
    start = time.monotonic()
    stop_reason = "candidate_pool_exhausted"
    for index, bar in ordered:
        if attempted >= maximum_candidate_checks or removed >= maximum_removed_bars:
            stop_reason = "finite_candidate_or_removal_limit"
            break
        if time.monotonic()-start >= time_limit_s:
            stop_reason = "candidate_search_time_limit"
            break
        attempted += 1
        loses_coverage = False
        for key, original in required.items():
            supplied = contributions.get((index, key))
            if supplied is None:
                continue
            at_risk = supplied.intersection(original)
            if at_risk.is_empty:
                continue
            others = [indexes[key][int(hit)][1] for hit in trees[key].query(at_risk)
                      if indexes[key][int(hit)][0] in active and indexes[key][int(hit)][0] != index]
            if at_risk.difference(unary_union(others)).area > 0:
                loses_coverage = True
                break
        if loses_coverage:
            rejected += 1
            continue
        active.remove(index)
        removed += 1
    search_elapsed = time.monotonic()-start
    current = tuple(bar for index, bar in enumerate(before) if index in active)
    report = check_trimmed_length_cleanup(before, current, lanes, problem, actual_host,
                                          stock_time_limit_s=stock_time_limit_s)
    rejected_stock_proposal = None
    if not report["accepted_nonregression"]:
        rejected_stock_proposal = {"removed_bar_count": report["removed_bar_count"],
                                  "physical_metrics": report["physical_metrics"],
                                  "stock_cutting": report["stock_cutting"]}
        current = before
        report = check_trimmed_length_cleanup(before, before, lanes, problem, actual_host,
                                              stock_time_limit_s=stock_time_limit_s)
    report["search"] = {"strategy": "exact-duplicate-first-then-shorter-first-deterministic-deletion",
        "candidate_checks": attempted, "coverage_rejected_candidates": rejected,
        "maximum_candidate_checks": maximum_candidate_checks, "maximum_removed_bars": maximum_removed_bars,
        "search_time_limit_s": time_limit_s, "search_elapsed_s": search_elapsed, "stop_reason": stop_reason,
        "stock_regression_proposal_rolled_back": rejected_stock_proposal,
        "minimum_allowed_length_invented": False, "merge_or_extension_attempted": False,
        "global_minimum_bar_count_or_positions_proved": False}
    return current, report
