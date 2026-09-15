"""Bounded set-cover repair with NEW host-contained catalogue-length bars.

Search operates after clipping: a candidate can replace several shorter pieces,
or add a neighbouring source-lane bar. No input FE is cropped to the host. This
is a finite search, not a proof that all remaining demand is infeasible.
"""
from __future__ import annotations

from dataclasses import replace
import math
import time

from shapely.geometry import Polygon

from rebar.models import Axis
from ..contracts.physical import PhysicalBar
from ..contracts.shaped_physical import Line3D
from ..services.cutting import PLATE_11700_CUT_LENGTHS_MM
from ..services.opening_relocation import lane_map
from ..services.physical_host_fit import _host_intervals
from ..services.shaped_collisions import _straight_pair
from ..services.shaped_geometry import check_shaped_host, shaped_cut_length_mm, shaped_mass_kg
from ..services.trimmed_repair import (
    batch_regions, check_rebuilt_trimmed_bars, demand_regions, validate_rebuilt_bars,
)
from ..services.tz_boundary_trim import trimming_domain


def _parts(geometry):
    if isinstance(geometry, Polygon):
        if geometry.area > 0:
            yield geometry
    elif hasattr(geometry, "geoms"):
        for member in geometry.geoms:
            yield from _parts(member)


def _bar(template, q, lo, hi, identifier):
    along = 0 if template.direction.axis is Axis.X else 1
    a, b = list(template.segments[0].start_mm), list(template.segments[0].end_mm)
    a[along], b[along] = lo, hi
    a[1-along] = b[1-along] = q
    return replace(template, id=identifier, segments=(Line3D(tuple(a), tuple(b)),),
                   selected_cut_length_mm=hi-lo)


def _material(template, domain):
    z, radius = template.segments[0].start_mm[2], template.diameter_mm/2
    selected = [s.footprint for s in domain.sections
                if min(z+radius, s.top_z_mm) > max(z-radius, s.bottom_z_mm)]
    if not selected or z-radius < domain.sections[0].bottom_z_mm or z+radius > domain.sections[-1].top_z_mm:
        return Polygon()
    result = selected[0]
    for shape in selected[1:]:
        result = result.intersection(shape)
    return result


def _positions(template, sources, target, material, maximum_shift_mm):
    along = 0 if template.direction.axis is Axis.X else 1
    across, q = 1-along, template.segments[0].start_mm[1-along]
    owners = [sources[template.direction, owner] for owner in template.source_bar_ids]
    low = max(q-maximum_shift_mm, *(o.axis_window_mm[0] for o in owners))
    high = min(q+maximum_shift_mm, *(o.axis_window_mm[1] for o in owners))
    positions = {q, low, high, (target.bounds[across]+target.bounds[across+2])/2}
    for owner in owners:
        positions.update((target.bounds[across]+owner.service_half_widths_mm[0],
                          target.bounds[across+2]-owner.service_half_widths_mm[1]))
    for p in _parts(material):
        for ring in (p.exterior, *p.interiors):
            for point in ring.coords:
                if low-template.diameter_mm <= point[across] <= high+template.diameter_mm:
                    positions.update((point[across]-template.diameter_mm/2,
                                      point[across]+template.diameter_mm/2))
    for value in sorted(positions, key=lambda v: (abs(v-q), v)):
        if not low <= value <= high:
            continue
        if any(abs(math.remainder(value-o.source.background_origin_mm, o.source.background_step_mm)) <
               (template.diameter_mm+o.source.background_diameter_mm)/2 for o in owners):
            continue
        yield value


