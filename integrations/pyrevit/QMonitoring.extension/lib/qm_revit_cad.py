# -*- coding: utf-8 -*-
"""Read CAD meshes for offline calibration. IronPython 2.7; no model mutations."""
from __future__ import division

import hashlib
import math
import os
import platform

from qm_revit_probe import Probe, collect_report, text_type
from qm_cad_diagnostics import (
    INSTANCE_METHOD, SYMBOL_METHOD, compare_geometry_reads, layer_catalog, material_info, style_info,
)

# Each of two passes has an explicit budget; reports now include diagnostic geometry.
MAX_TRIANGLES = 20000
MAX_OBJECTS = 5000
MAX_DEPTH = 12
MAX_FILE_BYTES = 64 * 1024 * 1024


class CadReadLimitError(ValueError):
    pass


def read_cad_geometry(probe, instance, mode="symbol"):
    """Read every mesh, but keep unknown/other layers outside trusted triangles_mm.

    Primary: symbol geometry + full transform chain. Alternative: GetInstanceGeometry
    ONLY on root instances (already in model coordinates), then nested symbol chains.
    Never apply the root transform twice or add GetTotalTransform. A failed object
    does not prevent reading siblings; global resource limits stop the pass explicitly.
    """
    if mode not in ("symbol", "instance"):
        raise ValueError("Unknown CAD reading method")
    DB = probe.DB
    result = {"status": "partial", "layer": "KLEENKA", "triangles_mm": [],
              "coordinate_system": "revit-internal-origin-and-axes", "units": "mm",
              "method": SYMBOL_METHOD if mode == "symbol" else INSTANCE_METHOD,
              "objects": [], "object_records": [], "meshes": [], "issues": [],
              "issue_count": 0, "visited_object_count": 0, "all_mesh_triangle_count": 0,
              "mesh_read_complete": True, "layer_resolution_complete": False,
              "placement_eligible": False, "limit_exceeded": None}
    counts = {}

    def issue(path, message, geometry_error=False):
        result["issue_count"] += 1
        if len(result["issues"]) < 100:
            result["issues"].append({"stage": "cad_geometry", "path": path, "message": text_type(message)[:1024]})
        if geometry_error:
            result["mesh_read_complete"] = False

    def record(obj, path, inherited, kind=None):
        if result["visited_object_count"] >= MAX_OBJECTS:
            raise CadReadLimitError("CAD object limit exceeded")
        result["visited_object_count"] += 1
        row = style_info(probe, obj, inherited)
        row.update({"path": path, "kind": kind or text_type(obj.GetType().Name)})
        result["object_records"].append(row)
        key = (row["layer"] or "<unresolved>", row["kind"])
        counts[key] = counts.get(key, 0) + 1
        return row

    def add_mesh(mesh, transform, owner):
        row = dict(owner)
        row.update({"material": material_info(probe, mesh), "read_status": "partial",
                    "triangles_mm": [], "trusted_triangle_range": None, "triangle_count": 0})
        result["meshes"].append(row)
        try:
            count = int(mesh.NumTriangles)
            row["declared_triangle_count"] = count
            if count <= 0:
                raise ValueError("Empty CAD mesh")
            if result["all_mesh_triangle_count"] + count > MAX_TRIANGLES:
                raise CadReadLimitError("CAD triangle limit exceeded")
            for i in range(count):
                triangle = mesh.get_Triangle(i)
                points = [probe.point(transform.OfPoint(triangle.get_Vertex(j))) for j in range(3)]
                if any(math.isnan(x) or math.isinf(x) for p in points for x in p):
                    raise ValueError("Non-finite CAD geometry")
                row["triangles_mm"].append(points)
                row["triangle_count"] += 1
                result["all_mesh_triangle_count"] += 1
            points = [p for t in row["triangles_mm"] for p in t]
            row["bbox_mm"] = {"min_mm": [min(p[i] for p in points) for i in range(3)],
                             "max_mm": [max(p[i] for p in points) for i in range(3)]}
            row["read_status"] = "collected"
            if row["layer"] is not None and row["layer"].lower() == "kleenka":
                start = len(result["triangles_mm"])
                result["triangles_mm"].extend(row["triangles_mm"])
                row["trusted_triangle_range"] = [start, len(result["triangles_mm"])]
                row["triangles_mm"] = []  # Do not duplicate the trusted payload in JSON.
        except CadReadLimitError:
            raise
        except Exception as exc:
            issue(owner["path"], exc, geometry_error=True)

    def walk(geometry, transform, inherited=None, depth=0, parent=""):
        if depth > MAX_DEPTH:
            raise CadReadLimitError("CAD nested instance limit exceeded")
        if geometry is None:
            raise ValueError("Missing CAD geometry")
        for index, obj in enumerate(geometry):
            path = parent + "/" + str(index)
            try:
                row = record(obj, path, inherited)
                layer = row["layer"]
                if isinstance(obj, DB.GeometryInstance):
                    # Metadata failures must not erase otherwise readable mesh coordinates.
                    try:
                        row["instance_transform"] = probe.transform(obj.Transform)
                    except Exception as exc:
                        row["transform_metadata_error"] = text_type(exc)[:1024]
                    if mode == "instance" and depth == 0:
                        row["child_read_method"] = "GetInstanceGeometry; root transform already applied"
                        walk(obj.GetInstanceGeometry(), DB.Transform.Identity, layer, depth + 1, path)
                    else:
                        row["child_read_method"] = "GetSymbolGeometry; apply instance transform once"
                        walk(obj.GetSymbolGeometry(), transform.Multiply(obj.Transform), layer, depth + 1, path)
                elif isinstance(obj, DB.Mesh):
                    add_mesh(obj, transform, row)
                elif isinstance(obj, DB.Solid):
                    if obj.Faces.Size == 0:
                        raise ValueError("Empty CAD solid")
                    for face_index, face in enumerate(obj.Faces):
                        face_path = path + "/face:" + str(face_index)
                        face_row = record(face, face_path, layer, "Face")
                        face_row["material"] = material_info(probe, face)
                        if not isinstance(face, DB.PlanarFace):
                            issue(face_path, "Curved CAD face; no approximation accepted", geometry_error=True)
                            continue
                        try:
                            add_mesh(face.Triangulate(), transform, face_row)
                        except CadReadLimitError:
                            raise
                        except Exception as exc:
                            issue(face_path, exc, geometry_error=True)
                elif layer is not None and layer.lower() == "kleenka":
                    issue(path, "Unsupported KLEENKA geometry: " + row["kind"])
            except CadReadLimitError:
                raise
            except Exception as exc:
                issue(path, exc, geometry_error=True)

    try:
        if not isinstance(instance, DB.ImportInstance):
            raise ValueError("Select a CAD ImportInstance")
        options = DB.Options()
        options.ComputeReferences = False
        options.IncludeNonVisibleObjects = True
        walk(instance.get_Geometry(options), DB.Transform.Identity)
    except CadReadLimitError as exc:
        result["limit_exceeded"] = text_type(exc)
        issue("/", exc, geometry_error=True)
    except Exception as exc:
        issue("/", exc, geometry_error=True)
    result["unresolved_mesh_count"] = sum(m["layer"] is None for m in result["meshes"])
    result["layer_resolution_complete"] = not result["unresolved_mesh_count"]
    if result["unresolved_mesh_count"]:
        issue("/", "Unresolved mesh layers retained as diagnostics; never relabelled KLEENKA")
    if not result["triangles_mm"]:
        issue("/", "No confirmed KLEENKA triangles; see diagnostic meshes and raw style IDs")
    if not result["issues"]:
        result["status"] = "collected"
    result["objects"] = [{"layer": layer, "kind": kind, "count": counts[(layer, kind)]}
                         for layer, kind in sorted(counts)]
    result["triangle_count"] = len(result["triangles_mm"])
    return result


