"""Read-only target geometry/density diagnostics, not physical bar acceptance."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
import math

from shapely.geometry import LineString, Polygon, box
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from rebar.models import Axis

from ..contracts.demand_transfer import CheckedDemandTransfer
from .demand_transfer import (
    _inputs, _number, _outside_parts, _parts, _shape, check_source_demand_transfer, polygon_record,
)


def target_density_summary(checked):
    """Exact overlay of additive SOURCE budgets; never adds supplied weak zones."""
    if not isinstance(checked, CheckedDemandTransfer):
        raise ValueError("Checked demand target required")
    patches = checked.target_patches
    shapes = [_shape(p.polygon) for p in patches]
    for patch in patches:
        _number(patch.required_additional_as_mm2_per_mm, 0, 1e6, "target intensity")
    tree = STRtree(shapes)
    interactions = sum(len(tree.query(shape)) for shape in shapes)
    profile = checked.certificate.profile
    if interactions > profile.maximum_overlay_pairs:
        raise ValueError("Complete target-density interaction budget exceeded")
    peak, meaningful_peak, above, faces = 0.0, 0.0, 0.0, 0
    limit = math.pi*16**2/(4*100)
    for face in polygonize(unary_union([p.boundary for p in shapes])):
        faces += 1
        if faces > profile.maximum_target_patches:
            raise ValueError("Complete target-density face budget exceeded")
        point = face.representative_point()
        density = math.fsum(patches[i].required_additional_as_mm2_per_mm
            for i in tree.query(point) if shapes[i].covers(point))
        peak = max(peak, density)
        if face.area > .001:
            meaningful_peak = max(meaningful_peak, density)
        if density > limit+1e-12:
            above += face.area
    return {"maximum_additional_as_mm2_per_mm": peak,
        "maximum_additional_as_on_faces_above_0_001mm2": meaningful_peak,
        "area_above_D16_at100_mm2": above, "D16_at100_additional_as_mm2_per_mm": limit,
        "atomic_face_count": faces, "overlay_interactions": interactions,
        "is_installed_mass_or_physical_supply_proof": False, "placement_eligible": False}


def _runs(geometry, along, slo, shi, qlo, qhi):
    """Closed q runs where an entire constant-q along segment is in geometry.

    These are sufficient rectangular-envelope corridors. Their absence is NOT
    a proof that no compressed nonrectangular FE fragment can fit.
    """
    region = box(slo, qlo, shi, qhi) if along == 0 else box(qlo, slo, qhi, shi)
    across = 1-along
    forbidden = sorted((p.bounds[across], p.bounds[across+2])
        for p in _parts(region.difference(geometry)) if p.area > 0)
    merged = []
    for a, b in forbidden:
        if merged and a <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(merged[-1][1], b)
        else:
            merged.append((a, b))
    result, cursor = [], qlo
    for a, b in merged:
        if cursor < a:
            result.append((cursor, a))
        cursor = max(cursor, b)
    if cursor < qhi:
        result.append((cursor, qhi))
    return result


def retained_transfer_geometry_diagnostics(problem, host, checked):
    """Classify retained FE demand, not host-failed reinforcement pieces.

    Only a positive longitudinal interval with zero material width in the whole
    permitted local q band is a necessary obstruction to transverse-only moves.
    """
    if not isinstance(checked, CheckedDemandTransfer):
        raise ValueError("Checked full target required")
    fresh = check_source_demand_transfer(problem, host, checked.certificate,
        expected_source_snapshot_sha256=checked.certificate.source_snapshot_sha256,
        expected_host_report_sha256=checked.certificate.host_report_sha256)
    if fresh.target_patches != checked.target_patches:
        raise ValueError("Diagnostic target differs from independently rebuilt certificate")
    source, material, safe, source_sha, host_sha = _inputs(problem, host, checked.certificate.profile)
    if (source_sha != checked.certificate.source_demand_sha256
            or host_sha != checked.certificate.host_geometry_sha256):
        raise ValueError("Diagnostic original source/host binding differs")
    outer = unary_union([Polygon(p.exterior) for p in _parts(material)])
    holes = outer.difference(material)
    retained = defaultdict(list)
    for patch in checked.target_patches:
        if patch.origin == "retained_source":
            retained[patch.source_cell_id].append(_shape(patch.polygon))
    along = 0 if problem.demand.direction.axis is Axis.X else 1
    across = 1-along
    span = checked.certificate.profile.maximum_transverse_span_mm
    vertices = {p[along] for geometry in (safe, material) for part in _parts(geometry)
        for ring in (part.exterior, *part.interiors) for p in ring.coords}
    rows, categories, proofs = [], Counter(), Counter()
    for cell_id, patches in retained.items():
        missing = tuple(p for patch in patches for p in _outside_parts(patch, material))
        missing_area = math.fsum(p.area for p in missing)
        if missing_area <= .001:
            continue
        exterior_area = math.fsum(p.area for fragment in missing for p in _outside_parts(fragment, outer))
        hole_area = math.fsum(fragment.intersection(holes).area for fragment in missing)
        category = "both" if exterior_area > .001 and hole_area > .001 else "outer_edge" if exterior_area > .001 else "opening"
        categories[category] += 1
        pieces = []
        for fragment in missing:
            if fragment.area <= 0:
                continue
            slo, shi = fragment.bounds[along], fragment.bounds[along+2]
            q0, q1 = fragment.bounds[across], fragment.bounds[across+2]
            qlo, qhi = q1-span, q0+span
            stations = sorted({slo, shi, *(s for s in vertices if slo < s < shi),
                *(p[along] for ring in (fragment.exterior, *fragment.interiors) for p in ring.coords)})
            empty_stations, empty_material_stations = [], []
            for a, b in zip(stations, stations[1:]):
                if a == b:
                    continue
                s = (a+b)/2
                line = LineString(((s, qlo), (s, qhi))) if along == 0 else LineString(((qlo, s), (qhi, s)))
                # Positive source slice is required; triangular tips are not a
                # finite As obstruction merely because an isolated point exists.
                if fragment.intersection(line).length > 0:
                    if safe.intersection(line).length == 0:
                        empty_stations.append((a, b))
                    if material.intersection(line).length == 0:
                        empty_material_stations.append((a, b))
            material_runs = _runs(material, along, slo, shi, qlo, qhi) if qlo < qhi else []
            safe_runs = _runs(safe, along, slo, shi, qlo, qhi) if qlo < qhi else []
            alternatives = []
            for k in (1, .75, .5, .25):
                for a, b in safe_runs:
                    low, high = max(a-k*q0, qlo-k*q0), min(b-k*q1, qhi-k*q1)
                    if low <= high:
                        alternatives.append({"scale": k, "offset_interval_mm": [low, high],
                            "recipient_q_run_mm": [a, b]})
            over_global = slo < material.bounds[along] or shi > material.bounds[along+2]
            proof = ("outside_global_longitudinal_host_bound" if over_global and empty_material_stations else
                "no_material_within_local_transverse_window" if empty_material_stations else
                "no_clearance_material_within_local_transverse_window" if empty_stations else
                "rectangle_corridor_candidates_exist" if alternatives else "finite_affine_search_not_exhaustive")
            proofs[proof] += 1
            point = fragment.representative_point()
            pieces.append({"polygon": asdict(polygon_record(fragment)), "area_mm2": fragment.area,
                "along_interval_mm": [slo, shi], "source_transverse_range_mm": [q0, q1],
                "permitted_recipient_q_envelope_mm": [qlo, qhi],
                "outside_global_along_lower_mm": max(0, material.bounds[along]-slo),
                "outside_global_along_upper_mm": max(0, shi-material.bounds[along+2]),
                "representative_point_distance_to_material_mm": point.distance(material),
                "full_along_material_q_corridors_mm": material_runs,
                "full_along_clearance_q_corridors_mm": safe_runs,
                "empty_positive_source_station_intervals_mm": empty_stations,
                "empty_bare_material_source_station_intervals_mm": empty_material_stations,
                "rectangular_envelope_affine_candidates": alternatives,
                "obstruction_status": proof})
        rows.append({"source_cell_id": cell_id, "direction": str(problem.demand.direction),
            "category": category, "outside_material_area_mm2": missing_area,
            "outside_exterior_area_mm2": exterior_area, "over_opening_area_mm2": hole_area,
            "additional_as_mm2_per_mm": source[cell_id][1], "fragments": pieces})
    return {"schema_version": "retained-demand-geometry-diagnostics/v1", "direction": str(problem.demand.direction),
        "units": "mm", "remaining_direction_FE_count_above_0_001mm2": len(rows),
        "categories": dict(categories), "fragment_obstruction_counts": dict(proofs), "cells": rows,
        "outside_area_method": "sum_polygon_component_differences_no_snapping_or_area_cutoff",
        "retained_geometry_predicate_disagreements": fresh.report["retained_geometry_predicate_disagreements"],
        "placement_eligible": False, "source_demand_removed": False,
        "warning": "These are original FE demand fragments, NOT bar counts or a bent-bar applicability proof."}
