"""Physical outer-edge cuts, explicitly NOT an anchorage waiver.

Each straight input is intersected with complete body-width material runs at
its actual height. A concave external recess can produce several physical
pieces. No positive piece is silently removed, and bent bars are never broken
into pretend straight segments. Original demand is checked without cropping.
"""
from __future__ import annotations

from dataclasses import replace
import math

from shapely.geometry import Polygon, box

from rebar.models import Axis
from ..contracts.physical import PhysicalBar
from ..contracts.shaped_physical import Line3D
from .opening_relocation import lane_map, service_boxes
from .shaped_collisions import check_shaped_collisions
from .shaped_fe_repair import ACTUAL_CORE_SERVICE
from .shaped_geometry import (
    check_shaped_host, main_horizontal_interval_mm, shaped_batch_metrics,
    shaped_cut_length_mm, shaped_cutting_schedule,
)
from .shaped_global_coverage import _offers, _problem, _shape_batch, _strict_coverage
from .stock_cutting import check_stock_cutting
from .tz_outer_scope import check_tz_outer_bar, outer_scope_domain, translate_straight_whole

POLICY = "user-physical-outer-boundary-trim-and-split/2026-09-15-v1"


def _polygons(shape):
    if isinstance(shape, Polygon):
        if not shape.is_empty:
            yield shape
    elif hasattr(shape, "geoms"):
        for child in shape.geoms:
            yield from _polygons(child)


def straight_outer_intersections(bar, actual_host):
    """All positive retained intervals, using full diameter, not centerline only."""
    shaped_cut_length_mm(bar)
    if bar.shape_kind != "straight":
        raise ValueError("Only a straight bar can be shortened by the outer boundary")
    along = 0 if bar.direction.axis is Axis.X else 1
    a, b = bar.segments[0].start_mm, bar.segments[0].end_mm
    if a[along] >= b[along]:
        raise ValueError("Canonical increasing straight interval required")
    domain = outer_scope_domain(actual_host)
    radius = bar.diameter_mm/2
    low_z, high_z = a[2]-radius, a[2]+radius
    if low_z < domain.sections[0].bottom_z_mm or high_z > domain.sections[-1].top_z_mm:
        return ()
    sections = [s.footprint for s in domain.sections
                if min(high_z, s.top_z_mm) > max(low_z, s.bottom_z_mm)]
    if not sections:
        return ()
    material = sections[0]
    for shape in sections[1:]:
        material = material.intersection(shape)
    left, right = a[along], b[along]
    q = a[1-along]
    strip = box(left, q-radius, right, q+radius) if along == 0 else box(q-radius, left, q+radius, right)
    excluded = sorted((p.bounds[along], p.bounds[along+2]) for p in _polygons(strip.difference(material))
                      if p.area > 0)
    if len(excluded) > 4096:
        raise ValueError("Outer interval budget exceeded; no partial trimming result")
    kept, cursor = [], left
    for low, high in excluded:
        if low > cursor:
            kept.append((cursor, low))
        cursor = max(cursor, high)
    if cursor < right:
        kept.append((cursor, right))
    return tuple(kept)


def build_trimmed_pieces(bar, intervals):
    """Construct actual new lengths; preservation/checking belongs to the caller."""
    shaped_cut_length_mm(bar)
    if bar.shape_kind != "straight" or not isinstance(intervals, tuple) or len(intervals) > 4096:
        raise ValueError("Bounded explicit straight intervals required")
    along = 0 if bar.direction.axis is Axis.X else 1
    segment = bar.segments[0]
    start, end = segment.start_mm[along], segment.end_mm[along]
    previous, result = start, []
    for index, pair in enumerate(intervals):
        if (not isinstance(pair, tuple) or len(pair) != 2
                or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in pair)
                or not previous <= pair[0] < pair[1] <= end):
            raise ValueError("Ordered positive nonoverlapping intervals inside the original bar required")
        a, b = list(segment.start_mm), list(segment.end_mm)
        a[along], b[along] = pair
        identifier = bar.id if len(intervals) == 1 else f"{bar.id}/outer-piece-{index+1}"
        piece = replace(bar, id=identifier, segments=(Line3D(tuple(a), tuple(b)),),
                        selected_cut_length_mm=pair[1]-pair[0])
        shaped_cut_length_mm(piece)
        result.append(piece)
        previous = pair[1]
    return tuple(result)


