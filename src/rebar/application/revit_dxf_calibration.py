"""Independent, read-only DXF ↔ Revit CAD check. This is not a placement contract.

The candidate mapping is the recorded GetTransform, with source units normalized by
the existing ingest. It is TESTED, never fitted to the slab or silently rescaled.
Every mesh vertex and every source cell must agree, not merely three bbox corners.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from itertools import combinations, product
from typing import Any

from rebar.models import Mosaic

TOLERANCE_MM = 0.1  # Technical readback tolerance, not an engineering acceptance limit.
MAX_TRIANGLES = 50000


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a finite coordinate, not text/boolean")
    if not math.isfinite(value) or abs(value) > 1e10:
        raise ValueError("Non-finite or out-of-range coordinate")
    return float(value)


def _point(value: Any, dimension: int = 3) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != dimension:
        raise ValueError("Invalid point dimension")
    return tuple(_number(x) for x in value)


def _mapping(value: dict) -> tuple[tuple, tuple, tuple, tuple]:
    origin = _point(value["origin_mm"])
    x, y, z = (_point(value["basis_" + axis]) for axis in "xyz")
    if value["is_conformal"] is not True or value["basis_units"] != "dimensionless":
        raise ValueError("Non-conformal or unknown transform units")
    # A proper horizontal rigid transform only. No inferred scale, reflection, tilt or shear.
    expected_y = (-x[1], x[0], 0)
    if (abs(math.hypot(x[0], x[1]) - 1) > 1e-9 or abs(x[2]) > 1e-9
            or math.dist(y, expected_y) > 1e-9 or math.dist(z, (0, 0, 1)) > 1e-9
            or abs(_number(value["determinant"]) - 1) > 1e-9):
        raise ValueError("Only unit-scale, non-reflected, horizontal CAD transforms are supported")
    return origin, x, y, z


def _transform(p: tuple, mapping: tuple) -> tuple[float, float, float]:
    origin, *basis = mapping
    return tuple(origin[i] + sum(basis[j][i] * p[j] for j in range(3)) for i in range(3))


def _cross(a: tuple, b: tuple, c: tuple) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _source_mesh(mosaic: Mosaic, mapping: tuple) -> tuple[list, list, dict, list]:
    if not 1 <= len(mosaic.cells) <= MAX_TRIANGLES:
        raise ValueError("Empty source or source cell limit exceeded")
    if mosaic.meta.get("units") != "mm" or mosaic.meta.get("source_format") != "dxf":
        raise ValueError("Use the normal DXF ingest with recorded millimetre normalization")
    zlo, zhi = _point(mosaic.meta["z_range_mm"], 2)
    if abs(zhi - zlo) > 1e-6:
        raise ValueError("Only one planar source mosaic is supported")
    raw = []
    for cell in mosaic.cells:
        points = [tuple(round(x, 6) for x in _point(p, 2)) for p in cell.poly]
        if len(points) not in (3, 4) or len(set(points)) != len(points):
            raise ValueError("Only nondegenerate source triangles and quads are supported")
        turns = [_cross(points[i-1], points[i], points[(i+1) % len(points)]) for i in range(len(points))]
        if not (all(t > 1e-6 for t in turns) or all(t < -1e-6 for t in turns)):
            raise ValueError("Source polygon is non-convex or degenerate")
        raw.append(points)
    source = sorted({p for poly in raw for p in poly})
    index = {p: i for i, p in enumerate(source)}
    world = [_transform((*p, zlo), mapping) for p in source]
    allowed = {}
    cells = []
    for cell_index, poly in enumerate(raw):
        ids = [index[p] for p in poly]
        if len(ids) == 3:
            alternatives = [frozenset([tuple(sorted(ids))])]
        else:
            a, b, c, d = ids
            alternatives = [frozenset([tuple(sorted(t)) for t in pair]) for pair in (
                ((a, b, c), (a, c, d)), ((a, b, d), (b, c, d)))]
        cells.append(alternatives)
        for triangle in combinations(ids, 3):
            key = tuple(sorted(triangle))
            if key in allowed:
                raise ValueError("Overlapping/duplicate source cells make triangle ownership ambiguous")
            allowed[key] = cell_index
    return source, world, allowed, cells


def _mesh_check(mosaic: Mosaic, geometry: dict, mapping: tuple) -> dict:
    if (geometry["status"] != "collected" or geometry["issues"]
            or geometry["layer"] != "KLEENKA" or geometry["units"] != "mm"
            or geometry["coordinate_system"] != "revit-internal-origin-and-axes"
            or geometry["method"] != "symbol-geometry-with-instance-transform-chain-once"):
        raise ValueError("CAD geometry collection is incomplete or has unsupported coordinates")
    triangles = geometry["triangles_mm"]
    if (not isinstance(triangles, list) or not 1 <= len(triangles) <= MAX_TRIANGLES
            or type(geometry["triangle_count"]) is not int or geometry["triangle_count"] != len(triangles)):
        raise ValueError("Invalid CAD triangle count")
    return _compare_triangles(mosaic, triangles, mapping)


def _compare_triangles(mosaic: Mosaic, triangles: list, mapping: tuple, *, expected_copies: int = 1) -> dict:
    """Shared numerical check only; callers enforce their OWN source/report policies.

    Counts every occurrence. An explicit second copy may have the other valid quad
    diagonal; removing or deduplicating occurrences is never an accepted repair.
    This helper does not assign CAD layers or change collection/placement flags.
    """
    if type(expected_copies) is not int or expected_copies not in (1, 2):
        raise ValueError("Expected mosaic copies must explicitly be 1 or 2")
    if not isinstance(triangles, list) or not 1 <= len(triangles) <= MAX_TRIANGLES:
        raise ValueError("Invalid CAD triangle count")
    source, expected, allowed, cells = _source_mesh(mosaic, mapping)
    buckets = defaultdict(list)

    def bucket(p):
        return tuple(math.floor(x / TOLERANCE_MM) for x in p)

    for i, p in enumerate(expected):
        buckets[bucket(p)].append(i)
    observed_cells = [Counter() for _ in cells]
    matches, bad_vertices, bad_triangles = {}, 0, 0
    max_error = 0.0
    all_actual = []
    cached_candidates = {}
    candidate_checks = 0
    for raw_triangle in triangles:
        if not isinstance(raw_triangle, list) or len(raw_triangle) != 3:
            raise ValueError("Invalid mesh triangle")
        ids = []
        for raw_point in raw_triangle:
            p = _point(raw_point)
            all_actual.append(p)
            if p not in cached_candidates:
                key, candidates = bucket(p), []
                for delta in product((-1, 0, 1), repeat=3):
                    for i in buckets.get(tuple(a+b for a, b in zip(key, delta)), ()):
                        candidate_checks += 1
                        if candidate_checks > 1000000:
                            raise ValueError("Source vertex comparison work limit exceeded")
                        if math.dist(expected[i], p) <= TOLERANCE_MM:
                            candidates.append(i)
                cached_candidates[p] = candidates
            candidates = cached_candidates[p]
            # No nearest-neighbour tie-breaking that could hide ambiguous source geometry.
            if len(candidates) != 1:
                bad_vertices += 1
                continue
            i = candidates[0]
            error = math.dist(expected[i], p)
            max_error = max(max_error, error)
            if i not in matches or error < matches[i][0]:
                matches[i] = (error, p)
            ids.append(i)
        key = tuple(sorted(ids))
        if len(ids) != 3 or key not in allowed:
            bad_triangles += 1
        else:
            observed_cells[allowed[key]][key] += 1
    bad_cells = [i for i, (observed, alternatives) in enumerate(zip(observed_cells, cells))
                 if not any(observed == sum((Counter(a) for a in copies), Counter())
                            for copies in product(alternatives, repeat=expected_copies))]
    missing = len(expected) - len(matches)
    # Stable, widely separated NON-collinear source vertices; not invented bbox corners.
    a = 0
    b = max(range(len(source)), key=lambda i: math.dist(source[a], source[i]))
    c = max(range(len(source)), key=lambda i: abs(_cross(source[a], source[b], source[i])))
    if abs(_cross(source[a], source[b], source[c])) <= 1e-6:
        raise ValueError("Non-collinear control points are required")
    controls = [{"source_xy_mm": list(source[i]), "expected_cad_xyz_mm": list(expected[i]),
                 "actual_cad_xyz_mm": list(matches[i][1]) if i in matches else None,
                 "error_mm": matches[i][0] if i in matches else None} for i in (a, b, c)]
    dimensions = []
    for i, j in ((a, b), (a, c), (b, c)):
        expected_length = math.dist(expected[i], expected[j])
        actual_length = math.dist(matches[i][1], matches[j][1]) if i in matches and j in matches else None
        dimensions.append({"source_length_mm": expected_length, "actual_length_mm": actual_length,
                           "scale_ratio": actual_length / expected_length if actual_length is not None else None})
    return {"status": "matches" if not (bad_vertices or bad_triangles or bad_cells or missing) else "differs",
            "source_cell_count": len(cells), "source_vertex_count": len(source),
            "actual_triangle_count": len(triangles), "matched_source_vertex_count": len(matches),
            "unmatched_or_ambiguous_vertex_occurrences": bad_vertices, "missing_source_vertices": missing,
            "unexpected_triangles": bad_triangles, "mismatched_cell_count": len(bad_cells),
            "first_mismatched_cell_indices": bad_cells[:20], "max_matched_vertex_error_mm": max_error,
            "control_points": controls, "control_lengths": dimensions,
            "actual_bbox_mm": {"min_mm": [min(p[i] for p in all_actual) for i in range(3)],
                               "max_mm": [max(p[i] for p in all_actual) for i in range(3)]}}


def verify_dxf_calibration(mosaic: Mosaic, report: dict, *, source_sha256: str | None = None) -> dict:
    """Recheck raw readback independently. All production/engineering gates stay closed."""
    result = {"schema_version": "revit-dxf-calibration/v1", "status": "blocked", "units": "mm",
              "placement_eligible": False, "tolerance_mm": TOLERANCE_MM,
              "source": {"name": mosaic.source_path.replace("\\", "/").split("/")[-1],
                         "sha256": source_sha256, "unit_scale_to_mm": mosaic.meta.get("unit_scale_to_mm"),
                         "direction": {"layer": mosaic.direction.layer.value, "axis": mosaic.direction.axis.value},
                         "unit_detection": mosaic.meta.get("unit_detection"),
                         "z_range_mm": mosaic.meta.get("z_range_mm")},
              "issues": [], "not_checked": ["cached-cad-colours-and-demand-identity", "background-grid-phase",
                  "xy-layer-order", "host-cover-openings-containment", "3d-collisions",
                  "composite-demand-coverage", "anchorage-engineering-acceptance", "placement-revalidation"],
              "z_policy": "CAD Z is checked only as source geometry; rebar Z must come from slab faces/cover"}
    try:
        if (report["schema_version"] != "revit-cad-calibration-probe/v1" or report["read_only"] is not True
                or report["placement_eligible"] is not False or report["status"] != "collected" or report["issues"]):
            raise ValueError("Require a complete read-only CAD Probe report, not a reference bbox report")
        document = report["document"]
        if (type(document["is_modified_before"]) is not bool
                or type(document["is_modified_after"]) is not bool
                or document["is_modified_before"] != document["is_modified_after"]):
            raise ValueError("Document modification state changed or is unknown")
        cad = report["cad"]
        mapping = _mapping(cad["instance_transform"])
        result["binding"] = {"cad_unique_id": cad["unique_id"], "cad_element_id": cad["element_id"],
                             "host_unique_id": report["floor"]["unique_id"],
                             "host_element_id": report["floor"]["element_id"],
                             "candidate_transform": cad["instance_transform"],
                             "method": "recorded-instance-transform-tested-not-fitted",
                             "extra_translation_mm": [0, 0, 0], "extra_scale": 1,
                             "placement_eligible": False}
        result["geometry"] = _mesh_check(mosaic, report["cad_geometry"], mapping)
        disk = report.get("linked_source_file", {})
        source_match = "not_checked"
        if source_sha256 is not None and cad["is_linked"] is True and disk.get("status") == "read":
            if len(source_sha256) != 64 or any(c not in "0123456789abcdef" for c in source_sha256):
                raise ValueError("Invalid source SHA256")
            source_match = "matches" if disk.get("sha256") == source_sha256 else "differs"
        result["linked_disk_file"] = {"status": source_match,
            "scope": "disk bytes only; cached model content and demand colours are not certified"}
        actual, host = result["geometry"]["actual_bbox_mm"], report["floor"]["bbox_mm"]
        deltas = {key: [actual[key][i] - _point(host[key])[i] for i in (0, 1)] for key in ("min_mm", "max_mm")}
        result["host_xy_bbox"] = {"status": "matches" if all(abs(x) <= TOLERANCE_MM for v in deltas.values()
                                                            for x in v) else "differs",
                                  "deltas_mm": deltas, "scope": "extents only, NOT host containment"}
        if result["geometry"]["status"] == "matches" and source_match != "differs":
            result["status"] = "geometry_matches"
        else:
            result["issues"].append("Source/CAD mesh or linked disk file differs; no automatic alignment")
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        result["issues"].append(str(exc))
    return result
