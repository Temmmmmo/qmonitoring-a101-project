"""Finite joint selection of contained straight-bar bypasses of small openings.

One position per physical bar, exact FE coverage atoms, incompatible position
pairs and unchanged cutting inventory. The independently checked incumbent is
retained on a solver timeout without a complete integer solution. This is not a
new zone optimizer and never changes source demand or invents an edge node.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import math
from time import perf_counter

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from ..contracts.opening_relocation import (
    OpeningRelocationConfig, OpeningRelocationLimitError, OpeningRelocationResult,
)
from ..services.opening_relocation import (
    TOL, check_relocation, collision, contained, eligible_holes, material_and_holes,
    service_boxes, source_geometry_ok, validate_inputs,
)
from ..services.physical_host_fit import _host_intervals, _source_window


def _config(config):
    if not isinstance(config, OpeningRelocationConfig):
        raise ValueError("Explicit typed opening correction configuration required")
    for value, low, high in ((config.maximum_shift_mm, 0.001, 300), (config.time_limit_s, 0.001, 600)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Invalid finite relocation search bounds")
    for value, low, high in ((config.maximum_candidates_per_bar, 1, 128),
        (config.maximum_total_candidates, 1, 20000), (config.maximum_coverage_atoms, 1, 200000),
        (config.maximum_pair_checks, 1, 5000000)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError("Invalid integer relocation search budget")


def _options(bar, sources, material, holes, hole_indexes, cover, bars, config):
    across = 1 if str(bar.direction).endswith("X") else 0
    parents = [sources[(bar.direction, key)] for key in bar.source_bar_ids]
    lo = max(bar.transverse_axis_mm - config.maximum_shift_mm, *(p.axis_window_mm[0] for p in parents))
    hi = min(bar.transverse_axis_mm + config.maximum_shift_mm, *(p.axis_window_mm[1] for p in parents))
    radius = cover + bar.diameter_mm / 2
    events = {lo, hi}
    for index in hole_indexes:
        bounds = holes[index].bounds
        events.update((bounds[across] - radius, bounds[across+2] + radius))
    # Background tangencies and neighbour tangencies delimit additional choices.
    for parent in parents:
        origin = parent.source.background_origin_mm
        distance = (bar.diameter_mm + parent.source.background_diameter_mm) / 2
        for i in range(math.floor((lo-origin)/300)-1, math.ceil((hi-origin)/300)+2):
            events.update((origin+300*i-distance, origin+300*i+distance))
    for other in bars:
        if other.direction == bar.direction and other.id != bar.id:
            distance = (bar.diameter_mm + other.diameter_mm) / 2
            events.update((other.transverse_axis_mm-distance, other.transverse_axis_mm+distance))
    events = sorted(q for q in events if lo <= q <= hi)
    axes = sorted({*events, *((a+b)/2 for a,b in zip(events, events[1:]))},
                  key=lambda q: (abs(q-bar.transverse_axis_mm), q))
    window, _ = _source_window(bar, tuple(p.source for p in parents))
    variants, seen = [], set()
    for q in axes:
        if abs(q-bar.transverse_axis_mm) <= TOL:
            continue
        candidate = replace(bar, transverse_axis_mm=q)
        if not source_geometry_ok(candidate, sources):
            continue
        for left, right in _host_intervals(candidate, material, cover, 4096):
            a, b = max(left, window[0]), min(right, window[1])
            if a > b:
                continue
            # The nearest start plus interval endpoints allow joint collision repair.
            starts = sorted({a, b, min(b, max(a, bar.installed_interval_mm[0]))},
                            key=lambda s: (abs(s-bar.installed_interval_mm[0]), s))
            for start in starts:
                item = replace(candidate, installed_interval_mm=(start, start+bar.installed_length_mm))
                signature = (q, start)
                if signature not in seen and contained(item, material, cover):
                    seen.add(signature)
                    variants.append(item)
    variants.sort(key=lambda b: (abs(b.transverse_axis_mm-bar.transverse_axis_mm),
        abs(b.installed_interval_mm[0]-bar.installed_interval_mm[0]), b.transverse_axis_mm, b.installed_interval_mm))
    return variants[:config.maximum_candidates_per_bar], len(variants)


def _coverage_rows(problem, fixed, choices, sources, config):
    """Exact rectangle arrangement on residual original FE, not point sampling.