def _candidates(templates, sources, missing, domain, maximum_shift_mm, cut_lengths):
    serial, seen = 0, set()
    materials = {}
    tasks = sorted(((key, part) for key, region in missing.items() for part in _parts(region)),
                   key=lambda item: (-item[1].area, str(item[0]), item[1].bounds))
    for (direction, diameter, step), target in tasks:
        along = 0 if direction.axis is Axis.X else 1
        for template in templates:
            if template.direction != direction or template.diameter_mm < diameter:
                continue
            owners = [sources[direction, owner] for owner in template.source_bar_ids]
            if not any(o.source.diameter_mm >= diameter and o.nominal_step_mm <= step
                       and o.axis_window_mm[0] < target.bounds[3-along]
                       and o.axis_window_mm[1] > target.bounds[1-along]
                       and o.source.required_interval_mm[0] < target.bounds[along+2]
                       and o.source.required_interval_mm[1] > target.bounds[along] for o in owners):
                continue
            material_key = direction, template.diameter_mm, template.segments[0].start_mm[2]
            if material_key not in materials:
                materials[material_key] = _material(template, domain)
            material = materials[material_key]
            for q in _positions(template, sources, target, material, maximum_shift_mm):
                proxy = PhysicalBar(template.id, direction, template.steel_class, template.diameter_mm,
                                    q, (0., 1.), template.source_bar_ids)
                for run_low, run_high_minus_one in _host_intervals(proxy, material, 0., 4096):
                    run_high = run_high_minus_one+1
                    a, b = max(run_low, target.bounds[along]), min(run_high, target.bounds[along+2])
                    if b <= a:
                        continue
                    sizes = [v for v in cut_lengths if v <= run_high-run_low]
                    for requested in (b-a, b-a+80*template.diameter_mm):
                        chosen = [v for v in sizes if v >= requested][:2] or sizes[-1:]
                        for length in chosen:
                            for desired in (a, b-length, (a+b-length)/2,
                                            a-40*template.diameter_mm, b+40*template.diameter_mm-length):
                                lo = min(max(desired, run_low), run_high-length)
                                identity = (direction, template.diameter_mm, q, lo, lo+length,
                                            template.source_bar_ids)
                                if identity in seen:
                                    continue
                                seen.add(identity)
                                serial += 1
                                yield _bar(template, q, lo, lo+length, f"rebuild-{serial}")


def _absorbed(candidate, current):
    """Merge only wholly contained, same-axis, same-Z, same-diameter pieces."""
    axis = 0 if candidate.direction.axis is Axis.X else 1
    line = candidate.segments[0]
    return tuple(bar for bar in current if bar.direction == candidate.direction
        and bar.diameter_mm == candidate.diameter_mm and bar.steel_class == candidate.steel_class
        and bar.segments[0].start_mm[1-axis] == line.start_mm[1-axis]
        and bar.segments[0].start_mm[2] == line.start_mm[2]
        and line.start_mm[axis] <= bar.segments[0].start_mm[axis]
        and line.end_mm[axis] >= bar.segments[0].end_mm[axis])


def _separated(candidate, remaining):
    line, radius = candidate.segments[0], candidate.diameter_mm/2
    for bar in remaining:
        other = bar.segments[0]
        rsum = radius+bar.diameter_mm/2
        if abs(line.start_mm[2]-other.start_mm[2]) >= rsum:
            continue
        if any(max(line.start_mm[i], other.start_mm[i])-min(line.end_mm[i], other.end_mm[i]) >= rsum
               for i in (0, 1)):
            continue
        exact = _straight_pair(candidate, bar)
        if exact is None or exact["status"] != "separated":
            return False
    return True


