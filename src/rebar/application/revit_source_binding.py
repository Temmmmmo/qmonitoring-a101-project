"""Explicit linked-DXF authority; Revit meshes verify a stored coordinate snapshot.

Not a fallback in the strict layer-certified checker. Does not certify cached CAD
colours, full CAD collection, host containment or permission to create reinforcement.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from rebar.models import Mosaic
from rebar.optimization.adapters.mosaic import build_demand_map
from rebar.reporting.serialization import to_jsonable

from .revit_dxf_calibration import TOLERANCE_MM, _compare_triangles, _mapping, _point

SOURCE_BINDING_POLICY = "linked-dxf-authority/mesh-snapshot-v1"
SYMBOL_METHOD = "symbol-geometry-with-instance-transform-chain-once"
INSTANCE_METHOD = "root-instance-geometry-then-nested-symbol-chain"
COORDINATES = "revit-internal-origin-and-axes"
UNKNOWN_LAYERS = "Unresolved mesh layers retained as diagnostics; never relabelled KLEENKA"
NO_TRUSTED_MESH = "No confirmed KLEENKA triangles; see diagnostic meshes and raw style IDs"
COMPARISON_ISSUE = "Two CAD mesh readings differ or could not be compared"
MAX_MESH_TRIANGLES = 20000
MAX_OBJECTS = 5000
REMAINING_CHECKS = [
    "cached-cad-layers-and-colours", "complete-cad-collection", "live-document-and-link-revalidation",
    "project-shk-approval", "background-grid-phase", "xy-layer-order", "host-cover-openings-containment",
    "3d-collisions", "composite-demand-coverage", "a101-positions", "minimum-width-and-zone-gap",
    "anchorage-engineering-acceptance", "placement-and-readback", "revit-annotations",
]


def _integer(value, name, maximum=MAX_OBJECTS):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def _sha256(value):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("A lowercase SHA256 of each original input is required")
    return value


def _issues(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError("Invalid/truncated issue list")
    rows = []
    for item in value:
        if not isinstance(item, dict) or set(item) - {"stage", "path", "message"}:
            raise ValueError("Unsupported issue record")
        if not all(isinstance(item.get(k), str) for k in ("stage", "message")):
            raise ValueError("Invalid issue record")
        if "path" in item and not isinstance(item["path"], str):
            raise ValueError("Invalid issue path")
        rows.append((item["stage"], item.get("path"), item["message"]))
    return Counter(rows)


def _snapshot(geometry: dict, method: str) -> tuple[list, dict]:
    """Validate ALL recorded meshes, not just a matching subset or the largest mesh.

    Recognizes only the 0.5.1 empty-face Solid and unknown-layer diagnostics. They
    remain limitations of full CAD collection, not discarded evidence of success.
    """
    if (geometry["method"] != method or geometry["units"] != "mm"
            or geometry["coordinate_system"] != COORDINATES or geometry["placement_eligible"] is not False
            or geometry["limit_exceeded"] is not None):
        raise ValueError("Unsupported or resource-limited mesh snapshot")
    records, meshes, trusted = geometry["object_records"], geometry["meshes"], geometry["triangles_mm"]
    if (not isinstance(records, list) or not 1 <= len(records) <= MAX_OBJECTS
            or not isinstance(meshes, list) or not 1 <= len(meshes) <= MAX_OBJECTS
            or not isinstance(trusted, list) or len(trusted) > MAX_MESH_TRIANGLES):
        raise ValueError("Invalid mesh/object payload")
    if _integer(geometry["visited_object_count"], "object count") != len(records):
        raise ValueError("Object inventory is incomplete")
    by_path = {}
    for row in records:
        if (not isinstance(row, dict) or not isinstance(row.get("path"), str)
                or not row["path"].startswith("/") or row["path"] in by_path
                or row.get("kind") not in ("Mesh", "PolyLine", "Solid", "GeometryInstance")
                or "transform_metadata_error" in row):
            raise ValueError("Unsupported/duplicate object record")
        by_path[row["path"]] = row
    expected_inventory = Counter((r.get("layer") or "<unresolved>", r["kind"]) for r in records)
    inventory = Counter()
    for item in geometry["objects"]:
        key = (item["layer"], item["kind"])
        if key in inventory:
            raise ValueError("Duplicate object inventory entry")
        inventory[key] = _integer(item["count"], "inventory count")
    if inventory != expected_inventory:
        raise ValueError("Object inventory counts differ")
    if _integer(geometry["triangle_count"], "trusted triangle count", MAX_MESH_TRIANGLES) != len(trusted):
        raise ValueError("Trusted triangle payload count differs")
    triangles, mesh_paths, claimed = [], set(), set()
    for mesh in meshes:
        path = mesh["path"]
        if (path not in by_path or path in mesh_paths or mesh["kind"] != "Mesh"
                or by_path[path]["kind"] != "Mesh" or mesh["layer"] != by_path[path].get("layer")
                or mesh["read_status"] != "collected"):
            raise ValueError("Incomplete/unaccounted mesh record")
        mesh_paths.add(path)
        payload, span = mesh["triangles_mm"], mesh["trusted_triangle_range"]
        if not isinstance(payload, list):
            raise ValueError("Invalid mesh triangle payload")
        if span is not None:
            if (not isinstance(span, list) or len(span) != 2 or payload or mesh["layer"] is None
                    or mesh["layer"].lower() != "kleenka"):
                raise ValueError("Invalid trusted triangle reference")
            a, b = (_integer(v, "trusted range", len(trusted)) for v in span)
            if a >= b or claimed.intersection(range(a, b)):
                raise ValueError("Overlapping/empty trusted triangle references")
            claimed.update(range(a, b))
            payload = trusted[a:b]
        count = _integer(mesh["declared_triangle_count"], "declared triangles", MAX_MESH_TRIANGLES)
        if not count or count != len(payload) or _integer(mesh["triangle_count"], "mesh triangles", MAX_MESH_TRIANGLES) != count:
            raise ValueError("Mesh triangle count mismatch")
        triangles.extend(payload)
        if len(triangles) > MAX_MESH_TRIANGLES:
            raise ValueError("Mesh triangle limit exceeded")
    if mesh_paths != {p for p, r in by_path.items() if r["kind"] == "Mesh"} or claimed != set(range(len(trusted))):
        raise ValueError("Unaccounted or missing mesh payload")
    if _integer(geometry["all_mesh_triangle_count"], "all triangles", MAX_MESH_TRIANGLES) != len(triangles):
        raise ValueError("All-triangle count mismatch")
    unresolved = sum(m["layer"] is None for m in meshes)
    if (_integer(geometry["unresolved_mesh_count"], "unresolved meshes") != unresolved
            or geometry["layer_resolution_complete"] is not (unresolved == 0)):
        raise ValueError("Inconsistent layer metadata")
    issues = _issues(geometry["issues"])
    solids = {p for p, r in by_path.items() if r["kind"] == "Solid"}
    expected_issues = Counter(("cad_geometry", p, "Empty CAD solid") for p in solids)
    if unresolved:
        expected_issues[("cad_geometry", "/", UNKNOWN_LAYERS)] += 1
    if not trusted:
        expected_issues[("cad_geometry", "/", NO_TRUSTED_MESH)] += 1
    if (issues != expected_issues or _integer(geometry["issue_count"], "issue count", 100) != sum(issues.values())
            or geometry["mesh_read_complete"] is not (not solids)
            or geometry["status"] != ("partial" if issues else "collected")):
        raise ValueError("Unexplained incomplete reading or unsupported CAD errors")
    # Validate numbers before hashing/sorting so NaN and booleans cannot masquerade as coordinates.
    for triangle in triangles:
        if not isinstance(triangle, list) or len(triangle) != 3:
            raise ValueError("Invalid mesh triangle")
        for point in triangle:
            _point(point)
    return triangles, {"mesh_count": len(meshes), "triangle_count": len(triangles),
                       "unresolved_mesh_count": unresolved, "empty_solid_paths": sorted(solids),
                       "original_status": geometry["status"],
                       "original_mesh_read_complete": geometry["mesh_read_complete"],
                       "original_issues": to_jsonable(geometry["issues"])}


def _identity(element):
    element_id = _integer(element["element_id"], "element ID", 2**63 - 1)
    uid = element["unique_id"]
    if not element_id or not isinstance(uid, str) or not 1 <= len(uid.strip()) <= 200:
        raise ValueError("Missing host/CAD identity")
    return {"element_id": element_id, "unique_id": uid}


def verify_source_binding(
    mosaic: Mosaic, report: dict, *, policy_id: str, expected_mosaic_copies: int,
    source_sha256: str, shk_sha256: str, report_sha256: str,
) -> dict[str, Any]:
    """Verify a linked-source snapshot under an explicitly selected narrow policy.

    Hashes must be computed from the actual original inputs by the file boundary.
    Success is source_snapshot_matches, NOT complete CAD calibration or a live binding.
    """
    result = {"schema_version": "revit-source-binding/v1", "status": "blocked", "units": "mm",
              "policy_id": policy_id, "placement_eligible": False, "live_binding_verified": False,
              "cad_layer_identity_verified": False, "cached_cad_colours_verified": False,
              "complete_cad_geometry_verified": False, "issues": [], "remaining_check_ids": list(REMAINING_CHECKS),
              "z_policy": "Verify CAD Z only; reinforcement Z must be derived from slab faces and cover"}
    try:
        if policy_id != SOURCE_BINDING_POLICY:
            raise ValueError("Explicit supported linked-DXF authority policy is required")
        if type(expected_mosaic_copies) is not int or expected_mosaic_copies not in (1, 2):
            raise ValueError("Explicit expected_mosaic_copies must be 1 or 2; no inferred deduplication")
        result["source_sha256"] = {"dxf": _sha256(source_sha256), "shk": _sha256(shk_sha256),
                                   "report": _sha256(report_sha256)}
        if (report["schema_version"] != "revit-cad-calibration-probe/v1" or report["probe_version"] != "0.5.1"
                or report["units"] != "mm" or report["coordinate_system"] != COORDINATES
                or report["read_only"] is not True or report["placement_eligible"] is not False):
            raise ValueError("Only an unchanged read-only CAD Probe 0.5.1 snapshot is supported")
        document, cad, disk = report["document"], report["cad"], report["linked_source_file"]
        if (type(document["is_modified_before"]) is not bool
                or document["is_modified_before"] is not document["is_modified_after"]):
            raise ValueError("Unknown or changed document modification state")
        if (cad["is_linked"] is not True or cad["linked_file_status"] != "Loaded"
                or disk["status"] != "read" or _sha256(disk["sha256"]) != source_sha256):
            raise ValueError("Require the loaded linked DXF with exactly matching source SHA256")
        mapping = _mapping(cad["instance_transform"])
        host_id, cad_id = _identity(report["floor"]), _identity(cad)
        if host_id["unique_id"] == cad_id["unique_id"] or host_id["element_id"] == cad_id["element_id"]:
            raise ValueError("Host and CAD identities cannot be the same")
        demand = build_demand_map(mosaic)
        if not mosaic.legend or any(level.recipe is None or level.requires_extra is None for level in demand.levels):
            raise ValueError("Explicit source legend with every reinforcement recipe is required")
        for cell in demand.cells:
            if demand.level(cell.level_index).aci != cell.aci:
                raise ValueError("Source cell colour/legend mapping is inconsistent")
        result["demand_source"] = {
            "authority": "external-dxf-plus-explicit-shk", "geometry_layer": "KLEENKA",
            "preprocessing": "not_applied_original_source_retained", "cell_count": len(demand.cells),
            "direction": to_jsonable(demand.direction), "levels": to_jsonable(demand.levels),
            "cells_by_level": dict(Counter(str(c.level_index) for c in demand.cells)),
            "unit_scale_to_mm": mosaic.meta.get("unit_scale_to_mm"),
            "unit_detection": mosaic.meta.get("unit_detection"),
            "scope": "Demand from selected hashed sources, not from cached Revit mesh colour or material",
        }
        primary, primary_info = _snapshot(report["cad_geometry"], SYMBOL_METHOD)
        alternate, alternate_info = _snapshot(report["cad_geometry_instance"], INSTANCE_METHOD)
        comparison = report["cad_geometry_comparison"]
        if comparison["placement_eligible"] is not False or comparison["layer_identity_verified"] is not False:
            raise ValueError("Unexpected approval flags in original CAD comparison")
        expected_issues = _issues(report["cad_geometry"]["issues"])
        if comparison["status"] == "not_checked":
            if (comparison.get("reason") != "Mesh reading incomplete or coordinate systems differ"
                    or not (primary_info["empty_solid_paths"] or alternate_info["empty_solid_paths"])):
                raise ValueError("Unexplained incomplete API comparison")
            expected_issues[("cad_geometry_comparison", None, COMPARISON_ISSUE)] += 1
        elif comparison["status"] != "matches":
            raise ValueError("Original CAD comparison reports a difference")
        elif primary_info["empty_solid_paths"] or alternate_info["empty_solid_paths"]:
            raise ValueError("Original CAD comparison contradicts incomplete collection")
        if (_issues(report["issues"]) != expected_issues
                or report["status"] != ("partial" if expected_issues else "collected")):
            raise ValueError("Unexplained report-level errors/status")
        result["original_probe"] = {"status": report["status"], "issues": to_jsonable(report["issues"]),
                                     "comparison": to_jsonable(comparison)}
        result["mesh_snapshots"] = {"symbol": primary_info, "instance": alternate_info}
        # Exact here is intentionally stricter than the old 0.01 mm API comparator.
        # Vertex order/winding and mesh grouping may vary; multiplicity cannot vary.
        def canonical(triangles):
            return Counter(tuple(sorted(tuple(p) for p in t)) for t in triangles)
        same = canonical(primary) == canonical(alternate)
        result["api_snapshot_agreement"] = {"status": "matches" if same else "differs",
                                             "method": "exact-coordinate-triangle-multiset"}
        result["geometry"] = _compare_triangles(mosaic, primary, mapping, expected_copies=expected_mosaic_copies)
        result["geometry"]["tolerance_mm"] = TOLERANCE_MM
        result["geometry"]["expected_mosaic_copies"] = expected_mosaic_copies
        result["geometry"]["multiplicity_policy"] = "Every source cell must occur exactly this many times; no deduplication"
        if not same or result["geometry"]["status"] != "matches":
            raise ValueError("Source/mesh geometry, multiplicity or two API snapshots differ; no automatic alignment")
        result["binding_candidate"] = {
            "host": host_id, "cad": cad_id, "recorded_transform": to_jsonable(cad["instance_transform"]),
            "method": "recorded-instance-transform-tested-not-fitted", "extra_translation_mm": [0, 0, 0],
            "extra_scale": 1, "placement_eligible": False,
        }
        result["status"] = "source_snapshot_matches"
    except (KeyError, TypeError, ValueError, OverflowError, AttributeError, IndexError) as exc:
        result["issues"].append(str(exc))
    return result
