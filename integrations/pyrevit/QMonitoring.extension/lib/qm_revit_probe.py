# -*- coding: utf-8 -*-
"""Read-only Revit adapter. No transactions, setters, network or model saves.

Revit DB is injected to permit tests without Autodesk binaries. IronPython 2.7 syntax.
"""
from __future__ import division

import datetime
import json
import os
import platform

from qm_probe_geometry import bounds, compare_reference, measure_parallel_bars, plane_depths

try:
    text_type = unicode  # noqa: F821 -- IronPython 2.7
except NameError:
    text_type = str

PROBE_VERSION = "0.5.1"
MAX_BAR_POSITIONS = 10000


def serialize_report_utf8(report):
    """Keep .NET text as Unicode; encode UTF-8 only after the JSON is complete.

    IronPython 2.7 maps str/unicode to System.String. The ASCII JSON encoder can
    mistake text containing Latin-1 symbols (e.g. square units) for UTF-8 bytes
    and call decode on it. Do not use that path, default=str or lossy encoding.
    NaN, infinity, unsupported objects and circular references still fail.
    """
    return json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def write_report_json(destination, report):
    """Create only a new .json; serialization errors must not leave an empty file."""
    if os.path.splitext(destination)[1].lower() != ".json":
        raise ValueError("Report destination must be a new .json file")
    content = serialize_report_utf8(report)
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
    with os.fdopen(descriptor, "wb") as report_file:
        report_file.write(content)


def element_id(value):
    # Revit 2024 has 64-bit IDs. IntegerValue is only a compatibility fallback.
    return int(value.Value if hasattr(value, "Value") else value.IntegerValue)


def element_name(element, DB):
    """Read the instance property first; .NET bridges expose different descriptors."""
    if element is None:
        raise ValueError("Cannot read the name of a missing element")
    try:
        return text_type(element.Name)
    except AttributeError:
        # ElementType can hide the inherited getter with a setter-only property.
        # A getset_descriptor has __get__, NOT GetValue; older bridges can expose
        # GetValue instead. Neither route changes a Revit property.
        descriptor = DB.Element.Name
        getter = getattr(descriptor, "__get__", None)
        if callable(getter):
            return text_type(getter(element, DB.Element))
        getter = getattr(descriptor, "GetValue", None)
        if callable(getter):
            return text_type(getter(element))
        raise


def identity(element, DB):
    return {"element_id": element_id(element.Id), "unique_id": text_type(element.UniqueId),
            "name": element_name(element, DB), "class": text_type(element.GetType().FullName)}


