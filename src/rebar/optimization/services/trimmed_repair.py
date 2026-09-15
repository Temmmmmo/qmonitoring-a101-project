"""Independent proof for rebuilding a cut batch, not a relaxed cutting certificate.

Physical replacements may have new lengths and one-to-many source ownership.
Finite transverse service widths are still reconstructed from the source lanes.
Original FE, both coverage models, host and the complete stock batch are re-read.
"""
from __future__ import annotations

from collections import defaultdict
import math

from shapely.geometry import Polygon
from shapely.ops import unary_union

from rebar.models import Axis
from .opening_relocation import lane_map
from .shaped_collisions import check_shaped_collisions
from .shaped_fe_repair import ACTUAL_CORE_SERVICE, ResearchLayerProfile, layer_elevations
from .shaped_geometry import check_shaped_host, shaped_batch_metrics, shaped_cut_length_mm, shaped_cutting_schedule
from .shaped_global_coverage import _offers, _problem, _strict_coverage
from .stock_cutting import check_stock_cutting
from .tz_boundary_trim import geometry_presence_offers, trimming_domain

POLICY = "rebuild-cut-bars-original-FE-finite-lanes/separate-40d/v1"


def positive_area_geometry(shape):
    """Keep exact polygon components; boundary-only lines carry no FE area.

Do not buffer/snap the source. Mixed polygon/line collections produced by exact
boundary contacts can fail a subsequent GEOS overlay even with valid polygons.
"""
    if isinstance(shape, Polygon):
        return shape
    polygons = []
    for child in getattr(shape, "geoms", ()):
        value = positive_area_geometry(child)
        if not value.is_empty:
            polygons.append(value)
    return unary_union(polygons)


def demand_regions(problem):
    """Group ORIGINAL demanded polygons by direction and sufficient recipe."""
    _problem(problem)
    groups = defaultdict(list)
    for original in problem.direction_problems:
        for cell in original.demand.cells:
            recipe = original.demand.level(cell.level_index).recipe
            if not recipe.additions:
                continue
            if len(recipe.additions) != 1:
                raise ValueError("Explicit single-addition source recipe required")
            spec = recipe.additions[0]
            groups[original.demand.direction, spec.diameter, spec.step].append(Polygon(cell.poly))
    return {key: unary_union(polygons) for key, polygons in groups.items()}


def offered_regions(regions, offers):
    """Only independently sufficient offers contribute; weak As never sums."""
    return {key: positive_area_geometry(region.intersection(unary_union([
        p for d, s, p in offers.get(key[0], ()) if d >= key[1] and s <= key[2]])))
        for key, region in regions.items()}


def batch_regions(bars, sources, regions, *, anchored=False):
    offers = (_offers(bars, sources, longitudinal_service_policy=ACTUAL_CORE_SERVICE)
              if anchored else geometry_presence_offers(bars, sources))
    return offered_regions(regions, offers)


def validate_rebuilt_bars(bars, sources, actual_host):
    """Validate data and declared source service, without assuming once-only owners."""
    if not isinstance(bars, tuple) or not 1 <= len(bars) <= 10000:
        raise ValueError("Complete bounded nonempty physical batch required")
    identities = set()
    for bar in bars:
        length = shaped_cut_length_mm(bar)
        if bar.shape_kind != "straight" or length > 11700 + 1e-7:
            raise ValueError("Straight physical lengths at most 11700 mm required")
        key = bar.direction, bar.id
        if key in identities:
            raise ValueError("Unique physical identities required")
        identities.add(key)
        along = 0 if bar.direction.axis is Axis.X else 1
        if bar.segments[0].start_mm[along] >= bar.segments[0].end_mm[along]:
            raise ValueError("Canonical increasing physical interval required")
        q = bar.segments[0].start_mm[1-along]
        for owner in bar.source_bar_ids:
            lane = sources.get((bar.direction, owner))
            if lane is None:
                raise ValueError("Unknown original source owner")
            if (bar.steel_class != lane.source.steel_class or bar.diameter_mm < lane.source.diameter_mm
                    or not lane.axis_window_mm[0] <= q <= lane.axis_window_mm[1]):
                raise ValueError("Source material, diameter or finite transverse window violated")
    domain = trimming_domain(actual_host, respect_openings=True)
    failures = [{"direction": str(b.direction), "bar_id": b.id}
                for b in bars if check_shaped_host(b, domain)["status"] != "pass"]
    return failures


