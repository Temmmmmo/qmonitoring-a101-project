"""Independent deletion-only cleanup proof for an already trimmed physical batch.

Neither equal missing-cell counts nor equal covered areas imply non-regression:
the checker preserves the GEOMETRIC covered subsets of every original FE level,
separately for steel presence and the existing actual-main-leg control40d policy.
Source owners may occur in several trimmed pieces or disappear with explicit
provenance. This is not the legacy once-only physical normalization certificate.
"""
from __future__ import annotations

import math

from shapely.geometry import Polygon
from shapely.ops import unary_union

from rebar.models import Axis
from .opening_relocation import lane_map
from .shaped_collisions import check_shaped_collisions
from .shaped_fe_repair import ACTUAL_CORE_SERVICE
from .shaped_geometry import check_shaped_host, shaped_batch_metrics, shaped_cutting_schedule
from .shaped_global_coverage import _offers, _problem, _strict_coverage
from .stock_cutting import check_stock_cutting
from .tz_boundary_trim import geometry_presence_offers, trimming_domain

POLICY = "trimmed-deletion-only-preserve-original-FE-covered-geometry/research-v1"


def _key(bar):
    return bar.direction, bar.id


def _bounded_time(value, name, upper=60):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not .001 <= value <= upper):
        raise ValueError(f"{name} must be a finite number in .001..{upper}")


def _validate_inputs(bars, lanes, problem, actual_host):
    _problem(problem)
    sources = lane_map(lanes)
    shaped_batch_metrics(bars)  # immutable, finite, canonical full geometry and unique physical IDs
    domain = trimming_domain(actual_host, respect_openings=True)
    for bar in bars:
        q = bar.segments[0].start_mm[1 if bar.direction.axis is Axis.X else 0]
        for owner in bar.source_bar_ids:
            lane = sources.get((bar.direction, owner))
            if lane is None:
                raise ValueError("Unknown original source owner in trimmed physical batch")
            if (bar.steel_class != lane.source.steel_class or bar.diameter_mm < lane.source.diameter_mm
                    or not lane.axis_window_mm[0] <= q <= lane.axis_window_mm[1]):
                raise ValueError("Original source material, diameter and finite service window required")
        if check_shaped_host(bar, domain)["status"] != "pass":
            raise ValueError("Cleanup requires a full-body host-valid input with all measured holes retained")
    return sources, domain


def _all_offers(bars, sources):
    return {"geometric_presence": geometry_presence_offers(bars, sources),
            "control_40d": _offers(bars, sources, longitudinal_service_policy=ACTUAL_CORE_SERVICE)}


def _demand_regions(problem):
    """Original FE polygons grouped only by direction and exact requested recipe."""
    result = {}
    for original in problem.direction_problems:
        demand = original.demand
        for level in demand.levels:
            if not level.recipe.additions:
                continue
            if len(level.recipe.additions) != 1:
                raise ValueError("Cleanup requires the explicit single-addition source coverage policy")
            spec = level.recipe.additions[0]
            polygons = [Polygon(cell.poly) for cell in demand.cells if cell.level_index == level.index]
            if polygons:
                result[demand.direction, level.index] = (spec.diameter, spec.step, unary_union(polygons))
    return result


def _covered_regions(offers, demanded):
    result = {}
    for policy, directional in offers.items():
        for (direction, level), (diameter, step, geometry) in demanded.items():
            sufficient = unary_union([shape for d, s, shape in directional.get(direction, ())
                                      if d >= diameter and s <= step])
            result[policy, direction, level] = geometry.intersection(sufficient)
    return result


def _lost_regions(before_regions, after_regions):
    rows = []
    for (policy, direction, level), before in before_regions.items():
        lost = before.difference(after_regions[policy, direction, level])
        if lost.area > 0:  # no absolute/relative positive-area tolerance
            rows.append({"policy": policy, "direction": str(direction), "level_index": level,
                         "lost_previously_covered_area_mm2": lost.area})
    return rows


def _stock(bars, time_limit_s):
    schedule = shaped_cutting_schedule(bars)
    if not bars:
        return {"status": "not_checked", "reason": "empty_physical_inventory", "groups": []}
    if len(schedule) > 512:
        return {"status": "not_checked", "reason": "cutting_position_budget_exceeded", "groups": []}
    return check_stock_cutting(schedule, time_limit_s=time_limit_s)