class Probe(object):
    def __init__(self, document, DB):
        self.doc = document
        self.DB = DB
        self.issues = []

    def attempt(self, section, callback, default=None):
        try:
            return callback()
        except Exception as exc:
            self.issues.append({"section": section, "error_type": type(exc).__name__,
                                "message": text_type(exc)})
            return default

    def mm(self, value):
        return float(self.DB.UnitUtils.ConvertFromInternalUnits(value, self.DB.UnitTypeId.Millimeters))

    def point(self, point):
        return [self.mm(point.X), self.mm(point.Y), self.mm(point.Z)]

    def vector(self, point):
        return [float(point.X), float(point.Y), float(point.Z)]

    def transform(self, transform):
        return {"origin_mm": self.point(transform.Origin),
                "basis_x": self.vector(transform.BasisX),
                "basis_y": self.vector(transform.BasisY),
                "basis_z": self.vector(transform.BasisZ),
                "basis_units": "dimensionless", "determinant": float(transform.Determinant),
                "is_conformal": bool(transform.IsConformal)}

    def bbox(self, element):
        box = element.get_BoundingBox(None)
        if box is None:
            raise ValueError("Element has no model bounding box")
        points = [self.point(box.Transform.OfPoint(self.DB.XYZ(x, y, z)))
                  for x in (box.Min.X, box.Max.X) for y in (box.Min.Y, box.Max.Y)
                  for z in (box.Min.Z, box.Max.Z)]
        return bounds(points)

    def curve(self, curve):
        return {"kind": text_type(curve.GetType().Name), "length_mm": self.mm(curve.Length),
                "start_mm": self.point(curve.GetEndPoint(0)),
                "end_mm": self.point(curve.GetEndPoint(1)),
                "tessellated_points_mm": [self.point(p) for p in curve.Tessellate()],
                "tessellation": "Revit approximation; endpoints/length from original curve"}

    def parameters(self, element):
        """Built-ins only. Double raw values stay internal; only length specs become mm."""
        result = []
        for parameter in element.Parameters:
            if element_id(parameter.Id) >= 0:
                continue

            def read(p=parameter):
                storage = text_type(p.StorageType)
                row = {"parameter_id": element_id(p.Id), "name": text_type(p.Definition.Name),
                       "storage_type": storage, "display_value": p.AsValueString()}
                if storage == "Double":
                    row["value_internal"] = float(p.AsDouble())
                    spec = p.Definition.GetDataType()
                    row["spec_type_id"] = text_type(spec.TypeId)
                    if spec.Equals(self.DB.SpecTypeId.Length):
                        row["length_mm"] = self.mm(p.AsDouble())
                elif storage == "Integer":
                    row["value"] = int(p.AsInteger())
                elif storage == "ElementId":
                    row["value"] = element_id(p.AsElementId())
                elif storage == "String":
                    row["value"] = p.AsString()
                return row

            row = self.attempt("parameter/{0}/{1}".format(element_id(element.Id),
                                                         element_id(parameter.Id)), read)
            if row is not None:
                result.append(row)
        return sorted(result, key=lambda p: p["parameter_id"])

    def mark(self, element):
        parameter = element.get_Parameter(self.DB.BuiltInParameter.ALL_MODEL_MARK)
        return parameter.AsString() if parameter is not None else None

    def resolve_reference(self, number, expected_class, mark):
        element = self.doc.GetElement(self.DB.ElementId(number))
        if element is None or not isinstance(element, expected_class):
            raise ValueError("Expected {0} at ElementId {1}".format(expected_class.__name__, number))
        if self.mark(element) != mark:
            raise ValueError("ElementId {0} has a different Mark; reference was not selected".format(number))
        return element

    def faces(self, floor, top):
        refs = (self.DB.HostObjectUtils.GetTopFaces(floor) if top
                else self.DB.HostObjectUtils.GetBottomFaces(floor))
        result = []
        for reference in refs:
            face = floor.GetGeometryObjectFromReference(reference)
            plane = None
            if isinstance(face, self.DB.PlanarFace):
                plane = {"origin_mm": self.point(face.Origin), "normal": self.vector(face.FaceNormal)}
            result.append({"plane": plane,
                           "edge_loops": [[self.curve(edge.AsCurve()) for edge in loop]
                                          for loop in face.EdgeLoops],
                           "loop_roles": "unclassified; do not assume the first loop is outer"})
        if not result:
            raise ValueError("No host faces returned")
        return result

    def cover(self, floor, parameter_name):
        parameter = floor.get_Parameter(getattr(self.DB.BuiltInParameter, parameter_name))
        if parameter is None or not parameter.HasValue:
            raise ValueError("Missing cover parameter: " + parameter_name)
        cover_type = self.doc.GetElement(parameter.AsElementId())
        result = identity(cover_type, self.DB)
        result["distance_mm"] = self.mm(cover_type.CoverDistance)
        return result

    def floor(self, floor):
        result = identity(floor, self.DB)
        result["mark"] = self.mark(floor)
        result["type"] = self.attempt("floor/type", lambda: identity(
            self.doc.GetElement(floor.GetTypeId()), self.DB))
        result["level"] = self.attempt("floor/level", lambda: identity(
            self.doc.GetElement(floor.LevelId), self.DB))
        result["bbox_mm"] = self.attempt("floor/bbox", lambda: self.bbox(floor))
        result["top_faces"] = self.attempt("floor/top_faces", lambda: self.faces(floor, True), [])
        result["bottom_faces"] = self.attempt("floor/bottom_faces", lambda: self.faces(floor, False), [])
        result["covers"] = {}
        for side, parameter in (("top", "CLEAR_COVER_TOP"), ("bottom", "CLEAR_COVER_BOTTOM"),
                                ("other", "CLEAR_COVER_OTHER")):
            result["covers"][side] = self.attempt("floor/cover/" + side,
                                                  lambda p=parameter: self.cover(floor, p))
        return result

    def bar_type(self, bar_type):
        result = identity(bar_type, self.DB)
        result["nominal_diameter_mm"] = self.mm(bar_type.BarNominalDiameter)
        result["model_diameter_mm"] = self.mm(bar_type.BarModelDiameter)
        return result

    def bar_system(self, system):
        result = identity(system, self.DB)
        result["host_id"] = element_id(system.GetHostId())
        result["system_id"] = element_id(system.SystemId)
        result["quantity"] = int(system.Quantity)
        result["number_of_bar_positions"] = int(system.NumberOfBarPositions)
        result["layout_rule"] = text_type(system.LayoutRule)
        result["max_spacing_mm"] = self.attempt("bar/max_spacing", lambda: self.mm(system.MaxSpacing))
        result["bar_type"] = self.bar_type(self.doc.GetElement(system.GetTypeId()))
        result["parameters"] = self.parameters(system)
        result["positions"] = []
        count = result["number_of_bar_positions"]
        if not 0 <= count <= MAX_BAR_POSITIONS:
            raise ValueError("Bar position limit exceeded; no truncated measurement is accepted")
        for index in range(count):
            def read(i=index):
                exists = bool(system.DoesBarExistAtPosition(i))
                row = {"position_index": i, "exists": exists, "curves": []}
                if exists:
                    # Already includes position AND individual movement. Never transform a second time.
                    curves = system.GetTransformedCenterlineCurves(False, False, False, i)
                    row["curves"] = [self.curve(c) for c in curves]
                    if not row["curves"]:
                        raise ValueError("Existing bar returned empty centerlines")
                return row
            row = self.attempt("bar/{0}/position/{1}".format(result["element_id"], index), read)
            result["positions"].append(row or {"position_index": index, "exists": None,
                                               "curves": [], "status": "read_failed"})
        result["readback_complete"] = (
            all(p["exists"] is not None for p in result["positions"])
            and sum(p["exists"] is True for p in result["positions"]) == result["quantity"]
        )
        if not result["readback_complete"]:
            self.issues.append({"section": "bar/{0}/quantity".format(result["element_id"]),
                                "message": "Position readback is incomplete or disagrees with Quantity"})
        return result

    def area(self, area, floor_report):
        result = identity(area, self.DB)
        result["mark"] = self.mark(area)
        result["host_id"] = element_id(area.GetHostId())
        result["parameters"] = self.parameters(area)
        # The requested limit belongs to the Area, not its generated RebarInSystem.
        # In the real 2024 report the parent returns 100, the child MaxSpacing 96.875.
        result["requested_top_major_spacing_mm"] = self.attempt(
            "area/requested_top_major_spacing", lambda: self.mm(area.get_Parameter(
                self.DB.BuiltInParameter.REBAR_SYSTEM_SPACING_TOP_DIR_1).AsDouble()))
        result["boundary_curves"] = self.attempt("area/boundary", lambda: [
            self.curve(self.doc.GetElement(i).Curve) for i in area.GetBoundaryCurveIds()], [])
        ids = list(area.GetRebarInSystemIds())
        result["rebar_in_system_ids"] = [element_id(i) for i in ids]
        result["bar_systems"] = []
        for system_id in ids:
            system = self.attempt("area/system/{0}".format(element_id(system_id)),
                                  lambda i=system_id: self.bar_system(self.doc.GetElement(i)))
            if system is not None:
                result["bar_systems"].append(system)
        systems = result["bar_systems"]
        complete = bool(ids) and len(systems) == len(ids) and all(
            s["readback_complete"] and s["host_id"] == result["host_id"]
            and s["system_id"] == result["element_id"] for s in systems)
        if not complete:
            self.issues.append({"section": "area/readback", "message":
                                "Missing, incomplete or mismatched rebar systems; count is unknown"})
        bars = []
        for system in systems:
            for position in system["positions"]:
                if position["exists"] is not True:
                    continue
                bar = dict(position)
                bar["system_id"] = system["element_id"]
                bar["nominal_diameter_mm"] = system["bar_type"]["nominal_diameter_mm"]
                bar["model_diameter_mm"] = system["bar_type"]["model_diameter_mm"]
                for side in ("top", "bottom"):
                    bar[side + "_face_depths"] = plane_depths(
                        bar["curves"], (floor_report or {}).get(side + "_faces", []),
                        bar["model_diameter_mm"] / 2.0)
                bars.append(bar)
        result["readback_complete"] = complete
        result["physical_bars"] = bars
        result["physical_bar_count"] = len(bars) if complete else None
        result["measurement"] = (measure_parallel_bars(bars) if complete else {
            "status": "not_checked", "reason": "Incomplete/empty system readback"})
        result["reference_comparison"] = compare_reference(
            result["measurement"], result["boundary_curves"], bars, systems,
            result["requested_top_major_spacing_mm"])
        return result

    def cad(self, instance):
        result = identity(instance, self.DB)
        result["is_linked"] = bool(instance.IsLinked)
        result["owner_view_id"] = element_id(instance.OwnerViewId)
        result["bbox_mm"] = self.attempt("cad/bbox", lambda: self.bbox(instance))
        result["instance_transform"] = self.attempt("cad/transform", lambda: self.transform(
            instance.GetTransform()))
        result["total_transform"] = self.attempt("cad/total_transform", lambda: self.transform(
            instance.GetTotalTransform()))
        cad_type = self.doc.GetElement(instance.GetTypeId())
        result["type"] = identity(cad_type, self.DB)
        if instance.IsLinked:
            result["linked_file_status"] = self.attempt("cad/link_status", lambda: text_type(
                cad_type.GetExternalFileReference().GetLinkedFileStatus()))
        result["coordinate_calibration"] = "not_checked"
        result["note"] = ("Transforms describe Revit instance geometry, not a calibrated raw-DXF "
                          "mapping. Source units and matching control points must still be checked.")
        return result


