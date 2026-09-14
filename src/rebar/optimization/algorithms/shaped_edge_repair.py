"""Bounded deterministic exterior-U proposal search, independently checked later."""
from __future__ import annotations

from collections import Counter

from ..services.collision_replacement import freeze_owner_fe_service
from ..services.opening_relocation import lane_map
from ..services.shaped_fe_repair import (
    ResearchLayerProfile, exterior_edge_choices, layer_elevations, owner_fragments_preserved,
)
from ..services.shaped_geometry import build_u_edge_bar, check_shaped_host, straight_bar_from_physical


def propose_exterior_u_repair(before, lanes, problem, host, *,
                              layer_profile=ResearchLayerProfile(), maximum_candidate_checks=50000):
    """Keep one bar, q, diameter and true cut length; never delete a failed bar.

    This deliberately does NOT yet choose a jointly collision-free shaped plan.
    A changed curve can intersect another face, so a separate 3D audit is required.
    """
    if type(maximum_candidate_checks) is not int or not 1 <= maximum_candidate_checks <= 100000:
        raise ValueError("Bounded positive candidate budget required")
    frozen = freeze_owner_fe_service(before, lanes, problem)
    sources = lane_map(lanes)
    checks, reasons, after = 0, Counter(), []
    for previous in before:
        zm, zr = layer_elevations(host, previous.direction, previous.diameter_mm, layer_profile)
        straight = straight_bar_from_physical(previous, axis_z_mm=zm,
                                              placement_profile_id=layer_profile.id)
        if check_shaped_host(straight, host)["whole_body_with_cover_contained"]:
            after.append(straight)
            continue
        selected = straight
        for edge, inward in exterior_edge_choices(host, previous.direction,
                previous.transverse_axis_mm, previous.diameter_mm):
            checks += 1
            if checks > maximum_candidate_checks:
                raise ValueError("Complete U candidate budget exceeded; no truncated proof")
            result = build_u_edge_bar(bar_id=previous.id, direction=previous.direction,
                steel_class=previous.steel_class, diameter_mm=previous.diameter_mm,
                transverse_axis_mm=previous.transverse_axis_mm, edge_coordinate_mm=edge,
                inward_sign=inward, main_axis_z_mm=zm, return_axis_z_mm=zr,
                slab_thickness_mm=host.sections[-1].top_z_mm-host.sections[0].bottom_z_mm,
                side_cover_mm=host.side_cover_mm, cut_length_mm=previous.installed_length_mm,
                source_bar_ids=previous.source_bar_ids, placement_profile_id=layer_profile.id)
            if result.status != "geometry_conditions_met":
                reasons[result.report["reason"]] += 1
                continue
            if not owner_fragments_preserved(result.bar, frozen, sources):
                reasons["positive_original_owner_FE_loss"] += 1
                continue
            if not check_shaped_host(result.bar, host)["whole_body_with_cover_contained"]:
                reasons["whole_U_outside_host_with_cover"] += 1
                continue
            selected = result.bar
            break
        after.append(selected)
    return tuple(after), {"candidate_checks": checks, "rejections": dict(reasons),
        "method": "fixed_q_diameter_true_length_exterior_U_first_fit",
        "joint_3D_collision_filter": False, "global_optimality_proven": False,
        "placement_eligible": False}
