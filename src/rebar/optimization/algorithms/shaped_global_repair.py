"""Finite global-FE repair: coverage may pass from one existing bar to another.

This changes neither the source field nor any required FE value. Original owner
IDs remain provenance, not a requirement to preserve every redundant old offer.
The independent final checker, not this search, certifies the complete party.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import math
from time import perf_counter

from shapely.geometry import Polygon
from shapely.ops import unary_union

from .shaped_translation_candidates import straight_translation_candidates
from ..services.opening_relocation import coverage_from_offers, lane_map
from ..services.shaped_collisions import check_shaped_collisions
from ..services.shaped_fe_repair import (
    ResearchLayerProfile, exterior_edge_choices, layer_elevations, shaped_service_offers,
)
from ..services.shaped_geometry import (
    _analytic_bounds, build_u_edge_bar, check_shaped_host, straight_bar_from_physical,
)


def _key(bar):
    return bar.direction, bar.id


def _body_bounds(bar):
    parts = [_analytic_bounds(s) for s in bar.segments]
    r = bar.diameter_mm/2+1e-7
    return (tuple(min(p[0][i] for p in parts)-r for i in range(3)),
            tuple(max(p[1][i] for p in parts)+r for i in range(3)))


def _near(a, b):
    return all(a[0][i] <= b[1][i] and b[0][i] <= a[1][i] for i in range(3))


def _pair_mentions(pair, bar):
    return any(row["bar_id"] == bar.id and row["direction"] == str(bar.direction)
               for row in (pair["first"], pair["second"]))


def _candidate_clear(candidate, current, bounds):
    envelope = _body_bounds(candidate)
    neighbors = tuple(b for key, b in current.items()
                      if key != _key(candidate) and _near(envelope, bounds[key]))
    if not neighbors:
        return True
    report = check_shaped_collisions((candidate, *neighbors))
    return not any(_pair_mentions(pair, candidate) for pair in (
        *report["proven_collision_pairs"], *report["uncertain_pairs"]))


def _required_without(bar, current, sources, original):
    """Search-only necessary regions; final source checker reruns every FE."""
    other_offers = [offer for key, value in current.items() if value.direction == bar.direction
                    and key != _key(bar) for offer in shaped_service_offers(value, sources)]
    result = []
    for level in original.demand.levels:
        if not level.recipe.additions:
            continue
        if len(level.recipe.additions) != 1:
            raise ValueError("Explicit single-addition source recipes required")
        spec = level.recipe.additions[0]
        demand = unary_union([Polygon(c.poly) for c in original.demand.cells if c.level_index == level.index])
        other = unary_union([shape for d, step, shape in other_offers
                             if d >= spec.diameter and step <= spec.step])
        needed = demand.difference(other)
        if not needed.is_empty:
            result.append((spec.diameter, spec.step, needed))
    return tuple(result)


def _covers_required(candidate, sources, required):
    offered = shaped_service_offers(candidate, sources)
    for diameter, step, needed in required:
        supply = unary_union([p for d, s, p in offered if d >= diameter and s <= step])
        # No positive source-owner discard is involved: final FE coverage uses
        # its existing explicit geometric tolerance, not this candidate flag.
        if needed.difference(supply).area > max(1e-6, needed.area*1e-9):
            return False
    return True


def _axis_choices(previous, sources, host, maximum_shift_mm, maximum_axes):
    across = 1 if str(previous.direction).endswith("X") else 0
    parents = [sources[previous.direction, owner] for owner in previous.source_bar_ids]
    lo = max(previous.transverse_axis_mm-maximum_shift_mm, *(p.axis_window_mm[0] for p in parents))
    hi = min(previous.transverse_axis_mm+maximum_shift_mm, *(p.axis_window_mm[1] for p in parents))
    choices = {lo, hi, previous.transverse_axis_mm}
    for delta in (0.1, 0.25, 1, 5, 10, 25, 50, 75, 100, 150, 200, 250, 300):
        choices.update((previous.transverse_axis_mm-delta, previous.transverse_axis_mm+delta))
    reserve = host.side_cover_mm+previous.diameter_mm/2
    for section in host.sections:
        parts = [section.footprint] if isinstance(section.footprint, Polygon) else section.footprint.geoms
        for part in parts:
            for ring in (part.exterior, *part.interiors):
                for point in ring.coords:
                    for q in (point[across]-reserve, point[across]+reserve):
                        if lo <= q <= hi:
                            choices.update((q, q-0.1, q+0.1))
    for parent in parents:
        source = parent.source
        for index in range(math.floor((lo-source.background_origin_mm)/300)-1,
                           math.ceil((hi-source.background_origin_mm)/300)+2):
            center = source.background_origin_mm+300*index
            radius = (previous.diameter_mm+source.background_diameter_mm)/2
            choices.update((center-radius, center+radius, center-radius-0.1, center+radius+0.1))
    finite = [q for q in choices if lo <= q <= hi and all(
        abs(math.remainder(q-p.source.background_origin_mm, 300))
        >= (previous.diameter_mm+p.source.background_diameter_mm)/2 for p in parents)]
    finite.sort(key=lambda q: (abs(q-previous.transverse_axis_mm), q))
    return tuple(finite[:maximum_axes]), len(finite) > maximum_axes


def propose_global_shaped_repair(before, lanes, problem, host, *,
        layer_profile=ResearchLayerProfile(), maximum_shift_mm=300., maximum_axes_per_bar=64,
        maximum_candidates=50000, maximum_passes=2, time_limit_s=180,
        maximum_longitudinal_shift_mm=0.):
    """A bounded incumbent search. Exhaustion is visible, never proof of optimum."""
    for value, low, high in ((maximum_shift_mm, 0, 300), (time_limit_s, .001, 600),
                            (maximum_longitudinal_shift_mm, 0, 11700)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Finite bounded search limits required")
    for value, cap in ((maximum_axes_per_bar, 256), (maximum_candidates, 100000), (maximum_passes, 10)):
        if type(value) is not int or not 1 <= value <= cap:
            raise ValueError("Positive bounded integer search limits required")
    # Typed source/inventory validation and unchanged original FE baseline proof.
    from ..services.collision_replacement import freeze_owner_fe_service
    freeze_owner_fe_service(before, lanes, problem)
    sources = lane_map(lanes)
    current = {}
    for previous in before:
        zm, _ = layer_elevations(host, previous.direction, previous.diameter_mm, layer_profile)
        current[_key(previous)] = straight_bar_from_physical(previous, axis_z_mm=zm,
                                                            placement_profile_id=layer_profile.id)
    bounds = {key: _body_bounds(bar) for key, bar in current.items()}
    host_ok = {key: check_shaped_host(bar, host)["whole_body_with_cover_contained"] for key, bar in current.items()}
    initial_pairs = check_shaped_collisions(tuple(current.values()))
    conflict_keys = {(row["direction"], row["bar_id"]) for pair in (
        *initial_pairs["proven_collision_pairs"], *initial_pairs["uncertain_pairs"])
        for row in (pair["first"], pair["second"])}
    started, checked, reasons, operations = perf_counter(), 0, Counter(), []
    truncated, exhausted, passes = False, False, 0
    for iteration in range(maximum_passes):
        passes += 1
        changed_this_pass = False
        ordered = sorted(before, key=lambda b: ((str(b.direction), b.id) not in conflict_keys,
                                                host_ok[_key(b)], str(b.direction), b.id))
        for previous in ordered:
            key = _key(previous)
            if host_ok[key] and (str(previous.direction), previous.id) not in conflict_keys:
                continue
            if checked >= maximum_candidates or perf_counter()-started > time_limit_s:
                exhausted = True
                break
            required = _required_without(current[key], current, sources, problem.problem(previous.direction))
            axes, cut = _axis_choices(previous, sources, host, maximum_shift_mm, maximum_axes_per_bar)
            truncated |= cut
            zm, zr = layer_elevations(host, previous.direction, previous.diameter_mm, layer_profile)

            def proposals():
                nonlocal truncated
                for q in axes:
                    shifted = replace(previous, transverse_axis_mm=q)
                    yield straight_bar_from_physical(shifted, axis_z_mm=zm, placement_profile_id=layer_profile.id)
                    translations, positions_cut = straight_translation_candidates(shifted, host, required,
                        maximum_longitudinal_shift_mm=maximum_longitudinal_shift_mm)
                    truncated |= positions_cut
                    for translated in translations:
                        yield straight_bar_from_physical(translated, axis_z_mm=zm,
                                                         placement_profile_id=layer_profile.id)
                    for edge, inward in exterior_edge_choices(host, previous.direction, q, previous.diameter_mm):
                        result = build_u_edge_bar(bar_id=previous.id, direction=previous.direction,
                            steel_class=previous.steel_class, diameter_mm=previous.diameter_mm,
                            transverse_axis_mm=q, edge_coordinate_mm=edge, inward_sign=inward,
                            main_axis_z_mm=zm, return_axis_z_mm=zr,
                            slab_thickness_mm=host.sections[-1].top_z_mm-host.sections[0].bottom_z_mm,
                            side_cover_mm=host.side_cover_mm, cut_length_mm=previous.installed_length_mm,
                            source_bar_ids=previous.source_bar_ids, placement_profile_id=layer_profile.id)
                        if result.status == "geometry_conditions_met":
                            yield result.bar

            for candidate in proposals():
                checked += 1
                if checked > maximum_candidates or perf_counter()-started > time_limit_s:
                    exhausted = True
                    break
                if candidate == current[key]:
                    continue
                if not _covers_required(candidate, sources, required):
                    reasons["global_original_FE_loss"] += 1
                    continue
                if not check_shaped_host(candidate, host)["whole_body_with_cover_contained"]:
                    reasons["host_not_proven"] += 1
                    continue
                if not _candidate_clear(candidate, current, bounds):
                    reasons["changed_bar_3D_collision_or_uncertainty"] += 1
                    continue
                # Strong final guard for each accepted step. A search mask or
                # candidate's own declared coverage never replaces this proof.
                updated = {**current, key: candidate}
                offered = {p.demand.direction: [o for b in updated.values() if b.direction == p.demand.direction
                    for o in shaped_service_offers(b, sources)] for p in problem.direction_problems}
                if coverage_from_offers(problem, offered, policy="global-original-FE-candidate-check")["status"] != "pass":
                    reasons["full_source_recheck_failed"] += 1
                    continue
                current = updated
                bounds[key], host_ok[key] = _body_bounds(candidate), True
                conflict_keys.discard((str(previous.direction), previous.id))
                operations.append({"direction": str(previous.direction), "bar_id": previous.id,
                    "shape": candidate.shape_kind, "original_axis_mm": previous.transverse_axis_mm,
                    "chosen_axis_mm": candidate.segments[0].start_mm[1 if str(previous.direction).endswith("X") else 0],
                    "longitudinal_start_shift_mm": (candidate.segments[0].start_mm[
                        0 if str(previous.direction).endswith("X") else 1]-previous.installed_interval_mm[0]
                        if candidate.shape_kind == "straight" else None),
                    "original_true_cut_length_mm": previous.installed_length_mm, "pass": iteration+1})
                changed_this_pass = True
                break
            if exhausted:
                break
        if exhausted or not changed_this_pass:
            break
    policy = ("global-original-FE-fixed-stock-XY-shift-exterior-U/research-v1" if maximum_longitudinal_shift_mm
              else "global-original-FE-fixed-stock-q-shift-exterior-U/research-v1")
    return tuple(current[_key(b)] for b in before), {"policy": policy,
        "maximum_longitudinal_shift_mm": maximum_longitudinal_shift_mm,
        "candidate_checks": checked, "candidate_axes_truncated": truncated,
        "budget_exhausted": exhausted, "search_runtime_s": perf_counter()-started,
        "passes": passes, "operations": operations, "rejections": dict(reasons),
        "initial_3D_collisions": initial_pairs,
        "global_optimality_proven": False, "source_field_transferred": False,
        "old_owner_positive_fragments_frozen": False, "placement_eligible": False}
