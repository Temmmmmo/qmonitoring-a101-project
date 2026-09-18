"""Opt-in longitudinal gap bridge after finite composite-zone recombination.

Only already valid full-demand search points are seeds. A bridge is a single
same-recipe/phase rectangle built by the shared window/detailing services;
every accepted full point passes the independent coverage and host checks.
No model, 40d, cutting rule or original demand is weakened.
"""
from __future__ import annotations

from collections import Counter
import math
from time import perf_counter

from rebar.models import Axis

from .composite_recombine import _pareto
from ..contracts.composite_search import CompositeSearchPoint, CompositeSearchProblem, CompositeSearchResult
from ..services.composite_coverage import (MAX_CELL_ZONE_PAIRS,
    check_composite_zone_coverage_geometry, evaluate_composite_coverage)
from ..services.composite_detailing import build_composite_zone, prepare_composite_detailing
from ..services.composite_host import evaluate_composite_host
from ..services.composite_host_fit import admissible_host_window, fit_composite_zone_to_host
from ..services.composite_windows import covering_composite_window
from ..services.zone_tradeoff import recommend_zone_knee


def _pair_gap(first, second, axis):
    along, across = ((0, 2), (1, 3)) if axis is Axis.X else ((1, 3), (0, 2))
    a, b = first.demand_bbox, second.demand_bbox
    gap = max(a[along[0]], b[along[0]]) - min(a[along[1]], b[along[1]])
    overlap = min(a[across[1]], b[across[1]]) - max(a[across[0]], b[across[0]])
    narrower = min(a[across[1]] - a[across[0]], b[across[1]] - b[across[0]])
    return gap, overlap, narrower


def _eligible_pairs(zones, axis, anchorage_diameters):
    pairs = []
    for i, first in enumerate(zones):
        for j in range(i + 1, len(zones)):
            second = zones[j]
            if (first.level_index != second.level_index or first.recipe != second.recipe
                    or first.placement != second.placement):
                continue
            gap, overlap, narrower = _pair_gap(first, second, axis)
            # The two facing ends each have at most one 40d research tail.
            bridge_limit = 2 * anchorage_diameters * min(spec.diameter for spec in first.recipe.additions)
            if gap <= bridge_limit + 1e-6 and overlap >= narrower * 0.5 - 1e-6:
                pairs.append((gap, -overlap, i, j))
    return sorted(pairs)


