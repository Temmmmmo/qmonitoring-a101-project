"""Explicit user-selected TZ scope, NOT a replacement Revit host certificate.

Closed holes and concrete cover are excluded from this task's failure criteria.
Outer recesses, complete bar bodies, original demand and collisions are retained.
The supplied real host is immutable; the derived domain is never a measured RVT.
"""
from __future__ import annotations

from dataclasses import replace
import math

from shapely.geometry import Polygon
from shapely.ops import unary_union

from rebar.models import Axis
from ..contracts.physical import PhysicalBar
from ..contracts.shaped_physical import Line3D
from .opening_relocation import lane_map
from .physical_host_fit import _host_intervals
from .shaped_collisions import check_shaped_collisions
from .shaped_geometry import (
    _validate_host, check_shaped_host, shaped_batch_metrics, shaped_cut_length_mm,
    shaped_cutting_schedule,
)
from .shaped_global_coverage import _offers, _problem, _shape_batch, _strict_coverage
from .stock_cutting import check_stock_cutting

TZ_OUTER_SCOPE = "user-TZ-outer-boundary-no-openings-no-cover/2026-09-15-v1"


def outer_scope_domain(actual_host):
    """Derived search/check domain only. Keep concavities and height changes."""
    _validate_host(actual_host)
    sections = []
    for section in actual_host.sections:
        parts = ([section.footprint] if isinstance(section.footprint, Polygon)
                 else section.footprint.geoms)
        outer = unary_union([Polygon(part.exterior) for part in parts])
        sections.append(replace(section, footprint=outer))
    volume = math.fsum(s.footprint.area*(s.top_z_mm-s.bottom_z_mm) for s in sections)
    return replace(actual_host, sections=tuple(sections), top_cover_mm=0.,
                   bottom_cover_mm=0., side_cover_mm=0., volume_mm3=volume)


def check_tz_outer_bar(bar, actual_host):
    checked = check_shaped_host(bar, outer_scope_domain(actual_host))
    return {
        "policy": TZ_OUTER_SCOPE, "direction": str(bar.direction), "bar_id": bar.id,
        "status": checked["status"],
        "whole_body_inside_outer_boundary": checked["whole_body_with_cover_contained"],
        "failed_body_enclosures": checked["failed_chords"],
        "openings_checked": False, "concrete_cover_checked": False,
        "outer_recesses_preserved": True, "height_sections_preserved": True,
        "actual_Revit_host_pass_claimed": False, "placement_eligible": False,
    }


def translate_straight_whole(bar, *, start_mm, transverse_axis_mm=None):
    """Rigid XY translation: no shortening, new bends, rotations, or Z change."""
    shaped_cut_length_mm(bar)
    if bar.shape_kind != "straight" or len(bar.segments) != 1:
        raise ValueError("Only a complete straight bar may be translated here")
    along = 0 if bar.direction.axis is Axis.X else 1
    across = 1-along
    old = bar.segments[0]
    q = old.start_mm[across] if transverse_axis_mm is None else transverse_axis_mm
    if any(isinstance(v, bool) or not isinstance(v, (int, float))
           or not math.isfinite(v) or abs(v) > 1e8 for v in (start_mm, q)):
        raise ValueError("Finite bounded coordinates required")
    if old.end_mm[along] <= old.start_mm[along]:
        raise ValueError("Canonical increasing straight interval required")
    shift = start_mm-old.start_mm[along]
    a, b = list(old.start_mm), list(old.end_mm)
    a[along], b[along] = start_mm, old.end_mm[along]+shift
    a[across] = b[across] = q
    return replace(bar, segments=(Line3D(tuple(a), tuple(b)),))


def outer_start_intervals(bar, actual_host, *, transverse_axis_mm=None):
    """Exact whole-length longitudinal fit BEFORE any FE/40d restriction.

    Uses the complete body-height interval, not one global bbox or one sampled
    endpoint. Nothing outside the external outline is clipped off the bar.
    """
    shaped_cut_length_mm(bar)
    if bar.shape_kind != "straight" or len(bar.segments) != 1:
        raise ValueError("Straight bar required for longitudinal intervals")
    along = 0 if bar.direction.axis is Axis.X else 1
    a, b = bar.segments[0].start_mm, bar.segments[0].end_mm
    q = a[1-along] if transverse_axis_mm is None else transverse_axis_mm
    moved = translate_straight_whole(bar, start_mm=a[along], transverse_axis_mm=q)
    domain = outer_scope_domain(actual_host)
    loz, hiz = a[2]-bar.diameter_mm/2, a[2]+bar.diameter_mm/2
    if loz < domain.sections[0].bottom_z_mm or hiz > domain.sections[-1].top_z_mm:
        return ()
    selected = [s.footprint for s in domain.sections
                if min(hiz, s.top_z_mm) > max(loz, s.bottom_z_mm)]
    if not selected:
        return ()
    material = selected[0]
    for shape in selected[1:]:
        material = material.intersection(shape)
    proxy = PhysicalBar(moved.id, moved.direction, moved.steel_class, moved.diameter_mm,
        q, (a[along], b[along]), moved.source_bar_ids)
    return _host_intervals(proxy, material, 0., 4096)


