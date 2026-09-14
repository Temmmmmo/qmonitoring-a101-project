"""Independent source-owner proof for an opt-in straight -> exterior U repair.

Only the main horizontal leg serves demand. The outer U node is a declared
engineering assumption, not a calculated anchorage resistance. The opposite
straight end retains full40d. Stock length, diameter, transverse axis and every
original owner's positive FE fragment are frozen in this first experiment.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import math

from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from rebar.models import Axis, Layer
from ..contracts.physical import PhysicalBar
from ..contracts.shaped_physical import K09_U_RETURN_50D_PROFILE, ShapedPhysicalBar
from .collision_replacement import freeze_owner_fe_service
from .opening_relocation import coverage_from_offers, lane_map, service_boxes
from .shaped_geometry import (
    build_u_edge_bar, check_shaped_host, curve_point, main_horizontal_interval_mm,
    shaped_batch_metrics, shaped_cutting_schedule, straight_bar_from_physical,
)
from .stock_cutting import check_stock_cutting

POLICY = "frozen-owner-FE-exterior-U-same-true-stock-length/research-v1"


@dataclass(frozen=True)
class ResearchLayerProfile:
    """One explicit order for BOTH faces; this is not a measured Revit profile."""

    id: str = "X-outer-both-faces-maxD16-zero-XY-clearance/research-v1"
    outer_axis: Axis = Axis.X
    outer_envelope_diameter_mm: int = 16
    orthogonal_clear_gap_mm: float = 0.0


def layer_elevations(host, direction, diameter, profile):
    if (profile != ResearchLayerProfile() or not isinstance(profile.outer_axis, Axis)
            or type(profile.outer_envelope_diameter_mm) is not int
            or isinstance(profile.orthogonal_clear_gap_mm, bool)):
        raise ValueError("Exact explicit whole-plate research layer profile required")
    if type(diameter) is not int or diameter not in (10, 12, 16):
        raise ValueError("Sourced shape profile is limited to D10/D12/D16")
    inset = 0 if direction.axis is profile.outer_axis else (
        profile.outer_envelope_diameter_mm+profile.orthogonal_clear_gap_mm)
    bottom = host.sections[0].bottom_z_mm+host.bottom_cover_mm+inset+diameter/2
    top = host.sections[-1].top_z_mm-host.top_cover_mm-inset-diameter/2
    if bottom >= top:
        raise ValueError("No positive distance between declared face layers")
    return (top, bottom) if direction.layer is Layer.TOP else (bottom, top)


def exterior_edge_choices(host, direction, q, diameter):
    """Real exterior edges only, including recesses; closed holes are excluded.

    Entire transverse cover envelope must lie within a straight exterior edge.
    The subsequent complete 3D host check still has to prove every curve fits.
    """
    material = host.sections[0].footprint
    for section in host.sections[1:]:
        material = material.intersection(section.footprint)
    parts = [material] if isinstance(material, Polygon) else list(material.geoms)
    along = 0 if direction.axis is Axis.X else 1
    across, reserve = 1-along, host.side_cover_mm+diameter/2
    choices = set()
    for part in parts:
        points = list(part.exterior.coords)
        for a, b in zip(points, points[1:]):
            if a[along] != b[along] or a[across] == b[across]:
                continue
            low, high = sorted((a[across], b[across]))
            if not low <= q-reserve < q+reserve <= high:
                continue
            edge = a[along]
            for inward in (-1, 1):
                # A positive thin rectangle disambiguates the inner side; it
                # is not a sampled proof of the eventual physical shape.
                interval = sorted((edge, edge+inward*1e-4))
                bounds = (*interval[:1], q-reserve, interval[1], q+reserve)
                if along == 1:
                    bounds = bounds[1], bounds[0], bounds[3], bounds[2]
                if part.covers(box(*bounds)):
                    choices.add((edge, inward))
    return tuple(sorted(choices))


def shaped_service_offers(bar, sources):
    """Actual main-leg geometry intersected with unchanged finite owner lanes."""
    interval = main_horizontal_interval_mm(bar)
    along = 0 if bar.direction.axis is Axis.X else 1
    across = 1-along
    q = bar.segments[0].start_mm[across]
    low, high = interval
    if bar.shape_kind == "straight":
        low, high = low+40*bar.diameter_mm, high-40*bar.diameter_mm
    elif bar.shape_kind == "U":
        # The main segment starts at its free end and ends at its bent node.
        if bar.segments[0].start_mm[along] < bar.segments[0].end_mm[along]:
            low += 40*bar.diameter_mm
        else:
            high -= 40*bar.diameter_mm
    else:
        raise ValueError("Unsupported anchorage node: only straight or sourced U")
    if low >= high:
        return ()
    proxy = PhysicalBar(bar.id, bar.direction, bar.steel_class, bar.diameter_mm,
                        q, interval, bar.source_bar_ids)
    offers = []
    for diameter, step, original in service_boxes(proxy, sources):
        bounds = list(original.bounds)
        bounds[along], bounds[along+2] = max(bounds[along], low), min(bounds[along+2], high)
        if bounds[along] < bounds[along+2]:
            offers.append((diameter, step, box(*bounds)))
    return tuple(offers)


def owner_fragments_preserved(bar, frozen, sources):
    """Not merely union coverage: every before-owner's positive part is retained."""
    for owner in bar.source_bar_ids:
        offer = unary_union([p for _, _, p in shaped_service_offers(
            replace(bar, source_bar_ids=(owner,)), sources)])
        if any(fragment.difference(offer).area > 0 for fragment in frozen[
                bar.direction, owner].fragments):
            return False
    return True