def collect_report(document, DB, cad_instance=None, runtime=None):
    probe = Probe(document, DB)
    report = {
        "schema_version": "revit-reference-probe/v1", "probe_version": PROBE_VERSION,
        "created_utc": datetime.datetime.utcnow().isoformat() + "Z",
        "units": "mm", "coordinate_system": "revit-internal-origin-and-axes",
        "runtime": runtime or {"python": platform.python_version()},
        "document": {"title": text_type(document.Title), "is_workshared": bool(document.IsWorkshared)},
        "revit": {}, "read_only": True, "placement_eligible": False,
        "not_checked": ["source-dxf-unit-and-control-point-calibration", "background-grid-phase",
                        "xy-layer-order", "host-cover-openings-containment", "3d-collisions",
                        "composite-demand-coverage", "engineering-acceptance"],
    }
    for attribute in ("VersionName", "VersionNumber", "VersionBuild", "SubVersionNumber"):
        report["revit"][attribute] = probe.attempt("revit/" + attribute,
            lambda a=attribute: text_type(getattr(document.Application, a)))
    if report["revit"]["VersionNumber"] != "2024":
        probe.issues.append({"section": "revit/version", "message": "Target is Revit 2024.3.4"})
    report["expected_reference"] = {
        "revit_subversion": "2024.3.4", "revit_build": "20250918_1515(x64)",
        "floor_id": 407801, "floor_mark": "TEST_SLAB_01",
        "area_id": 407878, "area_mark": "AR_TEST_TOP_X_001",
    }
    floor = probe.attempt("reference/floor", lambda: probe.resolve_reference(
        407801, DB.Floor, "TEST_SLAB_01"))
    area = probe.attempt("reference/area", lambda: probe.resolve_reference(
        407878, DB.Structure.AreaReinforcement, "AR_TEST_TOP_X_001"))
    report["floor"] = probe.attempt("floor", lambda: probe.floor(floor)) if floor is not None else None
    area_host_id = (probe.attempt("reference/area_host", lambda: element_id(area.GetHostId()))
                    if area is not None else None)
    if area is not None and (floor is None or area_host_id != element_id(floor.Id)):
        probe.issues.append({"section": "reference/host", "message": "Area host is not the reference floor"})
        area = None
    report["area"] = (probe.attempt("area", lambda: probe.area(area, report["floor"]))
                      if area is not None else None)
    report["cad"] = probe.attempt("cad", lambda: probe.cad(cad_instance)) if cad_instance is not None else None
    if cad_instance is None:
        probe.issues.append({"section": "cad", "message": "No CAD instance selected"})
    report["reference_bar_types"] = []
    for number in (165160, 165161, 165163):
        row = probe.attempt("reference/bar_type/{0}".format(number),
                            lambda n=number: probe.bar_type(document.GetElement(DB.ElementId(n))))
        if row is not None:
            report["reference_bar_types"].append(row)
    report["issues"] = probe.issues
    report["status"] = "partial" if probe.issues else "collected"
    if report["area"] is not None and not report["area"]["readback_complete"]:
        report["status"] = "partial"
    return report
