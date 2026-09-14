# -*- coding: utf-8 -*-
"""Bounded DEMO packet, physical plans and readback. Python 2.7, no Autodesk imports."""
from __future__ import division

import hashlib
import json
import math
import os

from qm_trial_input import _reject_constant, _unique_object, exact_keys, number
from qm_core_trial import same, label
from qm_probe_geometry import distance

MVP_VERSION = "0.6.0"
SCHEMA = "qmonitoring-demo-layout/v1"
WARNING = "DEMO ONLY - NOT FOR CONSTRUCTION"
MAX_BYTES = 512 * 1024


def mesh_signature(geometry):
    """Snapshot identity only: retain duplicates, reject partial/error inventories.

    Empty Solid and unknown-layer diagnostics of the existing probe are explicitly
    retained. This fingerprint does NOT certify layer names or cached CAD colours.
    """
    if (geometry["units"] != "mm" or geometry["limit_exceeded"] is not None
            or geometry["coordinate_system"] != "revit-internal-origin-and-axes"):
        raise ValueError("Unsupported live CAD snapshot")
    records, meshes = geometry["object_records"], geometry["meshes"]
    if not 1 <= len(records) <= 5000 or geometry["visited_object_count"] != len(records):
        raise ValueError("Incomplete CAD object inventory")
    paths = [r["path"] for r in records]
    if len(set(paths)) != len(paths) or any(r["kind"] not in ("Mesh", "Solid", "PolyLine", "GeometryInstance")
                                         or "transform_metadata_error" in r for r in records):
        raise ValueError("Unsupported CAD objects")
    mesh_paths = [m["path"] for m in meshes]
    if len(set(mesh_paths)) != len(mesh_paths) or set(mesh_paths) != set(r["path"] for r in records if r["kind"] == "Mesh"):
        raise ValueError("Missing or duplicated CAD mesh")
    solids = set(r["path"] for r in records if r["kind"] == "Solid")
    issues = geometry["issues"]
    if geometry["issue_count"] != len(issues) or len(issues) > 100:
        raise ValueError("Truncated CAD errors")
    empty = []
    for issue in issues:
        if issue["stage"] != "cad_geometry":
            raise ValueError("Unexpected CAD diagnostic")
        if issue["message"] == "Empty CAD solid" and issue["path"] in solids:
            empty.append(issue["path"])
        elif issue["path"] == "/" and issue["message"] in (
                "Unresolved mesh layers retained as diagnostics; never relabelled KLEENKA",
                "No confirmed KLEENKA triangles; see diagnostic meshes and raw style IDs"):
            pass
        else:
            raise ValueError("Unexplained CAD reading error")
    if len(empty) != len(solids) or set(empty) != solids:
        raise ValueError("Unsupported nonempty CAD solid")
    trusted = geometry["triangles_mm"]
    if len(trusted) > 20000 or geometry["triangle_count"] != len(trusted):
        raise ValueError("Invalid trusted CAD payload")
    claimed, triangles = set(), []
    for mesh in meshes:
        span = mesh["trusted_triangle_range"]
        payload = mesh["triangles_mm"]
        if span is not None:
            if not isinstance(span, list) or len(span) != 2 or payload:
                raise ValueError("Invalid mesh payload reference")
            a, b = span
            number(a, 0, len(trusted), integer=True)
            number(b, a + 1, len(trusted), integer=True)
            if claimed.intersection(range(a, b)):
                raise ValueError("Duplicated trusted mesh reference")
            claimed.update(range(a, b))
            payload = trusted[a:b]
        if (mesh["read_status"] != "collected" or mesh["triangle_count"] != len(payload)
                or mesh["declared_triangle_count"] != len(payload) or not payload):
            raise ValueError("Incomplete mesh payload")
        triangles.extend(payload)
        if len(triangles) > 20000:
            raise ValueError("CAD triangle limit")
    if claimed != set(range(len(trusted))) or geometry["all_mesh_triangle_count"] != len(triangles) or not triangles:
        raise ValueError("CAD triangle inventory differs")
    rows = []
    for triangle in triangles:
        if len(triangle) != 3 or any(len(p) != 3 for p in triangle):
            raise ValueError("Invalid CAD triangle")
        for point in triangle:
            for value in point:
                number(value, -1e9, 1e9)
        # Explicit 0.001 mm bins, identical in CPython and IronPython (no round ties).
        rows.append(tuple(sorted(tuple(int(math.floor(v * 1000 + 0.5)) for v in p) for p in triangle)))
    raw = json.dumps(sorted(rows), separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "triangle_count": len(rows),
            "quantization_mm": 0.001, "empty_solid_count": len(solids), "mesh_count": len(meshes)}


