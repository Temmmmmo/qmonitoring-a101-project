# -*- coding: utf-8 -*-
"""Bounded native-host reinforcement inventory, READ ONLY, IronPython 2.7.

Actual transformed centerlines already include distribution and individual moves.
Never transform again. This is evidence, not an engineering/placement certificate.
"""
from __future__ import division, unicode_literals

import datetime
import math
import platform

from qm_revit_probe import Probe, element_id, identity, text_type

VERSION = "0.1.0"
SCHEMA = "revit-working-rebar-probe/v1"
MAX_SCANNED_ELEMENTS = 250000
MAX_HOST_ELEMENTS = 5000
MAX_TOTAL_POSITIONS = 30000
MAX_CURVES = 300000
MAX_TESSELLATION_POINTS = 1000000
MAX_POINTS_PER_CURVE = 2048
MAX_PARAMETERS_PER_ELEMENT = 512
MAX_WORKSETS = 10000
MAX_LINKS = 1000


def _finite(value):
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        raise ValueError("Nonfinite native geometry value")
    return value


def _point(probe, point):
    return [_finite(probe.mm(point.X)), _finite(probe.mm(point.Y)), _finite(probe.mm(point.Z))]


def _vector(point):
    return [_finite(point.X), _finite(point.Y), _finite(point.Z)]


class ReadBudget(object):
    def __init__(self):
        self.scanned = 0
        self.positions = 0
        self.curves = 0
        self.points = 0

    def take(self, key, amount, maximum):
        if amount < 0 or getattr(self, key) + amount > maximum:
            raise ValueError("Read limit exceeded: " + key + "; remaining geometry is UNKNOWN")
        setattr(self, key, getattr(self, key) + amount)


def read_exact_curve(probe, curve, budget):
    """Native Line/Arc plus diagnostic tessellation; unsupported curves stay explicit."""
    budget.take("curves", 1, MAX_CURVES)
    kind = text_type(curve.GetType().Name)
    bounded = bool(curve.IsBound)
    result = {"kind": kind, "is_bound": bounded, "exact_geometry_supported": False,
        "tessellated_points_mm": [], "tessellation": "Revit approximation; NOT exact geometry"}
    if not bounded:
        raise ValueError("Unbounded centerline curve is unsupported: " + kind)
    result.update({"start_mm": _point(probe, curve.GetEndPoint(0)),
        "end_mm": _point(probe, curve.GetEndPoint(1)),
        "mid_mm": _point(probe, curve.Evaluate(0.5, True)),
        "length_mm": _finite(probe.mm(curve.Length))})
    if result["length_mm"] <= 0:
        raise ValueError("Nonpositive centerline curve length")
    if isinstance(curve, probe.DB.Line):
        result["exact_geometry_supported"] = True
    elif isinstance(curve, probe.DB.Arc):
        result.update({"center_mm": _point(probe, curve.Center),
            "radius_mm": _finite(probe.mm(curve.Radius)), "normal": _vector(curve.Normal),
            "x_direction": _vector(curve.XDirection), "y_direction": _vector(curve.YDirection),
            "start_parameter": _finite(curve.GetEndParameter(0)),
            "end_parameter": _finite(curve.GetEndParameter(1)), "parameter_units": "radians"})
        if result["radius_mm"] <= 0:
            raise ValueError("Nonpositive arc radius")
        result["exact_geometry_supported"] = True
    else:
        probe.issues.append({"section": "curve/type", "message":
            "Unsupported exact centerline type: " + kind + "; tessellation is evidence only"})
    for point in curve.Tessellate():
        if len(result["tessellated_points_mm"]) >= MAX_POINTS_PER_CURVE:
            raise ValueError("Per-curve tessellation limit exceeded")
        budget.take("points", 1, MAX_TESSELLATION_POINTS)
        result["tessellated_points_mm"].append(_point(probe, point))
    if len(result["tessellated_points_mm"]) < 2:
        raise ValueError("Incomplete centerline tessellation")
    return result


def _parameters(probe, element):
    count = 0
    for unused in element.Parameters:
        count += 1
        if count > MAX_PARAMETERS_PER_ELEMENT:
            raise ValueError("Parameter read limit exceeded")
    rows = probe.parameters(element)
    for row in rows:
        for key in ("value_internal", "length_mm"):
            if key in row:
                _finite(row[key])
    return rows