def bridge_composite_recombined(
    problem: CompositeSearchProblem, baseline: CompositeSearchResult, *,
    time_limit_s: float = 3.0, max_pair_evaluations: int = 256,
    max_merges_per_seed: int = 8, maximum_bar_length_mm: float = 11700,
) -> CompositeSearchResult:
    """Replace up to two seeds with strictly lighter, no-more-zones valid points.

    A shared three-second deadline and 256 unique proposal evaluations cover
    both seeds. Every accepted merge is independently checked against the full
    original FE demand; the untouched baseline is returned on any failure.
    """
    if (isinstance(time_limit_s, bool) or not math.isfinite(time_limit_s) or not 0 < time_limit_s <= 60
            or isinstance(maximum_bar_length_mm, bool) or not math.isfinite(maximum_bar_length_mm)
            or maximum_bar_length_mm <= 0):
        raise ValueError("нужны положительные конечные лимиты времени и длины")
    for value, maximum in ((max_pair_evaluations, 10000), (max_merges_per_seed, 128)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError("нужны положительные целые лимиты пар и шагов")
    if baseline.selected_index is not None and not 0 <= baseline.selected_index < len(baseline.points):
        raise ValueError("selected_index вне исходного фронта")
    if any(point.coverage.status != 'pass' for point in baseline.points):
        raise ValueError("bridge требует только проверенные исходные точки")
    started = perf_counter()
    deadline = started + time_limit_s
    telemetry = dict(baseline.telemetry)
    telemetry['algorithm'] = 'composite-bottom-up-plus-finite-recombine-plus-longitudinal-bridge/v1'
    telemetry['scope'] = telemetry.get('scope', '') + '_plus_bounded_post_recombine_longitudinal_bridge'
    stats = {'status': 'pending', 'seed_indexes': [], 'eligible_pairs': 0,
             'evaluated_pairs': 0, 'full_checks': 0, 'accepted_merges': 0,
             'candidate_mass_gain_kg': 0.0, 'retained_mass_gain_kg': 0.0,
             'positive_gap_pairs': 0, 'touching_overlap_pairs': 0,
             'rejections': {}, 'time_limit_s': time_limit_s,
             'max_pair_evaluations': max_pair_evaluations,
             'max_merges_per_seed': max_merges_per_seed,
             'maximum_bar_length_mm': maximum_bar_length_mm,
             'gap_limit': '2 * 40d(minimum ordered addition diameter)',
             'minimum_transverse_overlap_fraction_of_narrower': 0.5,
             'source_demand_preserved': True, 'engineering_optimality_proven': False,
             'timed_out': False, 'time_limit_semantics': 'deadline for starting operations; full checks finish'}
    telemetry['bridge'] = stats
    rejects = Counter()

    def finish(points, selected_index, status):
        stats['status'] = status
        stats['timed_out'] = perf_counter() >= deadline
        stats['rejections'] = dict(rejects)
        stats['elapsed_s'] = perf_counter() - started
        telemetry['runtime_s'] = baseline.telemetry.get('runtime_s', 0.0) + stats['elapsed_s']
        telemetry['retained_front_count'] = len(points)
        return CompositeSearchResult(tuple(points), selected_index, telemetry)

    if not baseline.points:
        return finish(baseline.points, baseline.selected_index, 'no_baseline_points')
    if not baseline.points[0].zones:
        return finish(baseline.points, baseline.selected_index, 'empty_demand')
    min_index = min(range(len(baseline.points)), key=lambda i: baseline.points[i].coverage.additional_mass_kg)
    seeds = tuple(dict.fromkeys((min_index, baseline.selected_index)))
    stats['seed_indexes'] = seeds
    context = prepare_composite_detailing(problem.demand)
    host_windows = {}
    improvements = []
    # Cache proposals, not "seen" flags: an unselected pair may still be useful
    # after another merge. Its unchanged geometry needs no second build.
    evaluated = {}
    for seed_index in seeds:
        if seed_index is None or perf_counter() >= deadline:
            break
        seed = baseline.points[seed_index]
        current = seed
        for step in range(max_merges_per_seed):
            if perf_counter() >= deadline:
                break
            pairs = _eligible_pairs(current.zones, problem.demand.direction.axis,
                                    problem.constraints.anchorage_diameters)
            stats['eligible_pairs'] += len(pairs)
            candidates = []
            for gap, _overlap, i, j in pairs:
                if perf_counter() >= deadline:
                    break
                first, second = current.zones[i], current.zones[j]
                key = tuple(sorted(((first.demand_bbox, first.level_index, first.recipe, first.placement),
                                    (second.demand_bbox, second.level_index, second.recipe, second.placement)),
                                   key=repr))
                if key in evaluated:
                    joined = evaluated[key]
                    if joined is None:
                        continue
                else:
                    if stats['evaluated_pairs'] >= max_pair_evaluations:
                        continue
                    stats['evaluated_pairs'] += 1
                    stats['positive_gap_pairs' if gap > 1 else 'touching_overlap_pairs'] += 1
                    joined = None
                bbox = (min(first.demand_bbox[0], second.demand_bbox[0]),
                        min(first.demand_bbox[1], second.demand_bbox[1]),
                        max(first.demand_bbox[2], second.demand_bbox[2]),
                        max(first.demand_bbox[3], second.demand_bbox[3]))
                level = first.level_index
                if joined is None:
                    try:
                        # Existing windows already include their outer periodic axes.
                        # Ask the shared coverer to reconstruct those axes from a
                        # slightly inset request; the full checker protects every FE.
                        inset = min(spec.step for spec in first.recipe.additions) / 2
                        if problem.demand.direction.axis is Axis.X:
                            request = (bbox[0], bbox[1] + inset, bbox[2], bbox[3] - inset)
                        else:
                            request = (bbox[0] + inset, bbox[1], bbox[2] - inset, bbox[3])
                        if problem.host_envelope is not None and level not in host_windows:
                            host_windows[level] = admissible_host_window(
                                problem.demand, level, problem.host_envelope, problem.constraints)
                        window = covering_composite_window(
                            problem.demand, request, level, first.placement,
                            constraints=problem.constraints, admissible_bbox=host_windows.get(level), context=context)
                        joined = build_composite_zone(
                            problem.demand, window, level, f'BR-{seed_index}-{step}-{i}-{j}',
                            first.placement, constraints=problem.constraints, context=context)
                        if any(component.installed_length_mm > maximum_bar_length_mm + 1e-6
                               for component in joined.components):
                            rejects['overlength'] += 1
                            evaluated[key] = None
                            continue
                        if problem.host_envelope is not None:
                            joined = fit_composite_zone_to_host(problem.demand, joined,
                                                               problem.host_envelope, constraints=problem.constraints)
                    except (ValueError, OverflowError) as error:
                        rejects[type(error).__name__] += 1
                        evaluated[key] = None
                        continue
                    evaluated[key] = joined
                prior_pair_mass = (current.coverage.zones[i].additional_mass_kg
                                   + current.coverage.zones[j].additional_mass_kg)
                joined_check = check_composite_zone_coverage_geometry(
                    problem.demand, joined, constraints=problem.constraints,
                    context=context, policy_id=problem.policy_id)
                if not joined_check.geometry_and_pattern_valid or joined_check.additional_mass_kg is None:
                    rejects['joined_geometry'] += 1
                    evaluated[key] = None
                    continue
                joined_mass = joined_check.additional_mass_kg
                gain = prior_pair_mass - joined_mass
                if gain <= 1e-6:
                    rejects['nonpositive_mass_gain'] += 1
                    continue
                candidates.append((gain, gap, i, j, joined))
            candidates.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
            accepted = False
            for gain, _gap, i, j, joined in candidates:
                if perf_counter() >= deadline:
                    break
                zones = tuple(zone for index, zone in enumerate(current.zones) if index not in (i, j)) + (joined,)
                if len(zones) * len(problem.demand.cells) > MAX_CELL_ZONE_PAIRS:
                    rejects['cell_zone_limit'] += 1
                    continue
                try:
                    check = evaluate_composite_coverage(problem.demand, zones,
                        policy_id=problem.policy_id, constraints=problem.constraints)
                    stats['full_checks'] += 1
                    if check.status != 'pass':
                        rejects['full_coverage'] += 1
                        continue
                    if problem.host_envelope is not None and 'fail' in evaluate_composite_host(
                            problem.demand, zones, problem.host_envelope,
                            constraints=problem.constraints)['checks'].values():
                        rejects['host'] += 1
                        continue
                except (ValueError, OverflowError) as error:
                    rejects[f'full_checker_{type(error).__name__}'] += 1
                    continue
                if (len(zones) > len(current.zones)
                        or check.additional_mass_kg >= current.coverage.additional_mass_kg - 1e-6):
                    rejects['no_full_point_dominance'] += 1
                    continue
                current = CompositeSearchPoint(zones, check)
                stats['accepted_merges'] += 1
                stats['candidate_mass_gain_kg'] += gain
                accepted = True
                break
            if not accepted:
                break
        if (current is not seed and len(current.zones) <= len(seed.zones)
                and current.coverage.additional_mass_kg < seed.coverage.additional_mass_kg - 1e-6):
            improvements.append((seed_index, current))
    if not improvements:
        return finish(baseline.points, baseline.selected_index, 'no_improvement')
    front = _pareto((*baseline.points, *(point for _, point in improvements)))
    if len(front) > len(baseline.points) or any(not any(
            len(new.zones) <= len(old.zones)
            and new.coverage.additional_mass_kg <= old.coverage.additional_mass_kg + 1e-6
            for new in front) for old in baseline.points):
        rejects['baseline_envelope'] += 1
        return finish(baseline.points, baseline.selected_index, 'fallback')
    recommendation = recommend_zone_knee(
        [(len(point.zones), point.coverage.additional_mass_kg) for point in front])
    telemetry['selection'] = recommendation
    stats['retained_mass_gain_kg'] = sum(
        baseline.points[index].coverage.additional_mass_kg - point.coverage.additional_mass_kg
        for index, point in improvements if point in front)
    stats['retained_seed_indexes'] = [index for index, point in improvements if point in front]
    return finish(front, recommendation['index'], 'improved')