def _pair_keys(report, name):
    return {tuple(sorted((pair[item]["direction"], pair[item]["bar_id"]) for item in ("first", "second")))
            for pair in report[name]}


def check_trimmed_length_cleanup(before, after, lanes, problem, actual_host, *, stock_time_limit_s=10):
    """Recompute the complete before/after; no solver claims or cached FE metrics.

Only exact unchanged input records may remain. All removed bar IDs/source-owner
labels stay in the explicit mapping. Acceptance is non-regression, NOT full FE
coverage/zero collisions/zero waste. A prior stock PASS may not become non-PASS.
"""
    _bounded_time(stock_time_limit_s, "stock_time_limit_s")
    sources, domain = _validate_inputs(before, lanes, problem, actual_host)
    shaped_batch_metrics(after)
    original = {_key(bar): bar for bar in before}
    if any(original.get(_key(bar)) != bar for bar in after):
        raise ValueError("Deletion-only cleanup cannot change geometry, diameter, Z, material, owners or IDs")
    retained = {_key(bar) for bar in after}
    # Independent final whole-body pass, not an axis/bbox or a hole-blind check.
    if any(check_shaped_host(bar, domain)["status"] != "pass" for bar in after):
        raise ValueError("Retained physical bodies must fit the exact host with openings")
    old_offers, new_offers = _all_offers(before, sources), _all_offers(after, sources)
    demanded = _demand_regions(problem)
    losses = _lost_regions(_covered_regions(old_offers, demanded), _covered_regions(new_offers, demanded))
    if losses:
        raise ValueError(f"Cleanup loses previously covered original FE geometry: {losses[:4]}")
    coverage_before = {name: _strict_coverage(problem, offered) for name, offered in old_offers.items()}
    coverage_after = {name: _strict_coverage(problem, offered) for name, offered in new_offers.items()}
    collisions_before, collisions_after = check_shaped_collisions(before), check_shaped_collisions(after)
    for name in ("proven_collision_pairs", "uncertain_pairs"):
        if not _pair_keys(collisions_after, name) <= _pair_keys(collisions_before, name):
            raise ValueError("Cleanup introduced a new 3D collision or uncertain body pair")
    stock_before = _stock(before, stock_time_limit_s)
    stock_after = _stock(after, stock_time_limit_s)
    stock_preserved = stock_before["status"] != "pass" or stock_after["status"] == "pass"
    removed = [bar for bar in before if _key(bar) not in retained]
    return {"schema_version": "trimmed-length-cleanup-check/v1", "policy": POLICY, "units": "mm",
        "accepted_nonregression": stock_preserved,
        "status": "nonregression_checked_not_engineering_approved" if stock_preserved else "rejected_stock_regression",
        "coverage_before": coverage_before, "coverage_after": coverage_after,
        "previously_covered_geometry_lost": [], "positive_area_loss_tolerance_mm2": 0,
        "stock_cutting_before": stock_before, "stock_cutting": stock_after,
        "prior_stock_pass_preserved": stock_preserved,
        "collisions_before": collisions_before, "collisions": collisions_after,
        "physical_metrics_before": shaped_batch_metrics(before), "physical_metrics": shaped_batch_metrics(after),
        "removed_bar_count": len(removed),
        "piece_mapping": [{"direction": str(bar.direction), "input_bar_id": bar.id,
            "output_bar_ids": [bar.id] if _key(bar) in retained else [], "source_bar_ids": list(bar.source_bar_ids),
            "reason": "unchanged" if _key(bar) in retained else "redundant_for_both_original_FE_coverage_policies"}
            for bar in before],
        "retained_geometry_exactly_unchanged": True, "material_boundary_failures_after": 0,
        "openings_retained": True, "concrete_cover_included": False,
        "original_FE_geometry_changed": False, "source_demand_removed": False,
        "weak_As_summation": False, "old_once_only_owner_certificate_reused": False,
        "manufacturing_length_tolerance_applied_mm": 0, "lengths_rounded_or_extended": False,
        "placement_eligible": False, "engineering_approval": False, "all_TZ_requirements_certified": False,
        "not_checked": ["actual_existing_background_3D_inventory", "engineering_anchorage_capacity",
                        "Revit_creation_and_readback", "manufacturing_cut_tolerances"]}