def trimming_parent(bar, actual_host, sources, *, nudge_edge_axis=False):
    """Optional radius-sized inward nudge for an axis lying ON an outer edge.

    This is not an arbitrary phase search. Only +/-radius is tried when the
    original straight bar has NO retained full-body interval at all. Original
    finite service windows and background clearances are retained. All FE and
    pair checks remain mandatory on the eventual party.
    """
    if type(nudge_edge_axis) is not bool:
        raise ValueError("Explicit boolean edge-axis nudge policy required")
    if not nudge_edge_axis or bar.shape_kind != "straight" or straight_outer_intersections(bar, actual_host):
        return bar
    along = 0 if bar.direction.axis is Axis.X else 1
    q = bar.segments[0].start_mm[1-along]
    for offset in (-bar.diameter_mm/2, bar.diameter_mm/2):
        trial_q = q+offset
        if any(not sources[bar.direction, owner].axis_window_mm[0] <= trial_q <=
               sources[bar.direction, owner].axis_window_mm[1] for owner in bar.source_bar_ids):
            continue
        if any(abs(math.remainder(trial_q-sources[bar.direction, owner].source.background_origin_mm,
                    sources[bar.direction, owner].source.background_step_mm)) <
               (bar.diameter_mm+sources[bar.direction, owner].source.background_diameter_mm)/2
               for owner in bar.source_bar_ids):
            continue
        candidate = translate_straight_whole(bar, start_mm=bar.segments[0].start_mm[along], transverse_axis_mm=trial_q)
        if straight_outer_intersections(candidate, actual_host):
            return candidate
    return bar


def geometry_presence_offers(bars, sources):
    """Steel present in finite source service lanes, NOT effective anchored As."""
    result = {}
    for bar in bars:
        along = 0 if bar.direction.axis is Axis.X else 1
        interval = main_horizontal_interval_mm(bar)
        q = bar.segments[0].start_mm[1-along]
        proxy = PhysicalBar(bar.id, bar.direction, bar.steel_class, bar.diameter_mm,
                            q, interval, bar.source_bar_ids)
        for diameter, step, original in service_boxes(proxy, sources):
            bounds = list(original.bounds)
            bounds[along], bounds[along+2] = interval
            result.setdefault(bar.direction, []).append((diameter, step, box(*bounds)))
    return result