def check_rebuilt_trimmed_bars(before, after, lanes, problem, actual_host, *, stock_time_limit_s=10):
    """No caller-provided coverage/host/stock pass and no hidden FE deletion."""
    if (isinstance(stock_time_limit_s, bool) or not isinstance(stock_time_limit_s, (int, float))
            or not math.isfinite(stock_time_limit_s) or not 0 < stock_time_limit_s <= 60):
        raise ValueError("Finite stock verification budget in (0, 60] required")
    sources, regions = lane_map(lanes), demand_regions(problem)
    validate_rebuilt_bars(before, sources, actual_host)
    host_failures = validate_rebuilt_bars(after, sources, actual_host)
    before_by_id = {(b.direction, b.id): b for b in before}
    new = [b for b in after if before_by_id.get((b.direction, b.id)) != b]
    # The reconstruction never changes source-prescribed background axes.
    for bar in new:
        across = 1 if bar.direction.axis is Axis.X else 0
        q = bar.segments[0].start_mm[across]
        if bar.segments[0].start_mm[2] != layer_elevations(
                actual_host, bar.direction, bar.diameter_mm, ResearchLayerProfile())[0]:
            raise ValueError("New bar must retain the declared whole-plate research layer")
        for owner in bar.source_bar_ids:
            source = sources[bar.direction, owner].source
            if abs(math.remainder(q-source.background_origin_mm, source.background_step_mm)) < (
                    bar.diameter_mm+source.background_diameter_mm)/2:
                raise ValueError("New bar penetrates a source-prescribed background axis")
    regressions = {}
    for label, anchored in (("geometric_presence", False), ("control_40d", True)):
        old = batch_regions(before, sources, regions, anchored=anchored)
        current = batch_regions(after, sources, regions, anchored=anchored)
        regressions[label] = math.fsum(old[k].difference(current[k]).area for k in regions)
    presence = _strict_coverage(problem, geometry_presence_offers(after, sources))
    anchor = _strict_coverage(problem, _offers(after, sources, longitudinal_service_policy=ACTUAL_CORE_SERVICE))
    collisions = check_shaped_collisions(after)
    stock = check_stock_cutting(shaped_cutting_schedule(after), time_limit_s=stock_time_limit_s)
    old_keys, new_keys = set(before_by_id), {(b.direction, b.id) for b in after}
    blockers = [name for name, bad in (
        ("material_boundary", bool(host_failures)), ("coverage_regression", any(regressions.values())),
        ("original_FE_geometric_presence", presence["status"] != "pass"),
        ("original_FE_control_40d", anchor["status"] != "pass"),
        ("additional_3D_collisions", collisions["status"] != "pass"),
        ("11700_zero_waste_cutting", stock["status"] != "pass")) if bad]
    return {"schema_version": "trimmed-zone-rebuild-check/v1", "policy": POLICY, "units": "mm",
        "before_metrics": shaped_batch_metrics(before), "physical_metrics": shaped_batch_metrics(after),
        "host_failures": host_failures, "material_boundary_failures_after": len(host_failures),
        "geometric_presence": presence, "coverage_with_control_40d": anchor,
        "previously_covered_area_lost_mm2": regressions, "collisions": collisions, "stock_cutting": stock,
        "added_bar_ids": sorted((str(d), i) for d, i in new_keys-old_keys),
        "removed_bar_ids": sorted((str(d), i) for d, i in old_keys-new_keys),
        "source_ownership": [{"direction": str(b.direction), "bar_id": b.id,
                              "source_bar_ids": list(b.source_bar_ids)} for b in after],
        "blockers": blockers, "status": "requires_review" if blockers else "checks_pass_research_profile",
        "original_FE_geometry_changed": False, "source_demand_removed": False,
        "weak_As_summation": False, "concrete_cover_checked": False, "openings_checked": True,
        "placement_profile_id": ResearchLayerProfile().id, "placement_profile_measured_in_Revit": False,
        "existing_Revit_rebar_checked": False, "placement_eligible": False, "engineering_approval": False}
