"""Prepare a narrowly scoped Revit DEMO packet from independently replayed research.

No engineering gate is promoted. The actual Revit adapter only writes a new demo copy.
"""
from __future__ import annotations

import hashlib
import json
import math

from .composite_host_review import HOST_COORDINATE_POLICY
from .composite_joint_review import research_composite_joint_depths
from .revit_source_binding import SOURCE_BINDING_POLICY, SYMBOL_METHOD, _snapshot, verify_source_binding


def build_revit_mvp_packet(mosaic, depth_report, reference, cad_report, *, source_sha256):
    if (depth_report.get("schema_version") != "composite-joint-depth-research/v1"
            or depth_report.get("placement_eligible") is not False or not depth_report.get("proposed_zones")):
        raise ValueError("Нужен геометрически проверенный research-отчёт глубин")
    if any(depth_report.get("source_sha256", {}).get(k) != source_sha256[k] for k in ("dxf", "shk", "reference")):
        raise ValueError("DXF/SHK/reference изменились после поиска глубин")
    binding = verify_source_binding(mosaic, cad_report, policy_id=SOURCE_BINDING_POLICY, expected_mosaic_copies=2,
        source_sha256=source_sha256["dxf"], shk_sha256=source_sha256["shk"], report_sha256=source_sha256["cad_report"])
    if binding["status"] != "source_snapshot_matches":
        raise ValueError("Привязка DXF к CAD-снимку не подтверждена: " + str(binding["issues"]))
    for key in ("element_id", "unique_id", "bbox_mm"):
        if reference["floor"][key] != cad_report["floor"][key]:
            raise ValueError("Reference и CAD относятся к разной плите/геометрии")
    replay = research_composite_joint_depths(mosaic, depth_report["input"], reference,
        coordinate_policy=HOST_COORDINATE_POLICY, candidate_depths_by_component=((34, 77), (65,)),
        minimum_clear_spacing_mm=25, hypothesis_source="Replayed MVP-only hypothesis; not engineering approval",
        preserve_component_order=True)
    if (not replay["proposed_zones"] or replay["proposed_zones"] != depth_report["proposed_zones"]
            or replay["maximum_installed_bar_length_mm"] > 11700 + 1e-6
            or replay["additional_mass_kg"] > 10329.4035 or replay["physical_bar_count"] > 464):
        raise ValueError("Раскладка не воспроизводится либо превышает контрольные ограничения")
    triangles, info = _snapshot(cad_report["cad_geometry"], SYMBOL_METHOD)
    rows = sorted(tuple(sorted(tuple(int(math.floor(v * 1000 + 0.5)) for v in point) for point in t)) for t in triangles)
    signature = hashlib.sha256(json.dumps(rows, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()
    floor = reference["floor"]
    types = {str(int(t["nominal_diameter_mm"])): {k: t[k] for k in ("element_id", "unique_id")}
             for t in reference["reference_bar_types"] if t["nominal_diameter_mm"] in (18, 25)}
    return {"schema_version": "qmonitoring-demo-layout/v1", "mode": "demo-copy-only", "units": "mm",
        "placement_eligible": False, "engineering_approved": False, "warning": "DEMO ONLY - NOT FOR CONSTRUCTION",
        "source_sha256": dict(source_sha256),
        "host": {"element_id": floor["element_id"], "unique_id": floor["unique_id"], "bbox_mm": floor["bbox_mm"],
                 "covers_mm": {side: floor["covers"][side]["distance_mm"] for side in ("top", "bottom", "other")}, "bar_types": types},
        "cad": {"element_id": cad_report["cad"]["element_id"], "unique_id": cad_report["cad"]["unique_id"],
                "instance_transform": cad_report["cad"]["instance_transform"],
                "mesh_signature": {"sha256": signature, "triangle_count": len(rows), "quantization_mm": 0.001,
                                   "mesh_count": info["mesh_count"], "empty_solid_count": len(info["empty_solid_paths"])}},
        "hypotheses": {"direction": {"layer": "top", "axis": "X"}, "background_origin_mm": 0,
            "second_origin_mm": 150, "first_depths_mm": [34, 77], "second_depth_mm": 65,
            "minimum_clear_spacing_mm": 25, "anchorage_diameters_each_end": 40, "maximum_bar_length_mm": 11700,
            "background_created": False, "effective_depth_and_splice_design": "not_checked", "other_directions": "not_checked"},
        "scope": {"target_cell_count": replay["interior_partition"]["target_cell_count"], "target_uncovered_cell_count": 0,
                  "original_uncovered_cell_count": replay["original_uncovered_cell_count"], "full_solution_mass_kg": None},
        "zones": [{"id": z["id"], "level_index": z["level_index"], "demand_bbox_mm": z["demand_bbox"],
                   "additions": [{"diameter_mm": c["rebar"]["diameter"], "nominal_step_mm": c["rebar"]["step"],
                                  "origin_mm": c["placement"]["origin_mm"],
                                  "axis_depth_from_face_mm": c["placement"]["axis_depth_from_face_mm"]} for c in z["components"]]}
                  for z in replay["proposed_zones"]],
        "expected": {"zone_count": replay["zone_count"], "physical_bar_count": replay["physical_bar_count"],
                     "additional_mass_kg": replay["additional_mass_kg"]}}