def check_boundary_trim(before, after, piece_mapping, lanes, problem, actual_host, *, stock_time_limit_s=30,
                        nudge_edge_axis=False, discard_empty_intersections=False):
    """Rebuild every authorized cut and freshly check the complete physical party.

    One-to-many owners are explicit source provenance, not additive weak As.
    Empty intersections are retained by default. Explicit opt-in removes them
    from the NEW physical result, with complete original-to-empty provenance;
    ORIGINAL FE stays intact and must be checked even if every bar is removed.
    """
    _problem(problem)
    sources = lane_map(lanes)
    _shape_batch(before, sources)
    if type(discard_empty_intersections) is not bool:
        raise ValueError("Explicit boolean empty-intersection policy required")
    if (not isinstance(after, tuple) or len(after) > 10000
            or not isinstance(piece_mapping, tuple) or len(piece_mapping) != len(before)):
        raise ValueError("Complete immutable trimmed inventory and one mapping per original bar required")
    if (isinstance(stock_time_limit_s, bool) or not isinstance(stock_time_limit_s, (int, float))
            or not math.isfinite(stock_time_limit_s) or not 0 < stock_time_limit_s <= 60):
        raise ValueError("Finite bounded stock verification time required")
    keyed = {(str(b.direction), b.id): b for b in after}
    if len(keyed) != len(after):
        raise ValueError("Unique physical piece identities required")
    seen, changes, unresolved, removed = set(), [], [], []
    for old, row in zip(before, piece_mapping):
        if row.get("direction") != str(old.direction) or row.get("source_bar_id") != old.id:
            raise ValueError("Each input bar must have its exact provenance mapping")
        parent = trimming_parent(old, actual_host, sources, nudge_edge_axis=nudge_edge_axis)
        if parent.shape_kind == "straight":
            intervals = straight_outer_intersections(parent, actual_host)
            expected = (build_trimmed_pieces(parent, intervals) if intervals or discard_empty_intersections
                        else (parent,))
        else:
            expected = (old,)
        keys = tuple((str(b.direction), b.id) for b in expected)
        if row.get("piece_ids") != [b.id for b in expected] or any(k in seen for k in keys):
            raise ValueError("Every physical piece must be mapped exactly once")
        if any(keyed.get(k) != bar for k, bar in zip(keys, expected)):
            raise ValueError("Actual geometry/material/length differs from the exact authorized boundary cut")
        seen.update(keys)
        if not expected:
            removed.append({"direction": str(old.direction), "bar_id": old.id,
                            "reason": "no_whole_body_intersection"})
        if expected != (old,):
            changes.append({"direction": str(old.direction), "bar_id": old.id,
                "before_length_mm": shaped_cut_length_mm(old), "piece_ids": [b.id for b in expected],
                "after_lengths_mm": [shaped_cut_length_mm(b) for b in expected],
                "transverse_shift_mm": parent.segments[0].start_mm[1 if old.direction.axis is Axis.X else 0]
                    - old.segments[0].start_mm[1 if old.direction.axis is Axis.X else 0],
                "removed_length_mm": shaped_cut_length_mm(old)-math.fsum(shaped_cut_length_mm(b) for b in expected)})
        for bar in expected:
            if check_tz_outer_bar(bar, actual_host)["status"] != "pass":
                unresolved.append({"direction": str(bar.direction), "bar_id": bar.id,
                    "reason": "no_whole_body_intersection" if bar.shape_kind == "straight" else "bent_shape_not_trimmed"})
    if seen != set(keyed):
        raise ValueError("Unmapped extra pieces are not allowed")
    presence = _strict_coverage(problem, geometry_presence_offers(after, sources))
    presence.update(policy="physical-main-leg-presence-NOT-anchorage", anchorage_checked=False,
                    engineering_coverage_certified=False)
    anchored = _strict_coverage(problem, _offers(after, sources, longitudinal_service_policy=ACTUAL_CORE_SERVICE))
    anchored.update(straight_end_control_anchor_diameters=40, arc_and_return_demand_credit=False,
                    bent_anchorage_capacity="not_checked")
    pairs = check_shaped_collisions(after)
    schedule = shaped_cutting_schedule(after)
    stock = ({"status": "not_checked", "reason": "empty_physical_inventory", "groups": []} if not after else
             check_stock_cutting(schedule, time_limit_s=stock_time_limit_s) if len(schedule) <= 512 else
             {"status": "not_checked", "reason": "cutting_position_budget_exceeded", "position_count": len(schedule)})
    before_metrics, after_metrics = shaped_batch_metrics(before), shaped_batch_metrics(after)
    blockers = []
    for failed, label in (
        (bool(unresolved), "external_boundary"),
        (presence["status"] != "pass", "original_FE_geometric_presence"),
        (anchored["status"] != "pass", "original_FE_with_control_40d"),
        (bool(pairs["proven_collision_pair_count"]), "additional_3D_collisions"),
        (bool(pairs["uncertain_pair_count"]), "additional_3D_separation_unproven"),
        (stock["status"] != "pass", "11700_zero_waste_cutting"),
    ):
        if failed:
            blockers.append(label)
    return {"schema_version": "tz-boundary-trim-check/v1", "policy": POLICY, "units": "mm",
        "status": "requires_engineering_review" if after else "empty_physical_result", "blockers": blockers,
        "external_boundary_failures_before": sum(check_tz_outer_bar(b, actual_host)["status"] != "pass" for b in before),
        "external_boundary_failures_after": len(unresolved), "unresolved_bars": unresolved,
        "changed_input_bar_count": len(changes), "changes": changes, "piece_mapping": list(piece_mapping),
        "radius_sized_edge_axis_nudge_enabled": nudge_edge_axis,
        **({"discard_empty_intersections": True, "removed_wholly_external_bars": removed,
            "removed_input_bar_count": len(removed)} if discard_empty_intersections else {}),
        "geometric_presence": presence, "coverage_with_control_40d": anchored, "collisions": pairs,
        "stock_cutting": stock, "physical_metrics_before": before_metrics, "physical_metrics": after_metrics,
        "removed_length_mm": before_metrics["true_cut_length_mm"]-after_metrics["true_cut_length_mm"],
        "removed_mass_kg": before_metrics["mass_kg"]-after_metrics["mass_kg"],
        "actual_Revit_host_informational_failures": sum(check_shaped_host(b, actual_host)["status"] != "pass" for b in after),
        "excluded_from_TZ": ["closed_openings", "concrete_cover"], "outer_recesses_preserved": True,
        "source_demand_removed": False, "original_FE_geometry_changed": False, "weak_As_summation": False,
        "cutoff_is_not_a_hidden_display_clip": True, "old_coverage_or_cutting_certificate_reused": False,
        "placement_eligible": False, "engineering_approval": False, "all_TZ_requirements_certified": False,
        "not_checked": ["edge_anchorage_capacity", "manufacturing_cut_length_tolerances", "actual_Revit_readback"]}