All offered rectangle edges are included. Coverage is constant within each
resulting tile; midpoint queries select that constant signature, while an exact
polygon intersection decides whether the tile contains any original FE demand.
    """
    rows, tile_checks = set(), 0
    for original in problem.direction_problems:
        direction = original.demand.direction
        constants = [entry for bar in fixed if bar.direction == direction for entry in service_boxes(bar, sources)]
        variables = [(i, d, s, rect) for i, bar in enumerate(choices) if bar.direction == direction
                     for d, s, rect in service_boxes(bar, sources)]
        cached = {}
        for cell in original.demand.cells:
            recipe = original.demand.level(cell.level_index).recipe
            if not recipe.additions:
                continue
            required = recipe.additions[0]
            level = cell.level_index
            if level not in cached:
                constant = unary_union([rect for d, s, rect in constants if d >= required.diameter and s <= required.step])
                offered = [(i, rect) for i, d, s, rect in variables if d >= required.diameter and s <= required.step]
                cached[level] = constant, offered
            constant, offered = cached[level]
            residual = Polygon(cell.poly).difference(constant)
            if residual.is_empty:
                continue
            relevant = [(i, rect.bounds) for i, rect in offered if rect.intersection(residual).area > 0]
            xmin, ymin, xmax, ymax = residual.bounds
            xs = sorted({xmin, xmax, *(v for _, r in relevant for v in (r[0], r[2]) if xmin < v < xmax)})
            ys = sorted({ymin, ymax, *(v for _, r in relevant for v in (r[1], r[3]) if ymin < v < ymax)})
            for a, b in zip(xs, xs[1:]):
                for c, d in zip(ys, ys[1:]):
                    tile_checks += 1
                    if tile_checks > config.maximum_coverage_atoms * 20:
                        raise OpeningRelocationLimitError("Exact FE arrangement tile budget exceeded")
                    if residual.intersection(box(a, c, b, d)).area == 0:
                        continue
                    x, y = (a+b)/2, (c+d)/2
                    signature = frozenset(i for i, r in relevant if r[0] <= x <= r[2] and r[1] <= y <= r[3])
                    if not signature:
                        # Numerical slivers may be tolerated by the full checker,
                        # but never fabricate a cover row: retain the incumbent.
                        return None, tile_checks
                    rows.add(signature)
                    if len(rows) > config.maximum_coverage_atoms:
                        raise OpeningRelocationLimitError("Complete FE coverage atom budget exceeded")
    return sorted(rows, key=lambda row: tuple(sorted(row))), tile_checks


def relocate_small_openings(bars, lanes, problem, host, *, config):
    """Return a complete jointly checked plan, never a list of isolated suggestions."""
    _config(config)
    started = perf_counter()
    sources, _ = validate_inputs(bars, lanes, problem)
    material, outer, holes = material_and_holes(host)
    reasons, options, generated, bounded = Counter(), {}, 0, 0
    for index, bar in enumerate(bars):
        indexes, status = eligible_holes(bar, material, outer, holes, host.side_cover_mm)
        reasons[status] += 1
        if status != "eligible":
            continue
        variants, count = _options(bar, sources, material, holes, indexes, host.side_cover_mm, bars, config)
        generated += count
        bounded += count > len(variants)
        if variants:
            options[index] = [bar, *variants]
        else:
            reasons["no_contained_background_compatible_candidate"] += 1
    fixed = tuple(bar for i, bar in enumerate(bars) if i not in options)
    # A candidate conflicting with a fixed bar cannot be rescued by joint selection.
    removed = 0
    for i in list(options):
        prior = options[i]
        options[i] = [prior[0], *(bar for bar in prior[1:] if not any(collision(bar, other) for other in fixed))]
        removed += len(prior)-len(options[i])
    choices, groups, ids, unchanged = [], [], [], set()
    for index, variants in options.items():
        group = []
        for offset, bar in enumerate(variants):
            group.append(len(choices))
            if offset == 0:
                unchanged.add(len(choices))
            choices.append(bar)
            ids.append(index)
        groups.append(group)
    if len(choices) > config.maximum_total_candidates:
        raise OpeningRelocationLimitError("Complete relocation candidate budget exceeded")
    telemetry = {"classification": dict(reasons), "candidate_count": len(choices),
        "generated_candidate_count": generated, "candidate_limited_bar_count": bounded,
        "fixed_collision_candidates_removed": removed, "movable_bar_count": len(options),
        "scope": "finite candidate set; not a proof of impossibility outside this search"}
    result = bars
    if choices:
        rows, tile_checks = _coverage_rows(problem, fixed, choices, sources, config)
        telemetry.update(coverage_tile_checks=tile_checks, coverage_row_count=None if rows is None else len(rows))
        if rows is None:
            telemetry["status"] = "incumbent_retained_unrepresented_coverage_atom"
        else:
            incompatible, checked = [], 0
            ordered = sorted(range(len(choices)), key=lambda i: (str(choices[i].direction), choices[i].transverse_axis_mm))
            for pos, i in enumerate(ordered):
                for j in ordered[pos+1:]:
                    if (choices[j].direction != choices[i].direction
                            or choices[j].transverse_axis_mm - choices[i].transverse_axis_mm >= 40):
                        break
                    if ids[i] == ids[j] or i in unchanged and j in unchanged:
                        continue
                    checked += 1
                    if checked > config.maximum_pair_checks:
                        raise OpeningRelocationLimitError("Complete candidate pair budget exceeded")
                    if collision(choices[i], choices[j]):
                        incompatible.append((i, j))
            telemetry.update(incompatible_candidate_pairs=len(incompatible), pair_checks=checked)
            import numpy as np
            from scipy.optimize import Bounds, LinearConstraint, milp
            from scipy.sparse import coo_matrix

            all_rows = [*groups, *rows, *incompatible]
            coordinates = [(r, c) for r, row in enumerate(all_rows) for c in sorted(row)]
            matrix = coo_matrix((np.ones(len(coordinates)), tuple(zip(*coordinates))),
                                shape=(len(all_rows), len(choices))).tocsc()
            lower = np.array([*([1]*len(groups)), *([1]*len(rows)), *([0]*len(incompatible))])
            upper = np.array([*([1]*len(groups)), *([np.inf]*len(rows)), *([1]*len(incompatible))])
            # One more bypass dominates every possible sum of small displacement ties.
            objective = np.array([1.0 if i in unchanged else (
                abs(bar.transverse_axis_mm-bars[ids[i]].transverse_axis_mm)/config.maximum_shift_mm
                + abs(bar.installed_interval_mm[0]-bars[ids[i]].installed_interval_mm[0])/11700
                )/(4*(len(groups)+1)) for i, bar in enumerate(choices)])
            solved = milp(objective, integrality=np.ones(len(choices)), bounds=Bounds(0, 1),
                constraints=LinearConstraint(matrix, lower, upper),
                options={"time_limit": config.time_limit_s, "mip_rel_gap": 1e-8})
            telemetry.update(solver_status=int(solved.status), status="incumbent_retained")
            if solved.x is not None and np.shape(solved.x) == (len(choices),) and np.all(np.isfinite(solved.x)):
                rounded = np.rint(solved.x)
                actual = matrix @ rounded
                if (np.all(np.abs(solved.x-rounded) <= TOL) and np.all(rounded >= 0) and np.all(rounded <= 1)
                        and np.all(actual >= lower) and np.all(actual <= upper)):
                    selected = {ids[i]: choices[i] for i in np.flatnonzero(rounded)}
                    result = tuple(selected.get(i, bar) for i, bar in enumerate(bars))
                    telemetry["status"] = "integer_candidate_selected"
    else:
        telemetry["status"] = "no_geometric_candidates"
    # Separate validator knows neither the MILP matrix nor its candidate flags.
    review = check_relocation(bars, result, lanes, problem, host, maximum_shift_mm=config.maximum_shift_mm)
    review.update(search=telemetry, runtime_s=perf_counter()-started)
    return OpeningRelocationResult(result, review)