def rebuild_trimmed_zones(before, templates, lanes, problem, actual_host, *, maximum_mass_kg,
                          maximum_shift_mm=150., maximum_candidates=12000, maximum_additions=128,
                          time_limit_s=60., stock_time_limit_s=10., additional_cut_lengths_mm=()):
    """Greedy positive-area repair; final whole-batch proof is independent.

Retaining failed baseline coverage is not called success. New bars must fit the
real hole-aware host, introduce no body pairs and respect an explicit mass cap.
The initial finite candidate pool and time/budget exhaustion are reported.
"""
    for label, value, lo, hi in (("mass", maximum_mass_kg, .001, 1e8),
            ("shift", maximum_shift_mm, 0., 300.), ("time", time_limit_s, .001, 3600.)):
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not lo <= value <= hi:
            raise ValueError(f"Bounded finite {label} required")
    if (type(maximum_candidates) is not int or not 1 <= maximum_candidates <= 100000
            or type(maximum_additions) is not int or not 0 <= maximum_additions <= 1000):
        raise ValueError("Bounded integer candidate/addition budgets required")
    if (not isinstance(additional_cut_lengths_mm, tuple) or len(additional_cut_lengths_mm) > 32
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   or not 100 <= v <= 11700 or abs(11700/v-round(11700/v)) > 1e-9
                   for v in additional_cut_lengths_mm)):
        raise ValueError("Additional research lengths must be explicit bounded divisors of 11700")
    cut_lengths = tuple(sorted(set(PLATE_11700_CUT_LENGTHS_MM+additional_cut_lengths_mm)))
    sources, regions = lane_map(lanes), demand_regions(problem)
    if validate_rebuilt_bars(before, sources, actual_host):
        raise ValueError("Repair must start from a complete host-contained trimmed batch")
    validate_rebuilt_bars(templates, sources, actual_host)  # original templates may be outside
    domain, started = trimming_domain(actual_host, respect_openings=True), time.monotonic()
    current, mass = list(before), math.fsum(shaped_mass_kg(b) for b in before)
    offered = batch_regions(before, sources, regions)
    anchored = batch_regions(before, sources, regions, anchored=True)
    missing = {k: region.difference(offered[k]) for k, region in regions.items()}
    # Physical presence first. A later independent pass can target anchorage.
    candidates, exhausted, generated = [], False, 0
    for candidate in _candidates(templates, sources, missing, domain, maximum_shift_mm, cut_lengths):
        if generated >= maximum_candidates or time.monotonic()-started >= time_limit_s*.6:
            exhausted = True
            break
        generated += 1
        while any(b.direction == candidate.direction and b.id == candidate.id for b in before):
            candidate = replace(candidate, id=candidate.id+"-new")
        if check_shaped_host(candidate, domain)["status"] != "pass":
            continue
        supply = batch_regions((candidate,), sources, regions)
        gain = math.fsum(supply[k].difference(offered[k]).area for k in regions)
        if gain > 0:
            candidates.append((candidate, supply, gain/shaped_mass_kg(candidate)))
    candidates.sort(key=lambda row: (-row[2], row[0].id))
    actions, rejected = [], {"collision": 0, "mass": 0, "regression": 0}
    for candidate, _, _ in candidates:
        if len(actions) >= maximum_additions or time.monotonic()-started >= time_limit_s:
            exhausted = True
            break
        absorbed = _absorbed(candidate, current)
        candidate = replace(candidate, source_bar_ids=tuple(sorted(set(candidate.source_bar_ids).union(
            *(set(b.source_bar_ids) for b in absorbed)))))
        supply = batch_regions((candidate,), sources, regions)
        gain = math.fsum(supply[k].difference(offered[k]).area for k in regions)
        if gain <= 0:
            continue
        proposed_mass = mass+shaped_mass_kg(candidate)-math.fsum(shaped_mass_kg(b) for b in absorbed)
        if proposed_mass > maximum_mass_kg:
            rejected["mass"] += 1
            continue
        removed = {(b.direction, b.id) for b in absorbed}
        remaining = [b for b in current if (b.direction, b.id) not in removed]
        if not _separated(candidate, remaining):
            rejected["collision"] += 1
            continue
        proposed = (*remaining, candidate)
        new_presence = batch_regions(proposed, sources, regions)
        new_anchor = batch_regions(proposed, sources, regions, anchored=True)
        if any(offered[k].difference(new_presence[k]).area > 0 or
               anchored[k].difference(new_anchor[k]).area > 0 for k in regions):
            rejected["regression"] += 1
            continue
        current, mass, offered, anchored = list(proposed), proposed_mass, new_presence, new_anchor
        actions.append({"new_bar_id": candidate.id, "direction": str(candidate.direction),
            "removed_bar_ids": sorted(b.id for b in absorbed), "new_length_mm": shaped_cut_length_mm(candidate),
            "newly_covered_area_mm2": gain, "mass_kg_after": mass})
    result = tuple(current)
    checked = check_rebuilt_trimmed_bars(before, result, lanes, problem, actual_host,
                                        stock_time_limit_s=stock_time_limit_s)
    if checked["physical_metrics"]["mass_kg"] > maximum_mass_kg:
        checked["blockers"].append("requested_mass_limit")
        checked["status"] = "requires_review"
    checked["search"] = {"candidate_count": len(candidates), "generated_count": generated,
        "actions": actions, "rejections": rejected,
        "budget_exhausted": exhausted, "maximum_candidates": maximum_candidates,
        "maximum_additions": maximum_additions, "time_limit_s": time_limit_s,
        "elapsed_s": time.monotonic()-started, "maximum_mass_kg": maximum_mass_kg,
        "maximum_shift_mm": maximum_shift_mm, "global_infeasibility_proven": False,
        "additional_research_cut_lengths_mm": list(additional_cut_lengths_mm),
        "length_policy": "existing-catalogue-plus-explicit-research-divisors; batch-zero-waste-separately-checked"}
    return result, checked