def _metadata(probe, element, section):
    result = {"element_id": element_id(element.Id), "unique_id": text_type(element.UniqueId),
        "class": text_type(element.GetType().FullName), "engineering_role": "unclassified"}
    result["identity"] = probe.attempt(section + "/identity", lambda: identity(element, probe.DB))
    result["mark"] = probe.attempt(section + "/mark", lambda: probe.mark(element))
    result["parameters"] = probe.attempt(section + "/parameters", lambda: _parameters(probe, element))
    for name in ("WorksetId", "OwnerViewId", "CreatedPhaseId", "DemolishedPhaseId", "GroupId"):
        result[name] = probe.attempt(section + "/" + name,
            lambda n=name: element_id(getattr(element, n)))
    return result


def _materials(probe, element):
    result = []
    for material_id in element.GetMaterialIds(False):
        if len(result) >= 100:
            raise ValueError("Material read limit exceeded")
        result.append(identity(probe.doc.GetElement(material_id), probe.DB))
    return {"materials": result, "steel_grade_inferred": False,
        "note": "Native material names/IDs only; no grade inferred from diameter, name or mark"}


def _bar_type(probe, bar, cache):
    type_id = element_id(bar.GetTypeId())
    if type_id not in cache:
        element = probe.doc.GetElement(bar.GetTypeId())
        data = probe.bar_type(element)
        data["nominal_diameter_mm"] = _finite(data["nominal_diameter_mm"])
        data["model_diameter_mm"] = _finite(data["model_diameter_mm"])
        if min(data["nominal_diameter_mm"], data["model_diameter_mm"]) <= 0:
            raise ValueError("Nonpositive RebarBarType diameter")
        data["parameters"] = _parameters(probe, element)
        data["materials"] = probe.attempt("bar_type/materials/" + text_type(type_id),
            lambda: _materials(probe, element))
        cache[type_id] = data
    return cache[type_id]


def _read_bar(probe, bar, is_system, budget, cache):
    section = "bar/" + text_type(element_id(bar.Id))
    start_issues = len(probe.issues)
    result = _metadata(probe, bar, section)
    result.update({"host_id": element_id(bar.GetHostId()), "quantity": int(bar.Quantity),
        "number_of_bar_positions": int(bar.NumberOfBarPositions),
        "layout_rule": text_type(bar.LayoutRule), "positions": [],
        "readback_complete": False, "physical_bar_count": None,
        "actual_xyz_source": "GetTransformedCenterlineCurves; NO additional transform",
        "layer_and_direction": "unclassified; a shaped bar may span several Z levels",
        "phase_origin_mm": None, "phase_status": "not_inferred_from_spacing_or_mark"})
    result["bar_type"] = probe.attempt(section + "/type", lambda: _bar_type(probe, bar, cache))
    result["materials"] = probe.attempt(section + "/materials", lambda: _materials(probe, bar))
    if is_system:
        result["system_id"] = probe.attempt(section + "/system_id", lambda: element_id(bar.SystemId))
    else:
        result["shape_id"] = probe.attempt(section + "/shape", lambda: element_id(bar.GetShapeId()))
        result["hook_type_ids"] = []
        for end in (0, 1):
            result["hook_type_ids"].append(probe.attempt(section + "/hook/" + text_type(end),
                lambda i=end: element_id(bar.GetHookTypeId(i))))
    count = result["number_of_bar_positions"]
    if count < 0 or result["quantity"] < 0 or result["quantity"] > count:
        raise ValueError("Inconsistent Quantity / NumberOfBarPositions")
    # Reserve the complete set. Never truncate a set then declare its count exact.
    try:
        budget.take("positions", count, MAX_TOTAL_POSITIONS)
    except ValueError as exc:
        probe.issues.append({"section": section, "message": text_type(exc)})
        result["unread_position_count"] = count
        return result
    existing_count = 0
    complete_geometry_count = 0
    for index in range(count):
        row = {"position_index": index, "position_key": [result["unique_id"], index],
            "exists": None, "curves": [], "status": "read_failed"}
        before = len(probe.issues)
        exists = probe.attempt(section + "/exists/" + text_type(index),
            lambda i=index: bool(bar.DoesBarExistAtPosition(i)))
        row["exists"] = exists
        if exists is False:
            row["status"] = "excluded"
        elif exists is True:
            existing_count += 1
            try:
                if is_system:
                    curves = bar.GetTransformedCenterlineCurves(False, False, False, index)
                else:
                    curves = bar.GetTransformedCenterlineCurves(False, False, False,
                        probe.DB.Structure.MultiplanarOption.IncludeAllMultiplanarCurves, index)
                for curve in curves:
                    row["curves"].append(read_exact_curve(probe, curve, budget))
                if not row["curves"]:
                    raise ValueError("Existing position returned no centerline curves")
                row["centerline_length_mm"] = 0.0
                for curve in row["curves"]:
                    row["centerline_length_mm"] += curve["length_mm"]
                if len(probe.issues) == before:
                    row["status"] = "collected"
                    complete_geometry_count += 1
            except Exception as exc:
                probe.issues.append({"section": section + "/position/" + text_type(index),
                    "error_type": type(exc).__name__, "message": text_type(exc)})
        result["positions"].append(row)
    result["read_existing_position_count"] = existing_count
    result["complete_geometry_position_count"] = complete_geometry_count
    if existing_count != result["quantity"] or complete_geometry_count != existing_count:
        probe.issues.append({"section": section + "/quantity", "message":
            "Quantity, included positions or complete exact geometry do not agree"})
    result["readback_complete"] = len(probe.issues) == start_issues
    if result["readback_complete"]:
        result["physical_bar_count"] = existing_count
    return result