def file_snapshot(path):
    """Use native .NET file metadata on IronPython (no Mono.Unix dependency)."""
    if platform.python_implementation() == "IronPython":
        from System.IO import FileInfo
        info = FileInfo(path)
        return int(info.Length), int(info.LastWriteTimeUtc.Ticks)
    info = os.stat(path)
    return info.st_size, info.st_mtime


def hash_local_dxf(path):
    """Bounded local read only. Never open UNC/server/cloud paths or other file types."""
    if (not os.path.isabs(path) or path.startswith(("\\\\", "//"))
            or os.path.splitext(path)[1].lower() != ".dxf"):
        raise ValueError("Only an absolute local DXF path can be fingerprinted")
    before = file_snapshot(path)
    if not 0 < before[0] <= MAX_FILE_BYTES:
        raise ValueError("Linked DXF is empty or exceeds 64 MiB")
    digest, size = hashlib.sha256(), 0
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise ValueError("Linked DXF grew beyond the read limit")
            digest.update(chunk)
    after = file_snapshot(path)
    if size != before[0] or before != after:
        raise ValueError("Linked DXF changed during fingerprinting")
    return {"status": "read", "name": os.path.basename(path), "size_bytes": size,
            "sha256": digest.hexdigest(), "scope": "linked disk file, not proof of cached CAD contents"}


def linked_source_file(probe, instance):
    if not instance.IsLinked:
        return {"status": "not_checked", "reason": "Imported CAD has no trusted source-file link"}
    try:
        ref = probe.doc.GetElement(instance.GetTypeId()).GetExternalFileReference()
        if text_type(ref.GetLinkedFileStatus()) != "Loaded":
            raise ValueError("CAD link is not loaded")
        path = probe.DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(ref.GetAbsolutePath())
        return hash_local_dxf(text_type(path))
    except Exception as exc:
        return {"status": "not_checked", "reason": text_type(exc)}


def collect_cad_report(document, DB, instance, runtime=None):
    before = bool(document.IsModified)
    report = collect_report(document, DB, instance, runtime)
    report["schema_version"] = "revit-cad-calibration-probe/v1"
    report["document"]["is_modified_before"] = before
    probe = Probe(document, DB)
    report["cad_geometry"] = read_cad_geometry(probe, instance)
    report["cad_geometry_instance"] = read_cad_geometry(probe, instance, mode="instance")
    report["cad_geometry_comparison"] = compare_geometry_reads(
        report["cad_geometry"], report["cad_geometry_instance"])
    report["cad_layer_catalog"] = layer_catalog(probe, instance)
    report["linked_source_file"] = linked_source_file(probe, instance)
    report["issues"].extend(report["cad_geometry"]["issues"])
    report["document"]["is_modified_after"] = bool(document.IsModified)
    if before != bool(document.IsModified):
        report["issues"].append({"stage": "document", "message": "Document modification flag changed"})
    if report["issues"] or report["cad_geometry"]["status"] != "collected":
        report["status"] = "partial"
    if report["cad_geometry_comparison"]["status"] != "matches":
        report["issues"].append({"stage": "cad_geometry_comparison", "message":
                                "Two CAD mesh readings differ or could not be compared"})
        report["status"] = "partial"
    # Only the offline verifier has the source Mosaic. Collection alone is NOT calibration.
    report["placement_eligible"] = False
    return report