def check_tz_outer_batch(before, after, lanes, problem, actual_host, *, stock_time_limit_s=30):
    """Independent report; excluded host criteria CANNOT leak into TZ status.

    A geometric trial is allowed to fail FE/collision checks, but never labelled
    accepted. Only rigid changes of existing straight bars are supported.
    """
    _problem(problem)
    sources = lane_map(lanes)
    _shape_batch(before, sources)
    _shape_batch(after, sources)
    original = {(b.direction, b.id): b for b in before}
    if len(after) != len(before) or {(b.direction, b.id) for b in after} != set(original):
        raise ValueError("Same complete physical bar identities required")
    changed = []
    for bar in after:
        old = original[bar.direction, bar.id]
        if bar == old:
            continue
        along = 0 if bar.direction.axis is Axis.X else 1
        canonical = translate_straight_whole(old,
            start_mm=bar.segments[0].start_mm[along],
            transverse_axis_mm=bar.segments[0].start_mm[1-along])
        if bar != canonical or abs(shaped_cut_length_mm(old)-shaped_cut_length_mm(bar)) > 1e-7:
            raise ValueError("Rigid whole-bar translation required; no cut/shape/material change")
        changed.append({"direction": str(bar.direction), "bar_id": bar.id,
            "longitudinal_shift_mm": bar.segments[0].start_mm[along]-old.segments[0].start_mm[along],
            "transverse_shift_mm": bar.segments[0].start_mm[1-along]-old.segments[0].start_mm[1-along]})
    before_outer = [check_tz_outer_bar(b, actual_host) for b in before]
    after_outer = [check_tz_outer_bar(b, actual_host) for b in after]
    failures = [r for r in after_outer if r["status"] != "pass"]
    coverage = _strict_coverage(problem, _offers(after, sources))
    pairs = check_shaped_collisions(after)
    background_failures = []
    minimum_gap = math.inf
    for bar in after:
        across = 1 if bar.direction.axis is Axis.X else 0
        q = bar.segments[0].start_mm[across]
        for owner in bar.source_bar_ids:
            source = sources[bar.direction, owner].source
            gap = abs(math.remainder(q-source.background_origin_mm, source.background_step_mm)) - (
                bar.diameter_mm+source.background_diameter_mm)/2
            minimum_gap = min(minimum_gap, gap)
            if gap < 0:
                background_failures.append({"direction": str(bar.direction), "bar_id": bar.id,
                    "source_owner": owner, "gap_mm": gap})
    stock = check_stock_cutting(shaped_cutting_schedule(after), time_limit_s=stock_time_limit_s)
    blockers = []
    for condition, label in (
        (bool(failures), "external_boundary"),
        (coverage["status"] != "pass", "original_FE_coverage_with_retained_40d"),
        (bool(pairs["proven_collision_pair_count"]), "additional_bar_3d_collisions"),
        (bool(pairs["uncertain_pair_count"]), "additional_bar_3d_separation_unproven"),
        (bool(background_failures), "source_prescribed_background_axis_collision"),
        (stock["status"] != "pass", "11700_cutting"),
    ):
        if condition:
            blockers.append(label)
    return {
        "schema_version": "tz-outer-scope-check/v1", "policy": TZ_OUTER_SCOPE,
        "checked_gates_status": "fail" if blockers else "pass", "tz_blockers": blockers,
        "excluded_from_TZ": ["closed_openings", "concrete_cover"],
        "excluded_criteria_reported_as_failures": False,
        "external_boundary_failures_before": sum(r["status"] != "pass" for r in before_outer),
        "external_boundary_failures_after": len(failures), "external_boundary_failures": failures,
        "changes": changed, "changed_bar_count": len(changed), "whole_lengths_preserved": True,
        "source_coverage": coverage, "collisions": pairs, "stock_cutting": stock,
        "source_prescribed_background_axis_failures": background_failures,
        "minimum_source_prescribed_background_gap_mm": minimum_gap,
        "physical_metrics": shaped_batch_metrics(after),
        "original_FE_geometry_changed": False, "source_demand_removed": False,
        "concrete_cover_values_in_actual_snapshot": {"top": actual_host.top_cover_mm,
            "bottom": actual_host.bottom_cover_mm, "side": actual_host.side_cover_mm},
        "all_TZ_requirements_certified": False,
        "not_checked": ["regrouped_rectangular_zone_and_STO_phase_certificates",
                        "bent_anchorage_capacity", "existing_Revit_rebar_inventory", "Revit_readback"],
        "placement_eligible": False, "engineering_approval": False,
    }