def _collect(probe, cls, budget):
    collector = probe.DB.FilteredElementCollector(probe.doc).OfClass(cls).WhereElementIsNotElementType()
    try:
        for element in collector:
            budget.take("scanned", 1, MAX_SCANNED_ELEMENTS)
            yield element
    finally:
        collector.Dispose()


def _worksets(probe):
    result = {"status": "not_workshared", "user_worksets": [], "closed_user_worksets": []}
    if not probe.doc.IsWorkshared:
        return result
    result["status"] = "collected"
    collector = probe.DB.FilteredWorksetCollector(probe.doc).OfKind(probe.DB.WorksetKind.UserWorkset)
    try:
        for item in collector:
            if len(result["user_worksets"]) >= MAX_WORKSETS:
                raise ValueError("Workset read limit exceeded")
            row = {"id": element_id(item.Id), "unique_id": text_type(item.UniqueId),
                "name": text_type(item.Name), "is_open": bool(item.IsOpen)}
            result["user_worksets"].append(row)
            if not row["is_open"]:
                result["closed_user_worksets"].append(row)
    finally:
        collector.Dispose()
    return result


def _parents(probe, floor_id, budget):
    result = []
    for class_name in ("AreaReinforcement", "PathReinforcement"):
        try:
            cls = getattr(probe.DB.Structure, class_name)
            for element in _collect(probe, cls, budget):
                section = "parent/" + text_type(element_id(element.Id))
                host_id = probe.attempt(section + "/host", lambda e=element: element_id(e.GetHostId()))
                if host_id != floor_id:
                    continue
                if len(result) >= MAX_HOST_ELEMENTS:
                    raise ValueError("Parent element read limit exceeded")
                row = _metadata(probe, element, section)
                row["host_id"] = host_id
                row["child_ids"] = probe.attempt(section + "/children", lambda e=element: _child_ids(e))
                result.append(row)
        except Exception as exc:
            probe.issues.append({"section": "parents/" + class_name, "message": text_type(exc)})
    return result


def _child_ids(element):
    result = []
    for value in element.GetRebarInSystemIds():
        if len(result) >= MAX_HOST_ELEMENTS:
            raise ValueError("Parent child read limit exceeded")
        number = element_id(value)
        if number in result:
            raise ValueError("Parent lists the same physical child more than once")
        result.append(number)
    return result


def _other_scope(probe, floor_id, budget):
    result = {"unsupported_host_elements": [], "links": [],
        "links_geometry_read": False, "other_host_rebar_geometry_read": False,
        "whole_model_collision_inventory_complete": False}
    for name in ("FabricArea", "FabricSheet", "RebarContainer"):
        try:
            cls = getattr(probe.DB.Structure, name)
            for element in _collect(probe, cls, budget):
                host_id = probe.attempt(name + "/host/" + text_type(element_id(element.Id)),
                    lambda e=element, n=name: element_id(e.HostId if n in ("FabricArea", "FabricSheet") else e.GetHostId()))
                if host_id == floor_id:
                    if len(result["unsupported_host_elements"]) >= MAX_HOST_ELEMENTS:
                        raise ValueError("Unsupported host element limit exceeded")
                    result["unsupported_host_elements"].append(identity(element, probe.DB))
                    probe.issues.append({"section": name, "message":
                        "Native host has unsupported reinforcement; geometry/count are unknown"})
        except Exception as exc:
            probe.issues.append({"section": "scope/" + name, "message": text_type(exc)})
    try:
        for link in _collect(probe, probe.DB.RevitLinkInstance, budget):
            if len(result["links"]) >= MAX_LINKS:
                raise ValueError("Link inventory limit exceeded")
            result["links"].append({"element_id": element_id(link.Id),
                "unique_id": text_type(link.UniqueId), "name": text_type(link.Name),
                "is_loaded": link.GetLinkDocument() is not None, "geometry_read": False})
    except Exception as exc:
        probe.issues.append({"section": "scope/links", "message": text_type(exc)})
    return result