def check_shaped_fe_repair(before, after, lanes, problem, host, *,
                            layer_profile=ResearchLayerProfile(), stock_time_limit_s=30):
    """Rebuild canonical forms and independently certify original FE plus stock.

    Any retained old host failures remain failures. A successful geometric U
    substitution does not certify its anchorage capacity or pairwise collisions.
    """
    frozen = freeze_owner_fe_service(before, lanes, problem)
    sources = lane_map(lanes)
    if (not isinstance(after, tuple) or len(after) != len(before)
            or any(not isinstance(bar, ShapedPhysicalBar) for bar in after)):
        raise ValueError("Same complete immutable physical inventory required")
    old = {(b.direction, b.id): b for b in before}
    if len({(b.direction, b.id) for b in after}) != len(after) or set(old) != {
            (b.direction, b.id) for b in after}:
        raise ValueError("Unique unchanged physical identities required")
    if (isinstance(stock_time_limit_s, bool) or not isinstance(stock_time_limit_s, (int, float))
            or not math.isfinite(stock_time_limit_s) or not .001 <= stock_time_limit_s <= 60):
        raise ValueError("Bounded positive stock verification budget required")
    offers, failures, baseline_failures, substitutions = {}, [], [], []
    for bar in after:
        previous = old[bar.direction, bar.id]
        zmain, zreturn = layer_elevations(host, bar.direction, bar.diameter_mm, layer_profile)
        unchanged = straight_bar_from_physical(previous, axis_z_mm=zmain,
                                               placement_profile_id=layer_profile.id)
        if not check_shaped_host(unchanged, host)["whole_body_with_cover_contained"]:
            baseline_failures.append((str(bar.direction), bar.id))
        if (bar.diameter_mm != previous.diameter_mm or bar.steel_class != previous.steel_class
                or bar.source_bar_ids != previous.source_bar_ids
                or abs(bar.selected_cut_length_mm-previous.installed_length_mm) > 1e-7):
            raise ValueError("Source ownership/material/diameter/true cut length changed")
        if bar.shape_kind == "straight":
            if bar != unchanged:
                raise ValueError("Unchanged straight bars must keep all original geometry")
        elif bar.shape_kind == "U":
            axis = 0 if bar.direction.axis is Axis.X else 1
            arc_tip = curve_point(bar.segments[1], 1)
            inward = 1 if bar.segments[0].start_mm[axis] > bar.segments[0].end_mm[axis] else -1
            edge = arc_tip[axis]-inward*(host.side_cover_mm+bar.diameter_mm/2)
            if (edge, inward) not in exterior_edge_choices(host, bar.direction,
                    previous.transverse_axis_mm, bar.diameter_mm):
                raise ValueError("U anchor is not on an actual supported exterior edge")
            rebuilt = build_u_edge_bar(bar_id=bar.id, direction=bar.direction,
                steel_class=previous.steel_class, diameter_mm=previous.diameter_mm,
                transverse_axis_mm=previous.transverse_axis_mm, edge_coordinate_mm=edge,
                inward_sign=inward, main_axis_z_mm=zmain, return_axis_z_mm=zreturn,
                slab_thickness_mm=host.sections[-1].top_z_mm-host.sections[0].bottom_z_mm,
                side_cover_mm=host.side_cover_mm, cut_length_mm=previous.installed_length_mm,
                source_bar_ids=previous.source_bar_ids, placement_profile_id=layer_profile.id)
            if rebuilt.status != "geometry_conditions_met" or rebuilt.bar != bar:
                raise ValueError("U differs from the canonical sourced geometry/layer profile")
            substitutions.append((str(bar.direction), bar.id))
        else:
            raise ValueError("No sourced anchorage assumption for this shape")
        if not owner_fragments_preserved(bar, frozen, sources):
            raise ValueError("A positive frozen original owner FE fragment was lost")
        check = check_shaped_host(bar, host)
        if not check["whole_body_with_cover_contained"]:
            failures.append((str(bar.direction), bar.id))
            if bar.shape_kind != "straight":
                raise ValueError("A new U must pass whole-body plus cover host proof")
        offers.setdefault(bar.direction, []).extend(shaped_service_offers(bar, sources))
    coverage = coverage_from_offers(problem, offers, policy=POLICY)
    if coverage["status"] != "pass":
        raise ValueError("The complete original source FE demand is not covered")
    stock = check_stock_cutting(shaped_cutting_schedule(after), time_limit_s=stock_time_limit_s)
    return {"schema_version": "shaped-frozen-owner-FE-repair-check/v1", "policy": POLICY,
        "placement_eligible": False, "engineering_approval": False, "production_ready": False,
        "source_demand_removed": False, "original_source_values_changed": False,
        "legacy_source_certificate_reused": False, "all_original_owner_FE_fragments_preserved": True,
        "original_FE_positive_fragment_count": sum(len(o.fragments) for o in frozen.values()),
        "physical_metrics": shaped_batch_metrics(after), "stock_cutting": stock,
        "physical_stock_inventory_preserved": True, "source_coverage": coverage,
        "shaped_host_not_proven_before": len(baseline_failures),
        "shaped_host_not_proven_after": len(failures), "host_failure_ids": failures,
        "host_failure_counts": dict(Counter(d for d, _ in failures)),
        "U_substitution_count": len(substitutions), "U_substitution_ids": substitutions,
        "all_U_geometries_rebuilt_independently": True,
        "arc_bridge_and_return_demand_credit": False,
        "free_straight_end_control_40d": "pass", "bent_end_anchorage_capacity": "not_checked",
        "bent_anchorage_profile": K09_U_RETURN_50D_PROFILE.id,
        "status": "blocked_host" if failures else "blocked_stock" if stock["status"] != "pass"
            else "geometry_and_source_pass_not_engineering_approved",
        "not_checked": ["bent_anchorage_resistance_and_compression_zone",
            "whole_party_3D_collisions", "existing_background_and_model_reinforcement",
            "rectangular_regrouping", "approved_XY_order", "Revit_readback"]}
