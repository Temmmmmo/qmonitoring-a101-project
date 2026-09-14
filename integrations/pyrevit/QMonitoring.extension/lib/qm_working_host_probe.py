# -*- coding: utf-8 -*-
"""Read one explicitly selected working Floor and CAD; never modify the model."""
from __future__ import division

import datetime
import platform

from qm_cad_diagnostics import compare_geometry_reads, layer_catalog
from qm_revit_cad import hash_local_dxf, linked_source_file, read_cad_geometry
from qm_revit_probe import Probe, element_id, text_type

VERSION = "0.1.0"
SCHEMA = "revit-working-host-cad-probe/v1"
MAX_HOST_FACES = 5000
MAX_HOST_EDGES = 50000
MAX_HOST_TESSELLATION_POINTS = 200000


def read_host_solid(probe, floor):
    """Native solid/loops only; bounded evidence, not a flat-prism approximation."""
    DB = probe.DB
    options = DB.Options()
    options.DetailLevel = DB.ViewDetailLevel.Fine
    geometry = floor.get_Geometry(options)
    solids = []
    for item in geometry:
        if isinstance(item, DB.GeometryInstance):
            raise ValueError("Nested host geometry is unsupported; no substitute host is produced")
        if isinstance(item, DB.Solid) and item.Volume > 0:
            solids.append(item)
    if len(solids) != 1:
        raise ValueError("Expected exactly one positive-volume native floor solid")
    faces, edge_count, point_count = [], 0, 0
    for face in solids[0].Faces:
        if len(faces) >= MAX_HOST_FACES:
            raise ValueError("Host face read limit exceeded; no truncated solid is returned")
        plane = ({"origin_mm": probe.point(face.Origin), "normal": probe.vector(face.FaceNormal)}
                 if isinstance(face, DB.PlanarFace) else None)
        loops = []
        for loop in face.EdgeLoops:
            curves = []
            for edge in loop:
                edge_count += 1
                if edge_count > MAX_HOST_EDGES:
                    raise ValueError("Host edge read limit exceeded; no truncated solid is returned")
                curve = probe.curve(edge.AsCurve())
                point_count += len(curve["tessellated_points_mm"])
                if point_count > MAX_HOST_TESSELLATION_POINTS:
                    raise ValueError("Host tessellation read limit exceeded; no truncated solid is returned")
                curves.append(curve)
            loops.append(curves)
        faces.append({"plane": plane, "edge_loops": loops})
    if not faces:
        raise ValueError("Native floor solid has no readable faces")
    return {"faces": faces, "volume_mm3": float(solids[0].Volume) * probe.mm(1.0) ** 3}


def _selected_file(path):
    if not path:
        return {"status": "not_checked", "reason": "No optional local source DXF was selected"}
    try:
        result = hash_local_dxf(path)
        result["scope"] = "explicitly selected local disk file; NOT proof of cached imported/linked CAD identity"
        return result
    except Exception as exc:
        return {"status": "not_checked", "reason": text_type(exc)}