def validate_packet(data):
    exact_keys(data, ("schema_version", "mode", "units", "placement_eligible", "engineering_approved", "warning",
                      "source_sha256", "host", "cad", "hypotheses", "scope", "zones", "expected"))
    for k, v in (("schema_version", SCHEMA), ("mode", "demo-copy-only"), ("units", "mm"),
                 ("placement_eligible", False), ("engineering_approved", False), ("warning", WARNING)):
        same(data[k], v)
    for key in ("dxf", "shk", "reference", "cad_report", "depth_report"):
        value = data["source_sha256"][key]
        if not isinstance(value, type(u"")) and not isinstance(value, str):
            raise ValueError("Invalid input fingerprint")
        if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Invalid input fingerprint")
    host = data["host"]
    exact_keys(host, ("element_id", "unique_id", "bbox_mm", "covers_mm", "bar_types"))
    number(host["element_id"], 407801, 407801, integer=True)
    label(host["unique_id"])
    same(host["covers_mm"], {"top": 25, "bottom": 25, "other": 25})
    exact_keys(host["bar_types"], ("18", "25"))
    for diameter, identifier in (("18", 165160), ("25", 165163)):
        row = host["bar_types"][diameter]
        exact_keys(row, ("element_id", "unique_id"))
        number(row["element_id"], identifier, identifier, integer=True)
        label(row["unique_id"])
    exact_keys(host["bbox_mm"], ("min_mm", "max_mm"))
    lo, hi = host["bbox_mm"]["min_mm"], host["bbox_mm"]["max_mm"]
    if len(lo) != 3 or len(hi) != 3:
        raise ValueError("Invalid host box")
    for a, b, extent in zip(lo, hi, (23600, 14000, 300)):
        number(a, -1e6, 1e6)
        number(b, a + 1, 1e6)
        if abs(b - a - extent) > 0.01:
            raise ValueError("Demo targets the confirmed reference prism")
    cad = data["cad"]
    exact_keys(cad, ("element_id", "unique_id", "instance_transform", "mesh_signature"))
    number(cad["element_id"], 407861, 407861, integer=True)
    label(cad["unique_id"])
    same(cad["instance_transform"], {"origin_mm": [0, 0, 0], "basis_x": [1, 0, 0], "basis_y": [0, 1, 0],
        "basis_z": [0, 0, 1], "basis_units": "dimensionless", "determinant": 1, "is_conformal": True})
    signature = cad["mesh_signature"]
    exact_keys(signature, ("sha256", "triangle_count", "quantization_mm", "empty_solid_count", "mesh_count"))
    label(signature["sha256"])
    if len(signature["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in signature["sha256"]):
        raise ValueError("Invalid CAD signature")
    number(signature["triangle_count"], 1, 20000, integer=True)
    number(signature["empty_solid_count"], 0, 100, integer=True)
    number(signature["mesh_count"], 1, 5000, integer=True)
    same(signature["quantization_mm"], 0.001)
    same(data["hypotheses"], {"direction": {"layer": "top", "axis": "X"}, "background_origin_mm": 0,
        "second_origin_mm": 150, "first_depths_mm": [34, 77], "second_depth_mm": 65,
        "minimum_clear_spacing_mm": 25, "anchorage_diameters_each_end": 40,
        "maximum_bar_length_mm": 11700, "background_created": False,
        "effective_depth_and_splice_design": "not_checked", "other_directions": "not_checked"})
    scope = data["scope"]
    exact_keys(scope, ("target_cell_count", "target_uncovered_cell_count", "original_uncovered_cell_count", "full_solution_mass_kg"))
    number(scope["target_cell_count"], 1, 10000, integer=True)
    number(scope["target_uncovered_cell_count"], 0, 0, integer=True)
    number(scope["original_uncovered_cell_count"], 1, 10000, integer=True)
    same(scope["full_solution_mass_kg"], None)
    zones = data["zones"]
    if not isinstance(zones, list) or not 1 <= len(zones) <= 64:
        raise ValueError("Demo supports 1..64 zones")
    ids = set()
    for z in zones:
        exact_keys(z, ("id", "level_index", "demand_bbox_mm", "additions"))
        label(z["id"])
        if z["id"] in ids:
            raise ValueError("Duplicate zone ID")
        ids.add(z["id"])
        number(z["level_index"], 1, 3, integer=True)
        box = z["demand_bbox_mm"]
        if not isinstance(box, list) or len(box) != 4:
            raise ValueError("Invalid demand rectangle")
        for v in box:
            number(v, -1e6, 1e6)
        if not box[0] < box[2] or not box[1] < box[3]:
            raise ValueError("Degenerate rectangle")
        additions = z["additions"]
        if not isinstance(additions, list) or len(additions) != (1 if z["level_index"] == 1 else 2):
            raise ValueError("Do not drop ordered recipe additions")
        for i, c in enumerate(additions):
            exact_keys(c, ("diameter_mm", "nominal_step_mm", "origin_mm", "axis_depth_from_face_mm"))
            same(c["diameter_mm"], 18 if i == 0 else 25)
            same(c["nominal_step_mm"], 300 if i == 1 and z["level_index"] == 2 else 150)
            same(c["origin_mm"], 0 if i == 0 else 150)
            number(c["axis_depth_from_face_mm"], 34, 77)
            if c["axis_depth_from_face_mm"] not in ((34, 77) if i == 0 else (65,)):
                raise ValueError("Unsupported experimental depth")
        if len(additions) == 2 and additions[0]["axis_depth_from_face_mm"] >= 65:
            raise ValueError("First addition must be nearer the face in this demo hypothesis")
    exact_keys(data["expected"], ("zone_count", "physical_bar_count", "additional_mass_kg"))
    number(data["expected"]["zone_count"], len(zones), len(zones), integer=True)
    number(data["expected"]["physical_bar_count"], 1, 1000, integer=True)
    number(data["expected"]["additional_mass_kg"], 0.001, 10329.4035)
    return data


def load_packet(path):
    if os.path.splitext(path)[1].lower() != ".json":
        raise ValueError("Choose demo-layout.json")
    with open(path, "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("Demo packet exceeds 512 KiB")
    return validate_packet(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_reject_constant))


def make_demo_plan(data, floor, bar_types):
    """Reconstruct all axes from rectangles/recipes, recheck body cover and collisions."""
    validate_packet(data)
    expected = data["host"]
    if floor["element_id"] != expected["element_id"] or floor["unique_id"] != expected["unique_id"]:
        raise ValueError("Live host identity differs")
    for side in ("min_mm", "max_mm"):
        if distance(floor["bbox_mm"][side], expected["bbox_mm"][side]) > 0.01:
            raise ValueError("Live host dimensions/position differ")
    for side in ("top", "bottom", "other"):
        if abs(floor["covers"][side]["distance_mm"] - expected["covers_mm"][side]) > 0.01:
            raise ValueError("Live cover differs")
    for diameter in ("18", "25"):
        actual = bar_types[diameter]
        for k, v in expected["bar_types"][diameter].items():
            same(actual[k], v)
        for k in ("nominal_diameter_mm", "model_diameter_mm"):
            if abs(actual[k] - int(diameter)) > 0.001:
                raise ValueError("Actual bar diameter differs")
    lo, hi = floor["bbox_mm"]["min_mm"], floor["bbox_mm"]["max_mm"]
    runs, all_axes, mass = [], [], 0.0
    for index, zone in enumerate(data["zones"]):
        box = zone["demand_bbox_mm"]
        for other in data["zones"][:index]:
            b = other["demand_bbox_mm"]
            if min(box[2], b[2]) - max(box[0], b[0]) > 1e-6 and min(box[3], b[3]) - max(box[1], b[1]) > 1e-6:
                raise ValueError("Overlapping demand rectangles")
        for component, c in enumerate(zone["additions"]):
            diameter, radius = int(c["diameter_mm"]), c["diameter_mm"] / 2.0
            start, end = box[0] - 40 * diameter, box[2] + 40 * diameter
            length = end - start
            if length > 11700 + 1e-6:
                raise ValueError("Length limit; do not clip or silently splice")
            z = hi[2] - c["axis_depth_from_face_mm"]
            offsets = (100, 200) if c["nominal_step_mm"] == 150 else (0,)
            for offset in offsets:
                origin = c["origin_mm"] + offset
                first = int(math.ceil((box[1] - origin - 1e-6) / 300))
                last = int(math.floor((box[3] - origin + 1e-6) / 300))
                count = last - first + 1
                if count <= 0:
                    continue
                number(count, 1, 1000, integer=True)
                axes = []
                for k in range(first, last + 1):
                    y = origin + k * 300
                    if min(start-lo[0], hi[0]-end, y-radius-lo[1], hi[1]-y-radius,
                           z-radius-lo[2], hi[2]-z-radius) < 25 - 0.01:
                        raise ValueError("Physical bar exits host cover; no clipping")
                    axis = {"start_mm": [start, y, z], "end_mm": [end, y, z]}
                    axes.append(axis)
                    all_axes.append((start, end, y, z, radius))
                runs.append({"zone_id": zone["id"], "component_index": component,
                    "host_id": expected["element_id"], "bar_type_id": expected["bar_types"][str(diameter)]["element_id"],
                    "diameter_mm": diameter, "bar_count": count, "length_mm": length, "spacing_mm": 300,
                    "layout_rule": "Single" if count == 1 else "NumberWithSpacing", "normal": [0, 1, 0], "axes": axes})
                mass += 0.006165 * diameter ** 2 * length / 1000 * count
                if len(runs) > 256 or len(all_axes) > 1000:
                    raise ValueError("Demo run/bar limit")
    for i, a in enumerate(all_axes):
        for b in all_axes[:i]:
            gap = max(a[0]-b[1], b[0]-a[1], 0)
            if math.sqrt(gap**2 + (a[2]-b[2])**2 + (a[3]-b[3])**2) < a[4]+b[4]+25-0.01:
                raise ValueError("Proposed additional bars collide under demo clearance")
    same(len(all_axes), data["expected"]["physical_bar_count"])
    if abs(mass - data["expected"]["additional_mass_kg"]) > 0.000001:
        raise ValueError("Independent demo mass differs")
    return {"runs": runs, "physical_bar_count": len(all_axes), "additional_mass_kg": mass,
            "zone_count": len(data["zones"]), "placement_eligible": False}


def compare_demo_readback(plan, readback):
    sets, diagnostics, actual_mass = readback["sets"], [], 0.0
    if len(sets) != len(plan["runs"]) or len(set(s["element_id"] for s in sets)) != len(sets):
        diagnostics.append("Missing/duplicate Rebar sets")
    count = sum(len(s["bars"]) for s in sets)
    maximum_error = 0.0
    for run, actual in zip(plan["runs"], sets):
        if (actual["host_id"] != run["host_id"] or actual["bar_type"]["element_id"] != run["bar_type_id"]
                or actual["layout_rule"] != run["layout_rule"] or actual["quantity"] != run["bar_count"]
                or actual["number_of_bar_positions"] != run["bar_count"] or len(actual["bars"]) != run["bar_count"]
                or actual["hook_type_ids"] != [-1, -1]):
            diagnostics.append("Host/type/count/layout/hooks differ")
        for key in ("model_diameter_mm", "nominal_diameter_mm"):
            number(actual["bar_type"][key], 1, 100)
            if abs(actual["bar_type"][key] - run["diameter_mm"]) > 0.001:
                diagnostics.append("Readback diameter differs")
        if any(len(bar["curves"]) != 1 or bar["curves"][0]["kind"] != "Line" for bar in actual["bars"]):
            diagnostics.append("Expected exactly one straight segment per bar")
            continue
        lines = sorted((bar["curves"][0] for bar in actual["bars"]), key=lambda line: line["start_mm"][1])
        for line, expected in zip(lines, run["axes"]):
            ends = sorted((line["start_mm"], line["end_mm"]))
            for point in ends:
                for value in point:
                    number(value, -1e9, 1e9)
            error = max(distance(ends[0], expected["start_mm"]), distance(ends[1], expected["end_mm"]))
            maximum_error = max(maximum_error, error)
            actual_mass += 0.006165 * actual["bar_type"]["model_diameter_mm"] ** 2 * distance(*ends) / 1000
    if maximum_error > 0.01 or count != plan["physical_bar_count"] or abs(actual_mass-plan["additional_mass_kg"]) > 0.01:
        diagnostics.append("Final axes/count/mass differ")
    return {"status": "differs" if diagnostics else "matches", "issues": diagnostics,
            "physical_bar_count": count, "additional_mass_from_axes_kg": actual_mass,
            "maximum_endpoint_error_mm": maximum_error, "axis_tolerance_mm": 0.01,
            "mass_tolerance_kg": 0.01, "placement_eligible": False}
