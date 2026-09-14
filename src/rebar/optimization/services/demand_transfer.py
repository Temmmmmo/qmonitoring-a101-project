"""Independent transverse-only source budget certificate, never a placement gate.

Affine images preserve the longitudinal station s exactly. For every station,
q'=kq+b and a'=a/k prove integral(a' dq')=integral(a dq), including oblique FE
edges. Untransferred original fragments stay in the target, including unresolved
ones over voids. This research analogy is NOT a general permission in STO 2.15.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
import hashlib
import json
import math

from shapely.affinity import affine_transform
from shapely.geometry import MultiPolygon, Polygon, box
from shapely.ops import polygonize, unary_union
from shapely.strtree import STRtree

from rebar.models import Axis, Direction, Layer

from ..contracts.demand_transfer import (
    CheckedDemandTransfer, DemandTransferPolygon, DemandTransferProfile,
    DemandTransferSupplyOffer, SourceDemandTransfer, TransferredDemandPatch,
    TransverseDemandTransferPiece,
)
from ..contracts.problem import LayoutProblem
from .physical_host_fit import _validate_host

PROFILE_ID = "local-transverse-additions-only-prescribed-background/research-v1"


def _number(value, low, high, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise ValueError(f"Finite {name} in {low}..{high} required")


def _sha(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Explicit lowercase SHA256 binding required")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def polygon_record(shape):
    """Keep holes; never replace a source/target polygon by its envelope."""
    if not isinstance(shape, Polygon) or not shape.is_valid or shape.area <= 0:
        raise ValueError("Positive simple polygon required")
    return DemandTransferPolygon(tuple(shape.exterior.coords)[:-1],
        tuple(tuple(ring.coords)[:-1] for ring in shape.interiors))


def _shape(value, *, maximum_vertices=100000):
    if not isinstance(value, DemandTransferPolygon) or not isinstance(value.holes_mm, tuple):
        raise ValueError("Typed immutable polygon record required")
    rings = (value.exterior_mm, *value.holes_mm)
    if sum(len(ring) for ring in rings) > maximum_vertices:
        raise ValueError("Polygon vertex budget exceeded")
    for ring in rings:
        if not isinstance(ring, tuple) or not 3 <= len(ring) <= 4096:
            raise ValueError("Bounded immutable polygon ring required")
        for point in ring:
            if not isinstance(point, tuple) or len(point) != 2:
                raise ValueError("Immutable XY points required")
            for v in point:
                _number(v, -1e9, 1e9, "polygon coordinate")
    result = Polygon(value.exterior_mm, value.holes_mm)
    if not result.is_valid or result.is_empty or result.area <= 0:
        raise ValueError("Positive simple polygon required")
    return result


def _parts(shape):
    if shape.is_empty:
        return ()
    if isinstance(shape, Polygon):
        return (shape,) if shape.area > 0 else ()
    return tuple(p for child in getattr(shape, "geoms", ()) for p in _parts(child))


def _outside_parts(shape, region):
    """Subtract per Polygon: GEOS MultiPolygon/sliver overlay can invent holes.

    Components are not dropped or snapped. A difference outside its operand's
    bounding envelope is an invalid set-operation result and fails closed.
    """
    result = []
    for part in _parts(shape):
        difference = part.difference(region)
        for fragment in _parts(difference):
            if not part.envelope.covers(fragment):
                raise ValueError(f"Numerically inconsistent polygon difference; no invented demand geometry: "
                    f"source_area={part.area!r}, outside_area={fragment.area!r}, "
                    f"source_bounds={part.bounds!r}, outside_bounds={fragment.bounds!r}")
            result.append(fragment)
    return tuple(result)


def _profile(profile):
    if not isinstance(profile, DemandTransferProfile) or profile.id != PROFILE_ID:
        raise ValueError("Explicit additions-only prescribed-background research profile required")
    _number(profile.maximum_transverse_span_mm, 0.001, 600, "local transverse span")
    _number(profile.recipient_clearance_mm, 0, 1000, "recipient clearance")
    for name, ceiling in (("maximum_source_cells", 10000), ("maximum_vertices", 100000),
            ("maximum_transfer_pieces", 20000), ("maximum_target_patches", 50000),
            ("maximum_overlay_pairs", 100000)):
        value = getattr(profile, name)
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError(f"Bounded integer {name} required")


def _inputs(problem, host, profile):
    _profile(profile)
    if not isinstance(problem, LayoutProblem) or not 1 <= len(problem.demand.cells) <= profile.maximum_source_cells:
        raise ValueError("Bounded original one-direction LayoutProblem required")
    demand = problem.demand
    if (not isinstance(demand.direction, Direction) or not isinstance(demand.direction.axis, Axis)
            or not isinstance(demand.direction.layer, Layer)):
        raise ValueError("Typed original direction/layer required")
    if not isinstance(demand.cells, tuple) or not isinstance(demand.levels, tuple):
        raise ValueError("Immutable original demand required")
    if len({level.recipe.background for level in demand.levels if level.recipe is not None}) != 1:
        raise ValueError("One explicit prescribed background per original direction required")
    for i, level in enumerate(demand.levels):
        if (type(level.index) is not int or level.index != i or level.recipe is None
                or len(level.recipe.additions) > 1):
            raise ValueError("Explicit contiguous single-addition source recipes required")
        for spec in (level.recipe.background, *level.recipe.additions):
            if type(spec.diameter) is not int or not 6 <= spec.diameter <= 40 or spec.step not in (100, 150, 300):
                raise ValueError("Bounded known source diameter and nominal step required")
    source, vertices = {}, 0
    for cell in demand.cells:
        if type(cell.id) is not int or cell.id in source or type(cell.level_index) is not int or not 0 <= cell.level_index < len(demand.levels):
            raise ValueError("Unique integer original FE IDs and known integer levels required")
        record = DemandTransferPolygon(cell.poly)
        shape = _shape(record)
        vertices += len(cell.poly)
        if vertices > profile.maximum_vertices:
            raise ValueError("Complete original FE vertex budget exceeded")
        recipe = demand.level(cell.level_index).recipe
        spec = recipe.additions[0] if recipe.additions else None
        intensity = math.pi*spec.diameter**2/(4*spec.step) if spec else 0.0
        source[cell.id] = shape, intensity, spec
    _validate_host(host, 20000)
    material = host.sections[0].footprint
    for section in host.sections[1:]:
        material = material.intersection(section.footprint)
    if material.is_empty or material.area <= 0:
        raise ValueError("Positive common material of every host height section required")
    maximum_diameter = max((spec.diameter for _, _, spec in source.values() if spec), default=0)
    if profile.recipient_clearance_mm < host.side_cover_mm+maximum_diameter/2:
        raise ValueError("Recipient clearance must retain host cover and maximum source bar radius")
    safe = material.buffer(-profile.recipient_clearance_mm, join_style=2)
    source_sha = _digest({"case_id": problem.case_id, "direction": str(demand.direction),
        "source_path": demand.source_path, "levels": [asdict(x) for x in demand.levels],
        "cells": [asdict(x) for x in demand.cells]})
    host_sha = _digest({"side_cover_mm": host.side_cover_mm,
        "sections": [(s.bottom_z_mm, s.top_z_mm, s.footprint.wkb_hex) for s in host.sections]})
    return source, material, safe, source_sha, host_sha


def conserved_strip_intensity(intensities_mm2_per_mm, widths_mm, recipient_width_mm):
    """STO132 arithmetic kernel; full/additional basis must be chosen by caller.

    This formula alone is not a transfer permission or evidence that the widths
    correspond to an admissible support/gradient condition.
    """
    if (not isinstance(intensities_mm2_per_mm, tuple) or not isinstance(widths_mm, tuple)
            or not 1 <= len(widths_mm) <= 10000 or len(widths_mm) != len(intensities_mm2_per_mm)):
        raise ValueError("Equal bounded immutable intensity and width tuples required")
    _number(recipient_width_mm, 0.001, 1e9, "positive recipient width")
    for value in intensities_mm2_per_mm:
        _number(value, 0, 1e6, "source intensity")
    for value in widths_mm:
        _number(value, 0.001, 1e9, "source width")
    return math.fsum(a*w for a, w in zip(intensities_mm2_per_mm, widths_mm, strict=True))/recipient_width_mm


def make_source_demand_transfer(problem, host, pieces, *, source_snapshot_sha256,
                                host_report_sha256, profile=DemandTransferProfile()):
    """Create a bound record; acceptance always requires the independent checker."""
    _, _, _, source_sha, host_sha = _inputs(problem, host, profile)
    _sha(source_snapshot_sha256)
    _sha(host_report_sha256)
    return SourceDemandTransfer(problem.case_id, problem.demand.direction, source_snapshot_sha256,
        source_sha, host_report_sha256, host_sha, profile, pieces)


def check_source_demand_transfer(problem, host, certificate, *, expected_source_snapshot_sha256,
                                 expected_host_report_sha256):
    """Rebuild the whole effective target from original FE, never trust totals.

    Only moved recipients must be inside material. Every unresolved original
    remainder is retained explicitly and must still be satisfied by later supply.
    """
    if not isinstance(certificate, SourceDemandTransfer) or certificate.schema_version != "source-demand-transfer/v1":
        raise ValueError("Typed source demand transfer certificate required")
    _sha(expected_source_snapshot_sha256)
    _sha(expected_host_report_sha256)
    source, material, safe, source_sha, host_sha = _inputs(problem, host, certificate.profile)
    if (certificate.case_id != problem.case_id or certificate.direction != problem.demand.direction
            or certificate.source_demand_sha256 != source_sha or certificate.host_geometry_sha256 != host_sha
            or certificate.source_snapshot_sha256 != expected_source_snapshot_sha256
            or certificate.host_report_sha256 != expected_host_report_sha256):
        raise ValueError("Exact original demand, case, direction and host SHA binding differs")
    if not isinstance(certificate.pieces, tuple) or len(certificate.pieces) > certificate.profile.maximum_transfer_pieces:
        raise ValueError("Bounded immutable transfer piece tuple required")
    by_cell, seen, patches, moves = defaultdict(list), set(), [], []
    source_partition_checks = 0
    vertices = sum(len(cell.poly) for cell in problem.demand.cells)
    across = 1 if certificate.direction.axis is Axis.X else 0
    for piece in certificate.pieces:
        if (not isinstance(piece, TransverseDemandTransferPiece) or not isinstance(piece.id, str)
                or not piece.id or piece.id in seen or type(piece.source_cell_id) is not int
                or piece.source_cell_id not in source):
            raise ValueError("Unique transfer IDs and existing original FE required")
        seen.add(piece.id)
        original, intensity, _ = source[piece.source_cell_id]
        if intensity <= 0:
            raise ValueError("Prescribed background is not transferable additional demand")
        _number(piece.transverse_scale, 0.001, 1000, "positive transverse scale")
        _number(piece.transverse_offset_mm, -1e9, 1e9, "transverse affine offset")
        before = _shape(piece.source_polygon)
        vertices += len(piece.source_polygon.exterior_mm)+sum(len(r) for r in piece.source_polygon.holes_mm)
        if vertices > certificate.profile.maximum_vertices:
            raise ValueError("Complete source/transfer vertex budget exceeded")
        if before.difference(original).area > 0:
            raise ValueError("Transferred fragment leaves its original FE")
        source_partition_checks += len(by_cell[piece.source_cell_id])
        if source_partition_checks > certificate.profile.maximum_overlay_pairs:
            raise ValueError("Complete source partition interaction budget exceeded")
        if any(before.intersection(other).area > 0 for other in by_cell[piece.source_cell_id]):
            raise ValueError("Original fragment counted twice by overlapping transfers")
        by_cell[piece.source_cell_id].append(before)
        k, offset = piece.transverse_scale, piece.transverse_offset_mm
        matrix = (1, 0, 0, k, 0, offset) if across == 1 else (k, 0, 0, 1, offset, 0)
        after = affine_transform(before, matrix)
        span = max(before.bounds[across+2], after.bounds[across+2])-min(before.bounds[across], after.bounds[across])
        if span > certificate.profile.maximum_transverse_span_mm:
            raise ValueError("Source and recipient exceed their finite local transverse strip")
        if not safe.covers(after) or after.difference(safe).area > 0:
            raise ValueError("Transferred recipient leaves actual net material plus cover/radius clearance")
        transferred_intensity = intensity/k
        patches.append(TransferredDemandPatch("transfer/"+piece.id, piece.source_cell_id,
            polygon_record(after), transferred_intensity, "transferred_source"))
        moves.append({"id": piece.id, "source_cell_id": piece.source_cell_id,
            "source_additional_as_mm2_per_mm": intensity,
            "recipient_additional_as_mm2_per_mm": transferred_intensity,
            "transverse_scale": k, "transverse_offset_mm": offset, "local_span_mm": span,
            "source_integral_mm3": intensity*before.area,
            "recipient_integral_mm3": transferred_intensity*after.area})
    retained_outside, retained_outside_safe, retained_measurements, predicate_disagreements = [], [], [], []
    for cell_id, (original, intensity, _) in source.items():
        if intensity <= 0:
            continue
        remainder = original.difference(unary_union(by_cell[cell_id])) if by_cell[cell_id] else original
        for index, shape in enumerate(_parts(remainder)):
            patches.append(TransferredDemandPatch(f"retained/{cell_id}/{index}", cell_id,
                polygon_record(shape), intensity, "retained_source"))
        outside_area = math.fsum(p.area for p in _outside_parts(remainder, material))
        outside_safe_area = math.fsum(p.area for p in _outside_parts(remainder, safe))
        for part in _parts(remainder):
            for label, region in (("material", material), ("recipient_clearance", safe)):
                if not region.covers(part) and part.difference(region).area == 0:
                    predicate_disagreements.append({"source_cell_id": cell_id, "region": label,
                        "retained_component_area_mm2": part.area, "source_fragment_retained": True})
        if outside_area > 0:
            retained_outside.append(cell_id)
        if outside_safe_area > 0:
            retained_outside_safe.append(cell_id)
        if outside_area > 0 or outside_safe_area > 0:
            retained_measurements.append({"source_cell_id": cell_id,
                "outside_material_area_mm2": outside_area, "outside_recipient_clearance_area_mm2": outside_safe_area,
                "additional_as_mm2_per_mm": intensity,
                "outside_material_demand_integral_mm3": outside_area*intensity,
                "outside_clearance_demand_integral_mm3": outside_safe_area*intensity})
    if len(patches) > certificate.profile.maximum_target_patches:
        raise ValueError("Complete effective target patch budget exceeded")
    if sum(len(p.polygon.exterior_mm)+sum(len(r) for r in p.polygon.holes_mm) for p in patches) > certificate.profile.maximum_vertices:
        raise ValueError("Complete effective target vertex budget exceeded")
    original_total = math.fsum(shape.area*intensity for shape, intensity, _ in source.values())
    target_total = math.fsum(_shape(p.polygon).area*p.required_additional_as_mm2_per_mm for p in patches)
    if abs(original_total-target_total) > max(1e-6, original_total*1e-12):
        raise ValueError("Numerical complete additional demand integral changed")
    report = {"schema_version": "source-demand-transfer-check/v1", "profile": PROFILE_ID,
        "placement_eligible": False, "engineering_approval": False, "structural_placement_supported": False,
        "source_demand_removed": False, "original_source_values_changed": False,
        "effective_demand_redistributed": bool(moves), "original_FE_count": len(source),
        "original_FE_ids": sorted(source), "transfer_piece_count": len(moves), "target_patch_count": len(patches),
        "source_partition_interaction_checks": source_partition_checks,
        "original_additional_demand_integral_mm3": original_total,
        "target_additional_demand_integral_mm3": target_total,
        "stationwise_transverse_integral_conservation": "analytical_positive_affine_change_of_variables",
        "longitudinal_coordinate_changed": False, "direction_or_layer_changed": False,
        "prescribed_background_transferred_or_credited": False, "source_band_values_reinterpreted": False,
        "retained_original_FE_outside_material": retained_outside,
        "retained_original_FE_outside_recipient_clearance": retained_outside_safe,
        "retained_original_measurements": retained_measurements,
        "retained_geometry_predicate_disagreements": predicate_disagreements,
        "outside_area_method": "sum_polygon_component_differences_no_snapping_or_area_cutoff",
        "retained_FE_outside_material_above_0_001mm2": [x["source_cell_id"] for x in retained_measurements
            if x["outside_material_area_mm2"] > .001],
        "retained_FE_outside_clearance_above_0_001mm2": [x["source_cell_id"] for x in retained_measurements
            if x["outside_recipient_clearance_area_mm2"] > .001],
        "tiny_remainders_removed_or_snapped": False,
        "area_integral_numerical_check_tolerance_mm3": max(1e-6, original_total*1e-12),
        "moves": moves, "status": "conservation_pass_not_supply_or_placement",
        "units": {"intensity": "mm2/mm", "station_integral": "mm2", "area_integral": "mm3_NOT_installed_mass"},
        "assumptions": ["additional recipe intensity pi*d^2/(4*step), not unknown exact FE As",
            "background remains a separate prescribed requirement; no background surplus credit",
            "local transverse span<=600mm is explicit research analogy, not universal STO permission"],
        "not_checked": ["structural_redistribution_acceptance", "gradient_and_support_conditions",
            "actual_supplied_reinforcement", "anchorage", "stock", "collisions", "actual_Z", "Revit_readback"]}
    return CheckedDemandTransfer(certificate, tuple(patches), report)


def check_transferred_target_supply(problem, host, certificate, offers, *,
                                    expected_source_snapshot_sha256, expected_host_report_sha256):
    """Exact polygon overlay: sum SOURCE obligations <= one sufficient offer.

    Requirements from several original FE are additive at a recipient. Supplied
    weak zones are NOT additive: the maximum single offer must meet that total.
    Offer intensities and polygons still need a physical derivation downstream.
    """
    checked = check_source_demand_transfer(problem, host, certificate,
        expected_source_snapshot_sha256=expected_source_snapshot_sha256,
        expected_host_report_sha256=expected_host_report_sha256)
    if not isinstance(offers, tuple):
        raise ValueError("Immutable explicit supply offers required")
    profile = checked.certificate.profile
    _profile(profile)
    if len(offers) > 5000:
        raise ValueError("Supply offer budget exceeded")
    demand_shapes = [_shape(p.polygon) for p in checked.target_patches]
    supply_shapes, ids = [], set()
    for offer in offers:
        if (not isinstance(offer, DemandTransferSupplyOffer) or not isinstance(offer.id, str)
                or not offer.id or offer.id in ids):
            raise ValueError("Unique typed supply offers required")
        ids.add(offer.id)
        _number(offer.supplied_additional_as_mm2_per_mm, 0, 1e6, "additional supply intensity")
        supply_shapes.append(_shape(offer.polygon))
    shapes = demand_shapes+supply_shapes
    if sum(len(p.exterior.coords)+sum(len(r.coords) for r in p.interiors) for p in shapes) > profile.maximum_vertices:
        raise ValueError("Complete supply-overlay vertex budget exceeded")
    tree = STRtree(shapes)
    if sum(len(tree.query(shape)) for shape in shapes) > profile.maximum_overlay_pairs:
        raise ValueError("Complete overlay interaction budget exceeded; no truncation")
    boundaries = unary_union([p.boundary for p in shapes])
    rows, missing_integral, positive_faces = [], 0.0, 0
    for face in polygonize(boundaries):
        positive_faces += 1
        if positive_faces > profile.maximum_target_patches:
            raise ValueError("Complete overlay face budget exceeded")
        point = face.representative_point()
        demand, supply = 0.0, 0.0
        for i in tree.query(point):
            if not shapes[i].covers(point):
                continue
            if i < len(demand_shapes):
                demand += checked.target_patches[i].required_additional_as_mm2_per_mm
            else:
                supply = max(supply, offers[i-len(demand_shapes)].supplied_additional_as_mm2_per_mm)
        if demand > supply+1e-12:
            missing = (demand-supply)*face.area
            missing_integral += missing
            rows.append({"polygon": asdict(polygon_record(face)), "required_additional_as_mm2_per_mm": demand,
                "single_offer_additional_as_mm2_per_mm": supply, "missing_integral_mm3": missing})
    return {"status": "pass" if not rows else "under_supplied", "placement_eligible": False,
        "source_certificate_independently_revalidated": True,
        "weak_supply_As_summed": False, "positive_overlay_faces": positive_faces,
        "under_supplied_faces": rows, "missing_additional_demand_integral_mm3": missing_integral,
        "physical_offer_derivation_checked": False}


def _recipient_pressure(shape, intensity, static, dynamic, statistics, maximum_interactions):
    """Exact local SOURCE density maximum; not a physical supply calculation."""
    entries = []
    for tree, shapes, densities in (static, dynamic):
        for i in tree.query(shape):
            statistics["interaction_checks"] += 1
            if statistics["interaction_checks"] > maximum_interactions:
                raise ValueError("Complete recipient-pressure interaction budget exceeded")
            hit = shape.intersection(shapes[i])
            if hit.area > 0:
                entries.append((hit, densities[i]))
    statistics["evaluations"] += 1
    if not entries:
        return intensity, 0.0
    mean = math.fsum(geometry.area*density for geometry, density in entries)/shape.area
    local_shapes = [geometry for geometry, _ in entries]
    local_tree = STRtree(local_shapes)
    peak = 0.0
    for face in polygonize(unary_union([shape.boundary, *(p.boundary for p in local_shapes)])):
        statistics["overlay_faces"] += 1
        if statistics["overlay_faces"] > maximum_interactions:
            raise ValueError("Complete recipient-pressure face budget exceeded")
        point = face.representative_point()
        if shape.covers(point):
            peak = max(peak, math.fsum(entries[i][1] for i in local_tree.query(point) if local_shapes[i].covers(point)))
    return intensity+peak, mean


def _recipient_search_region(safe, region, profile):
    """Untrusted proposal mask can only shrink the independently checked safe host."""
    if region is None:
        return safe, {"provided": False, "independent_recipient_clearance_relaxed": False}
    if not isinstance(region, (Polygon, MultiPolygon)) or not region.is_valid or region.is_empty:
        raise ValueError("Positive finite Polygon/MultiPolygon recipient search mask required")
    vertices = 0
    for part in _parts(region):
        record = polygon_record(part)
        _shape(record, maximum_vertices=profile.maximum_vertices)
        vertices += len(record.exterior_mm)+sum(len(ring) for ring in record.holes_mm)
        if vertices > profile.maximum_vertices:
            raise ValueError("Complete recipient search mask vertex budget exceeded")
    chosen = safe.intersection(region)
    if sum(len(p.exterior.coords)+sum(len(r.coords) for r in p.interiors)
            for p in _parts(chosen)) > profile.maximum_vertices:
        raise ValueError("Intersected recipient search mask vertex budget exceeded")
    return chosen, {"provided": True, "requested_wkb_sha256": hashlib.sha256(region.wkb).hexdigest(),
        "intersected_safe_wkb_sha256": hashlib.sha256(chosen.wkb).hexdigest(),
        "requested_area_mm2": region.area, "intersected_safe_area_mm2": chosen.area,
        "independent_recipient_clearance_relaxed": False, "physical_attainability_of_mask_checked": False}


def construct_local_transverse_transfer(problem, host, *, source_snapshot_sha256,
        host_report_sha256, profile=DemandTransferProfile(), compression_scales=(1.0, .75, .5, .25),
        maximum_candidate_checks=100000, maximum_longitudinal_slices=10000,
        maximum_generated_candidates=1000000, recipient_selection="nearest",
        maximum_pressure_interactions=1000000, recipient_region=None):
    """Bounded geometry proposals; unresolved source remains in the full target.

    Split only at actual polygon vertex stations. Never aggregate along the bar,
    extrapolate demand, erase a void, or silently expand the transfer strip.
    """
    source, _, actual_safe, _, _ = _inputs(problem, host, profile)
    safe, mask_report = _recipient_search_region(actual_safe, recipient_region, profile)
    if recipient_selection not in ("nearest", "minimum-peak"):
        raise ValueError("Explicit supported recipient selection mode required")
    if (not isinstance(compression_scales, tuple) or not compression_scales
            or len(compression_scales) > 16 or len(set(compression_scales)) != len(compression_scales)):
        raise ValueError("Bounded unique immutable compression scale choices required")
    for value in compression_scales:
        _number(value, .001, 1, "compression scale")
    for value, ceiling in ((maximum_candidate_checks, 1000000), (maximum_longitudinal_slices, 100000),
            (maximum_generated_candidates, 1000000), (maximum_pressure_interactions, 1000000)):
        if type(value) is not int or not 1 <= value <= ceiling:
            raise ValueError("Bounded integer proposal resource limits required")
    along = 0 if problem.demand.direction.axis is Axis.X else 1
    across = 1-along
    edges = sorted({point[across] for p in _parts(safe) for ring in (p.exterior, *p.interiors)
        for point in ring.coords})
    pieces, missing_candidates, checks, slices, generated = [], [], 0, 0, 0
    original_shapes = [shape for shape, intensity, _ in source.values() if intensity > 0]
    original_intensities = [intensity for _, intensity, _ in source.values() if intensity > 0]
    static = STRtree(original_shapes), original_shapes, original_intensities
    accepted_shapes, accepted_intensities = [], []
    pressure = {"evaluations": 0, "overlay_faces": 0, "interaction_checks": 0}
    exhausted = False
    for cell_id, (original, intensity, _) in source.items():
        if intensity <= 0:
            continue
        outside = original.difference(safe)
        for part in _parts(outside):
            stations = sorted({point[along] for ring in (part.exterior, *part.interiors) for point in ring.coords})
            for lo, hi in zip(stations, stations[1:]):
                slices += 1
                if slices > maximum_longitudinal_slices:
                    raise ValueError("Longitudinal critical-station slice budget exceeded; no truncation")
                clip = box(lo, -1e9, hi, 1e9) if along == 0 else box(-1e9, lo, 1e9, hi)
                for fragment in _parts(part.intersection(clip)):
                    q0, q1 = fragment.bounds[across], fragment.bounds[across+2]
                    candidates = []
                    for k in compression_scales:
                        offsets = set()
                        for edge in edges:
                            if edge < q0-profile.maximum_transverse_span_mm or edge > q1+profile.maximum_transverse_span_mm:
                                continue
                            for offset in (edge-k*q0, edge-k*q1):
                                offsets.update((offset, offset-1e-6, offset+1e-6))
                        offsets.add((1-k)*(q0+q1)/2)
                        for offset in offsets:
                            left, right = k*q0+offset, k*q1+offset
                            span = max(q1, right)-min(q0, left)
                            if span <= profile.maximum_transverse_span_mm:
                                generated += 1
                                if generated > maximum_generated_candidates:
                                    raise ValueError("Complete candidate generation budget exceeded; no truncation")
                                candidates.append((1/k, max(abs(left-q0), abs(right-q1)), k, offset))
                    found, best_score, found_shape = None, None, None
                    dynamic = STRtree(accepted_shapes), accepted_shapes, accepted_intensities
                    for compression, displacement, k, offset in sorted(candidates):
                        if checks == maximum_candidate_checks:
                            exhausted = True
                            break
                        checks += 1
                        matrix = (1, 0, 0, k, 0, offset) if across == 1 else (k, 0, 0, 1, offset, 0)
                        after = affine_transform(fragment, matrix)
                        if safe.covers(after) and after.difference(safe).area == 0 and fragment.difference(original).area == 0:
                            if recipient_selection == "minimum-peak":
                                peak, mean = _recipient_pressure(after, intensity/k, static, dynamic,
                                    pressure, maximum_pressure_interactions)
                                score = peak, mean, compression, displacement, offset
                            else:
                                score = compression, displacement, offset
                            if best_score is None or score < best_score:
                                best_score, found_shape = score, after
                                found = TransverseDemandTransferPiece(f"local/{cell_id}/{len(pieces)}", cell_id,
                                    polygon_record(fragment), k, offset)
                            if recipient_selection == "nearest":
                                break
                    if found is None:
                        missing_candidates.append({"source_cell_id": cell_id,
                            "source_fragment_bounds_mm": list(fragment.bounds), "source_fragment_area_mm2": fragment.area})
                    else:
                        pieces.append(found)
                        accepted_shapes.append(found_shape)
                        accepted_intensities.append(intensity/found.transverse_scale)
                        if len(pieces) > profile.maximum_transfer_pieces:
                            raise ValueError("Complete accepted transfer piece budget exceeded")
    certificate = make_source_demand_transfer(problem, host, tuple(pieces), source_snapshot_sha256=source_snapshot_sha256,
        host_report_sha256=host_report_sha256, profile=profile)
    checked = check_source_demand_transfer(problem, host, certificate,
        expected_source_snapshot_sha256=source_snapshot_sha256, expected_host_report_sha256=host_report_sha256)
    return checked, {"method": "finite_transverse_affine_images_of_actual_outside_fragments",
        "critical_longitudinal_slices": slices, "candidate_geometry_checks": checks,
        "generated_candidates": generated,
        "recipient_search_mask": mask_report,
        "recipient_selection": recipient_selection, "recipient_pressure": pressure,
        "candidate_budget_exhausted": exhausted, "unmoved_fragments": missing_candidates,
        "compression_scales": compression_scales, "global_optimality_proven": False,
        "unmoved_source_removed": False, "placement_eligible": False}


def construct_refined_transverse_transfer(problem, host, *, source_snapshot_sha256,
        host_report_sha256, profile=DemandTransferProfile(), compression_scales=(1.0, .75, .5, .25),
        maximum_candidate_checks=200000, maximum_longitudinal_slices=10000,
        maximum_generated_candidates=1000000, recipient_selection="nearest",
        maximum_pressure_interactions=1000000, refinement_passes=2,
        maximum_transverse_fragment_width_mm=300.0, recipient_region=None):
    """Opt-in exact residual refinement, retaining every original FE obligation.

    Initial proposals are unchanged. Later proposals split only the original
    remainder outside the chosen recipient region, using material/safe critical
    stations and bounded transverse strips. This is search refinement, not demand
    clipping: the independent checker rebuilds all retained pieces from original
    FE after every pass. Search mask feasibility itself is not certified here.
    """
    if type(refinement_passes) is not int or not 1 <= refinement_passes <= 8:
        raise ValueError("Bounded positive refinement pass count required")
    _number(maximum_transverse_fragment_width_mm, 1, 300, "transverse refinement width")
    checked, search = construct_local_transverse_transfer(problem, host,
        source_snapshot_sha256=source_snapshot_sha256, host_report_sha256=host_report_sha256,
        profile=profile, compression_scales=compression_scales,
        maximum_candidate_checks=maximum_candidate_checks, maximum_longitudinal_slices=maximum_longitudinal_slices,
        maximum_generated_candidates=maximum_generated_candidates, recipient_selection=recipient_selection,
        maximum_pressure_interactions=maximum_pressure_interactions, recipient_region=recipient_region)
    if search["candidate_budget_exhausted"]:
        raise ValueError("Initial search exhausted refinement's complete candidate budget")
    source, material, safe, _, _ = _inputs(problem, host, profile)
    chosen, _ = _recipient_search_region(safe, recipient_region, profile)
    along = 0 if problem.demand.direction.axis is Axis.X else 1
    across = 1-along
    geometry_points = [point for geometry in (material, safe, chosen) for p in _parts(geometry)
        for ring in (p.exterior, *p.interiors) for point in ring.coords]
    s_events = sorted({p[along] for p in geometry_points})
    q_events = sorted({p[across] for p in geometry_points})
    recipient_edges = sorted({point[across] for p in _parts(chosen)
        for ring in (p.exterior, *p.interiors) for point in ring.coords})
    pieces = list(checked.certificate.pieces)
    donors = defaultdict(list)
    for piece in pieces:
        donors[piece.source_cell_id].append(_shape(piece.source_polygon))
    accepted = [p for p in checked.target_patches if p.origin == "transferred_source"]
    accepted_shapes = [_shape(p.polygon) for p in accepted]
    accepted_intensities = [p.required_additional_as_mm2_per_mm for p in accepted]
    original_shapes = [shape for shape, intensity, _ in source.values() if intensity > 0]
    original_intensities = [intensity for _, intensity, _ in source.values() if intensity > 0]
    static = STRtree(original_shapes), original_shapes, original_intensities
    pressure = dict(search["recipient_pressure"])
    checks, generated = search["candidate_geometry_checks"], search["generated_candidates"]
    slices = search["critical_longitudinal_slices"]
    refinement_rows, fragment_count, strict_partition_rejections, partition_checks = [], 0, 0, 0
    for pass_number in range(1, refinement_passes+1):
        initial_count = len(pieces)
        for cell_id, (original, intensity, _) in source.items():
            if intensity <= 0:
                continue
            remaining = original.difference(unary_union(donors[cell_id])) if donors[cell_id] else original
            for part in _parts(remaining.difference(chosen)):
                s0, s1 = part.bounds[along], part.bounds[along+2]
                q0, q1 = part.bounds[across], part.bounds[across+2]
                stations = sorted({s0, s1, *(s for s in s_events if s0 < s < s1),
                    *(p[along] for ring in (part.exterior, *part.interiors) for p in ring.coords)})
                segments = math.ceil((q1-q0)/maximum_transverse_fragment_width_mm)
                if segments > maximum_longitudinal_slices:
                    raise ValueError("Complete transverse refinement partition budget exceeded")
                transverse = sorted({q0, q1, *(q for q in q_events if q0 < q < q1),
                    *(q0+(q1-q0)*i/segments for i in range(1, segments))})
                if (len(stations)-1)*(len(transverse)-1) > maximum_longitudinal_slices:
                    raise ValueError("Complete refinement partition budget exceeded")
                for lo, hi in zip(stations, stations[1:]):
                    for qlo, qhi in zip(transverse, transverse[1:]):
                        slices += 1
                        if slices > maximum_longitudinal_slices:
                            raise ValueError("Complete refinement partition budget exceeded")
                        clip = box(lo, qlo, hi, qhi) if along == 0 else box(qlo, lo, qhi, hi)
                        for fragment in _parts(part.intersection(clip)):
                            fragment_count += 1
                            if fragment_count > profile.maximum_target_patches:
                                raise ValueError("Complete refinement fragment budget exceeded")
                            partition_checks += len(donors[cell_id])
                            if partition_checks > profile.maximum_overlay_pairs:
                                raise ValueError("Complete refinement partition interaction budget exceeded")
                            if (fragment.difference(original).area > 0
                                    or any(fragment.intersection(p).area > 0 for p in donors[cell_id])):
                                strict_partition_rejections += 1
                                continue  # Retained by original-minus-accepted reconstruction, never deleted.
                            left, right = fragment.bounds[across], fragment.bounds[across+2]
                            candidates = []
                            for k in compression_scales:
                                offsets = {(1-k)*(left+right)/2}
                                for edge in recipient_edges:
                                    if left-profile.maximum_transverse_span_mm <= edge <= right+profile.maximum_transverse_span_mm:
                                        for offset in (edge-k*left, edge-k*right):
                                            offsets.update((offset, offset-1e-6, offset+1e-6))
                                for offset in offsets:
                                    a, b = k*left+offset, k*right+offset
                                    if max(right, b)-min(left, a) <= profile.maximum_transverse_span_mm:
                                        generated += 1
                                        if generated > maximum_generated_candidates:
                                            raise ValueError("Complete refinement candidate generation budget exceeded")
                                        candidates.append((1/k, max(abs(a-left), abs(b-right)), k, offset))
                            found, best_score, found_shape = None, None, None
                            dynamic = STRtree(accepted_shapes), accepted_shapes, accepted_intensities
                            for compression, displacement, k, offset in sorted(candidates):
                                checks += 1
                                if checks > maximum_candidate_checks:
                                    raise ValueError("Complete refinement candidate check budget exceeded")
                                matrix = (1, 0, 0, k, 0, offset) if across == 1 else (k, 0, 0, 1, offset, 0)
                                after = affine_transform(fragment, matrix)
                                if (not after.is_valid or after.area <= 0 or not chosen.covers(after)
                                        or after.difference(chosen).area > 0):
                                    continue
                                if recipient_selection == "minimum-peak":
                                    peak, mean = _recipient_pressure(after, intensity/k, static, dynamic,
                                        pressure, maximum_pressure_interactions)
                                    score = peak, mean, compression, displacement, offset
                                else:
                                    score = compression, displacement, offset
                                if best_score is None or score < best_score:
                                    best_score, found_shape = score, after
                                    found = TransverseDemandTransferPiece(f"refine/{pass_number}/{cell_id}/{len(pieces)}",
                                        cell_id, polygon_record(fragment), k, offset)
                                if recipient_selection == "nearest":
                                    break
                            if found is not None:
                                pieces.append(found)
                                donors[cell_id].append(fragment)
                                accepted_shapes.append(found_shape)
                                accepted_intensities.append(intensity/found.transverse_scale)
                                if len(pieces) > profile.maximum_transfer_pieces:
                                    raise ValueError("Complete accepted refinement transfer budget exceeded")
        certificate = make_source_demand_transfer(problem, host, tuple(pieces),
            source_snapshot_sha256=source_snapshot_sha256, host_report_sha256=host_report_sha256, profile=profile)
        checked = check_source_demand_transfer(problem, host, certificate,
            expected_source_snapshot_sha256=source_snapshot_sha256, expected_host_report_sha256=host_report_sha256)
        refinement_rows.append({"pass": pass_number, "added_transfer_pieces": len(pieces)-initial_count,
            "remaining_material_FE_above_0_001mm2": checked.report["retained_FE_outside_material_above_0_001mm2"],
            "remaining_clearance_FE_above_0_001mm2": checked.report["retained_FE_outside_clearance_above_0_001mm2"]})
        if len(pieces) == initial_count:
            break
    # One final, bounded translation-only pass over whole ORIGINAL void pieces.
    # Do not re-slice a valid sloping fragment merely because the larger
    # outside-clearance component was partitioned at other host stations.
    whole_void_checks, whole_void_accepted = 0, []
    for cell_id, (original, intensity, _) in source.items():
        if intensity <= 0:
            continue
        remaining = original.difference(unary_union(donors[cell_id])) if donors[cell_id] else original
        for fragment in _outside_parts(remaining, material):
            partition_checks += len(donors[cell_id])
            if partition_checks > profile.maximum_overlay_pairs:
                raise ValueError("Complete whole-void partition interaction budget exceeded")
            if (fragment.difference(original).area > 0
                    or any(fragment.intersection(p).area > 0 for p in donors[cell_id])):
                strict_partition_rejections += 1
                continue
            left, right = fragment.bounds[across], fragment.bounds[across+2]
            offsets = set()
            for edge in recipient_edges:
                if left-profile.maximum_transverse_span_mm <= edge <= right+profile.maximum_transverse_span_mm:
                    for offset in (edge-left, edge-right):
                        for candidate in (offset, offset-1e-6, offset+1e-6):
                            if max(right, right+candidate)-min(left, left+candidate) <= profile.maximum_transverse_span_mm:
                                offsets.add(candidate)
            generated += len(offsets)
            if generated > maximum_generated_candidates:
                raise ValueError("Complete whole-void candidate generation budget exceeded")
            dynamic = STRtree(accepted_shapes), accepted_shapes, accepted_intensities
            best = None
            for offset in sorted(offsets, key=lambda v: (abs(v), v)):
                checks += 1
                whole_void_checks += 1
                if checks > maximum_candidate_checks:
                    raise ValueError("Complete whole-void candidate check budget exceeded")
                matrix = (1, 0, 0, 1, 0, offset) if across == 1 else (1, 0, 0, 1, offset, 0)
                after = affine_transform(fragment, matrix)
                if not chosen.covers(after) or after.difference(chosen).area > 0:
                    continue
                if recipient_selection == "minimum-peak":
                    peak, mean = _recipient_pressure(after, intensity, static, dynamic,
                        pressure, maximum_pressure_interactions)
                    score = peak, mean, abs(offset), offset
                else:
                    score = abs(offset), offset
                if best is None or score < best[0]:
                    best = score, offset, after
                if recipient_selection == "nearest":
                    break
            if best is not None:
                piece = TransverseDemandTransferPiece(f"whole-void/{cell_id}/{len(pieces)}", cell_id,
                    polygon_record(fragment), 1.0, best[1])
                pieces.append(piece)
                donors[cell_id].append(fragment)
                accepted_shapes.append(best[2])
                accepted_intensities.append(intensity)
                whole_void_accepted.append(piece.id)
                if len(pieces) > profile.maximum_transfer_pieces:
                    raise ValueError("Complete whole-void transfer piece budget exceeded")
    certificate = make_source_demand_transfer(problem, host, tuple(pieces),
        source_snapshot_sha256=source_snapshot_sha256, host_report_sha256=host_report_sha256, profile=profile)
    checked = check_source_demand_transfer(problem, host, certificate,
        expected_source_snapshot_sha256=source_snapshot_sha256, expected_host_report_sha256=host_report_sha256)
    return checked, {**search, "method": "iterative_refinement_of_exact_original_residual",
        "initial_search": search, "refinement_passes": refinement_rows,
        "maximum_transverse_fragment_width_mm": maximum_transverse_fragment_width_mm,
        "refinement_fragment_count": fragment_count, "strict_partition_rejections_retained": strict_partition_rejections,
        "refinement_partition_interaction_checks": partition_checks,
        "whole_void_translation_candidate_checks": whole_void_checks,
        "accepted_whole_void_translations": whole_void_accepted,
        "critical_longitudinal_slices": slices, "candidate_geometry_checks": checks,
        "generated_candidates": generated, "recipient_pressure": pressure,
        "unmoved_fragments": None, "unmoved_geometry_source": "independently_rebuilt_retained_original_measurements",
        "candidate_budget_exhausted": False, "every_pass_source_certificate_rechecked": True}