def _host(probe, floor):
    result = identity(floor, probe.DB)
    result["mark"] = probe.attempt("host/mark", lambda: probe.mark(floor))
    result["type"] = probe.attempt("host/type", lambda: identity(probe.doc.GetElement(floor.GetTypeId()), probe.DB))
    result["level"] = probe.attempt("host/level", lambda: identity(probe.doc.GetElement(floor.LevelId), probe.DB))
    result["bbox_mm"] = probe.attempt("host/bbox", lambda: _host_bbox(probe, floor))
    result["covers"] = {}
    for side, key in (("top", "CLEAR_COVER_TOP"), ("bottom", "CLEAR_COVER_BOTTOM"), ("other", "CLEAR_COVER_OTHER")):
        result["covers"][side] = probe.attempt("host/cover/" + side, lambda k=key: _host_cover(probe, floor, k))
    result["solid_geometry"] = "not_read; bind the separate Working Host Probe by native document/host identity"
    return result


def _host_bbox(probe, floor):
    result = probe.bbox(floor)
    for key in ("min_mm", "max_mm"):
        for value in result[key]:
            _finite(value)
    return result


def _host_cover(probe, floor, key):
    result = probe.cover(floor, key)
    result["distance_mm"] = _finite(result["distance_mm"])
    if result["distance_mm"] < 0:
        raise ValueError("Negative native cover distance")
    return result