def collect_working_host_report(document, DB, floor, cad_instance, runtime=None, source_dxf_path=None):
    """Explicit native selections only. Never call the reference-model collector."""
    if document is None or document.IsFamilyDocument:
        raise ValueError("Open a Revit project, not a family document")
    if not isinstance(floor, DB.Floor) or floor.Document != document:
        raise ValueError("Choose a native Floor in the active document")
    if not isinstance(cad_instance, DB.ImportInstance) or cad_instance.Document != document:
        raise ValueError("Choose a CAD ImportInstance in the same active document")
    probe = Probe(document, DB)
    report = {"schema_version": SCHEMA, "probe_version": VERSION,
        "created_utc": datetime.datetime.utcnow().isoformat() + "Z", "read_only": True,
        "units": "mm", "coordinate_system": "revit-internal-origin-and-axes",
        "placement_eligible": False, "engineering_approval": False,
        "status": "partial", "host_id": element_id(floor.Id), "cad_id": element_id(cad_instance.Id),
        "runtime": runtime or {"python": platform.python_version(), "implementation": platform.python_implementation()},
        "document": {"title": text_type(document.Title), "is_workshared": bool(document.IsWorkshared)},
        "revit": {}, "read_issues": probe.issues, "issues": [],
        "source_binding": {"status": "not_checked", "reason": "Offline comparison against the exact source DXF is required"},
        "source_dxf_units": {"status": "not_checked", "reason": "Revit coordinates are normalized to mm; original DXF units are not inferred from instance scale"},
        "not_checked": ["raw-dxf-to-host-binding", "source-dxf-units", "xy-depth-profile", "bar-containment",
                        "background-and-additions-3d-contact", "engineering-acceptance"]}
    for key, attribute in (("path", "PathName"), ("is_read_only", "IsReadOnly"),
                           ("is_modifiable", "IsModifiable"), ("is_modified_before", "IsModified")):
        report["document"][key] = probe.attempt("document/" + key,
            lambda a=attribute: text_type(getattr(document, a)) if a == "PathName" else bool(getattr(document, a)))
    for attribute in ("VersionName", "VersionNumber", "VersionBuild", "SubVersionNumber"):
        report["revit"][attribute] = probe.attempt("revit/" + attribute,
            lambda a=attribute: text_type(getattr(document.Application, a)))
    host_issue_start = len(probe.issues)
    report["host"] = probe.attempt("working_host/floor", lambda: probe.floor(floor))
    report["host_solid"] = probe.attempt("working_host/solid", lambda: read_host_solid(probe, floor))
    report["host_read_issues"] = list(probe.issues[host_issue_start:])
    if report["host"] is not None and report["host"].get("element_id") != report["host_id"]:
        mismatch = {"section": "working_host/identity", "error_type": "ValueError",
                    "message": "Native floor snapshot ID differs from the explicit selection"}
        probe.issues.append(mismatch)
        report["host_read_issues"].append(mismatch)
    report["cad"] = probe.attempt("working_cad/metadata", lambda: probe.cad(cad_instance))
    # Capture built-in unit/display parameters as evidence, without inventing an enum-to-unit mapping.
    report["cad_parameters"] = probe.attempt("working_cad/parameters", lambda: probe.parameters(cad_instance))
    report["cad_type_parameters"] = probe.attempt("working_cad/type_parameters",
        lambda: probe.parameters(document.GetElement(cad_instance.GetTypeId())))
    report["cad_geometry"] = read_cad_geometry(probe, cad_instance)
    report["cad_geometry_instance"] = read_cad_geometry(probe, cad_instance, mode="instance")
    report["cad_geometry_comparison"] = compare_geometry_reads(report["cad_geometry"], report["cad_geometry_instance"])
    report["cad_layer_catalog"] = layer_catalog(probe, cad_instance)
    report["linked_source_file"] = linked_source_file(probe, cad_instance)
    report["selected_source_file"] = _selected_file(source_dxf_path)
    report["document"]["is_modified_after"] = probe.attempt("document/is_modified_after", lambda: bool(document.IsModified))
    before, after = report["document"]["is_modified_before"], report["document"]["is_modified_after"]
    report["document"]["modification_flag_unchanged"] = None if before is None or after is None else before == after
    if before is not None and after is not None and before != after:
        report["issues"].append({"stage": "document", "message": "Document modification flag changed during read"})
    for key in ("cad_geometry", "cad_geometry_instance", "cad_layer_catalog"):
        if report[key]["status"] != "collected":
            report["issues"].append({"stage": key, "message": "Partial read; inspect this section's diagnostics"})
    if report["cad_geometry_comparison"]["status"] != "matches":
        report["issues"].append({"stage": "cad_geometry_comparison", "message": "Two CAD geometry reads could not be matched"})
    if not report["read_issues"] and not report["issues"] and report["host"] is not None and report["host_solid"] is not None:
        report["status"] = "collected"
    return report