def collect_working_rebar_report(document, DB, floor, runtime=None):
    """Read selected native Floor's complete available Rebar/RebarInSystem inventory."""
    if document is None or document.IsFamilyDocument:
        raise ValueError("Open a project document, not a family")
    if not isinstance(floor, DB.Floor) or floor.Document != document:
        raise ValueError("Select a native Floor from the active document")
    probe, budget, cache = Probe(document, DB), ReadBudget(), {}
    report = {"schema_version": SCHEMA, "probe_version": VERSION,
        "created_utc": datetime.datetime.utcnow().isoformat() + "Z", "read_only": True,
        "units": "mm", "coordinate_system": "revit-internal-origin-and-axes",
        "placement_eligible": False, "engineering_approval": False, "status": "partial",
        "host_id": element_id(floor.Id), "host_unique_id": text_type(floor.UniqueId),
        "runtime": runtime or {"python": platform.python_version(), "implementation": platform.python_implementation()},
        "document": {"title": text_type(document.Title), "is_workshared": bool(document.IsWorkshared)},
        "revit": {}, "read_issues": probe.issues, "bars": [], "parents": [],
        "selection_scope": "native selected Floor by GetHostId; document collector, NOT active view or bounding box",
        "not_checked": ["background/additional/edge engineering roles", "phase and XY/Z installation policy",
            "FE capacity/coverage", "anchorage", "host containment", "3D collisions",
            "reinforcement of OTHER hosts or linked models", "engineering acceptance"],
        "limits": {"scanned_elements": MAX_SCANNED_ELEMENTS, "host_elements": MAX_HOST_ELEMENTS,
            "total_positions": MAX_TOTAL_POSITIONS, "curves": MAX_CURVES,
            "tessellation_points": MAX_TESSELLATION_POINTS, "points_per_curve": MAX_POINTS_PER_CURVE}}
    for key, attribute in (("path", "PathName"), ("is_read_only", "IsReadOnly"),
            ("is_modifiable", "IsModifiable"), ("is_modified_before", "IsModified"),
            ("is_detached", "IsDetached"), ("is_cloud", "IsModelInCloud")):
        report["document"][key] = probe.attempt("document/" + key,
            lambda a=attribute: text_type(getattr(document, a)) if a == "PathName" else bool(getattr(document, a)))
    report["document"]["project_information_unique_id"] = probe.attempt("document/project_identity",
        lambda: text_type(document.ProjectInformation.UniqueId))
    if document.IsWorkshared:
        report["document"]["central_model_path"] = probe.attempt("document/central_identity",
            lambda: text_type(DB.ModelPathUtils.ConvertModelPathToUserVisiblePath(document.GetWorksharingCentralModelPath())))
    for attribute in ("VersionName", "VersionNumber", "VersionBuild", "SubVersionNumber"):
        report["revit"][attribute] = probe.attempt("revit/" + attribute,
            lambda a=attribute: text_type(getattr(document.Application, a)))
    report["host"] = probe.attempt("host", lambda: _host(probe, floor))
    report["worksharing"] = probe.attempt("worksets", lambda: _worksets(probe))
    if report["worksharing"] and report["worksharing"]["closed_user_worksets"]:
        probe.issues.append({"section": "worksets", "message":
            "Closed user worksets may hide host reinforcement; they were NOT opened"})
    report["parents"] = _parents(probe, report["host_id"], budget)
    seen, by_id = {}, {}
    for name in ("Rebar", "RebarInSystem"):
        try:
            cls = getattr(DB.Structure, name)
            for element in _collect(probe, cls, budget):
                section = name + "/" + text_type(element_id(element.Id))
                host_id = probe.attempt(section + "/host", lambda e=element: element_id(e.GetHostId()))
                if host_id != report["host_id"]:
                    continue
                uid, eid = text_type(element.UniqueId), element_id(element.Id)
                if uid in seen:
                    if seen[uid] != eid:
                        raise ValueError("Duplicate UniqueId has conflicting ElementId")
                    continue
                if len(seen) >= MAX_HOST_ELEMENTS:
                    raise ValueError("Host reinforcement element limit exceeded")
                seen[uid] = eid
                data = probe.attempt(section, lambda e=element, system=name == "RebarInSystem":
                    _read_bar(probe, e, system, budget, cache))
                if data is None:
                    data = {"element_id": eid, "unique_id": uid, "host_id": host_id,
                        "readback_complete": False, "positions": [], "status": "read_failed"}
                report["bars"].append(data)
                by_id[eid] = data
        except Exception as exc:
            probe.issues.append({"section": "collector/" + name, "message": text_type(exc)})
    parent_by_id = {}
    for parent in report["parents"]:
        parent_by_id[parent["element_id"]] = parent
        child_ids = parent["child_ids"]
        if not child_ids:
            probe.issues.append({"section": "parent/children", "message":
                "Area/Path has no readable physical children; do NOT infer zero reinforcement"})
        for child_id in child_ids or []:
            child = by_id.get(child_id)
            if child is None or child.get("system_id") != parent["element_id"]:
                probe.issues.append({"section": "parent/child-binding", "message":
                    "Missing or mismatched physical child " + text_type(child_id)})
    for bar in report["bars"]:
        if "system_id" in bar:
            parent = parent_by_id.get(bar["system_id"])
            if parent is None or bar["element_id"] not in (parent.get("child_ids") or []):
                probe.issues.append({"section": "bar/parent-binding", "message":
                    "RebarInSystem parent missing or does not list this child"})
    report["scope"] = _other_scope(probe, report["host_id"], budget)
    report["document"]["is_modified_after"] = probe.attempt("document/is_modified_after", lambda: bool(document.IsModified))
    before, after = report["document"]["is_modified_before"], report["document"]["is_modified_after"]
    report["document"]["modification_flag_unchanged"] = before == after if before is not None and after is not None else None
    if report["document"]["modification_flag_unchanged"] is not True:
        probe.issues.append({"section": "document", "message": "Document modification flag changed or is unreadable"})
    complete = not probe.issues
    read_count, complete_count, excluded_count = 0, 0, 0
    for bar in report["bars"]:
        for position in bar["positions"]:
            if position["exists"] is True:
                read_count += 1
            elif position["exists"] is False:
                excluded_count += 1
            if position["status"] == "collected":
                complete_count += 1
        if not bar["readback_complete"]:
            complete = False
    report["summary"] = {"host_reinforcement_element_count_read": len(report["bars"]),
        "read_existing_position_count": read_count, "exact_geometry_position_count": complete_count,
        "excluded_position_count": excluded_count, "physical_bar_count": read_count if complete else None,
        "native_host_inventory_complete": complete, "parent_systems_not_counted_as_bars": True,
        "engineering_roles_classified": False, "whole_model_collision_inventory_complete": False}
    report["resource_usage"] = {"scanned_elements": budget.scanned, "positions": budget.positions,
        "curves": budget.curves, "tessellation_points": budget.points}
    if complete:
        report["status"] = "collected"
    return report
