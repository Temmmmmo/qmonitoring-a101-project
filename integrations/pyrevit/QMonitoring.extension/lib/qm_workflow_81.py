# -*- coding: utf-8 -*-
"""TZ 8.1 view-family workflow. Source zones are NOT a physical bar inventory.

IronPython 2.7 compatible; explicit parameter bindings, no guessed family names.
This module never calls an optimizer and never approves structural placement.
"""
from __future__ import division, unicode_literals

import copy
import math
import re

from qm_revit_probe import element_id, element_name, text_type
from qm_revit_source_preview import DIRECTIONS, build_source_primitives, _finite_tree
from qm_trial_input import number

VERSION = "0.1.0"
REQUEST_SCHEMA = "qmonitoring-workflow-request/v1"
REPORT_SCHEMA = "qmonitoring-workflow-view-families/v1"
DISCLAIMER = "ИСХОДНЫЕ ПАРАМЕТРИЧЕСКИЕ ЗОНЫ / НЕ ФИЗИЧЕСКАЯ ПАРТИЯ / НЕ REBAR"
ALGORITHMS = ("genetic-pareto", "composite-pool", "finite-cover")
LENGTH_FIELDS = ("length_mm", "width_mm", "diameter_mm", "step_mm")
ZONE_FIELDS = LENGTH_FIELDS + ("bar_count", "source_id")
MAX_COMPONENTS = 4000


def settings(background_diameter_mm=10, background_step_mm=300,
             anchorage_diameters=40, minimum_zone_fe_count=2,
             algorithm="genetic-pareto", mass_preference=0.5):
    """Validated calculation request, not a claim that an imported result used it."""
    number(background_diameter_mm, 1, 80)
    number(background_step_mm, 1, 2000)
    number(anchorage_diameters, 40, 200)
    number(mass_preference, 0, 1)
    if isinstance(minimum_zone_fe_count, bool) or minimum_zone_fe_count not in (2, 3):
        raise ValueError("Minimum zone width must be explicitly 2 or 3 FE")
    if algorithm not in ALGORITHMS:
        raise ValueError("Unknown optimization strategy")
    return {"background_diameter_mm": background_diameter_mm,
        "background_step_mm": background_step_mm, "anchorage_diameters": anchorage_diameters,
        "minimum_zone_fe_count": minimum_zone_fe_count, "algorithm": algorithm,
        "mass_preference": mass_preference, "objective": "point-3-mass-versus-physical-bar-count",
        "objective_status": "requested-preference-not-a-proven-optimum"}


def _digest(value):
    if not isinstance(value, text_type) or re.match(r"^[0-9a-f]{64}$", value) is None:
        raise ValueError("Expected exact source JSON SHA256")
    return value


def make_request(packet_sha256, direction, config, underlay, document_identity):
    if direction not in DIRECTIONS:
        raise ValueError("Select one of four reinforcement directions")
    _finite_tree(underlay)
    _finite_tree(document_identity)
    if underlay.get("kind") not in ("DXF", "PNG") or underlay.get("alignment_confirmed") is not True:
        raise ValueError("Confirm the selected DXF/PNG underlay alignment explicitly")
    validated = settings(**dict((key, config[key]) for key in (
        "background_diameter_mm", "background_step_mm", "anchorage_diameters",
        "minimum_zone_fe_count", "algorithm", "mass_preference")))
    return {"schema_version": REQUEST_SCHEMA, "version": VERSION,
        "source_packet_sha256": _digest(packet_sha256), "direction": direction,
        "settings": validated, "underlay": copy.deepcopy(underlay),
        "document": copy.deepcopy(document_identity), "status": "awaiting_calculation",
        "calculation_performed": False, "placement_eligible": False,
        "engineering_approval": False,
        "note": "Settings are a NEW calculation request; they do not modify the imported source zones."}


def source_components(packet, direction, offset_xy_mm=(0, 0)):
    """Use original component extents, not demand_bbox and not trimmed bar totals."""
    build_source_primitives(packet, *offset_xy_mm)
    if direction not in DIRECTIONS:
        raise ValueError("Select one direction")
    rows = []
    for source in packet["directions"]:
        key = source["direction"]["layer"]+"-"+source["direction"]["axis"]
        if key != direction:
            continue
        for zone in source["zone_drafts"]:
            for component in zone["components"]:
                bounds = component["bar_axis_bbox_mm"]
                along = (bounds[0], bounds[2]) if key.endswith("X") else (bounds[1], bounds[3])
                across = (bounds[1], bounds[3]) if key.endswith("X") else (bounds[0], bounds[2])
                x, y = (bounds[0]+bounds[2])/2, (bounds[1]+bounds[3])/2
                identity = "{0}/{1}/{2}".format(key, zone["source_zone_id"], component["component_index"])
                row = {"source_id": identity, "source_zone_id": zone["source_zone_id"],
                    "component_index": component["component_index"], "direction": key,
                    "length_mm": component["installed_length_mm"], "width_mm": across[1]-across[0],
                    "diameter_mm": component["diameter_mm"], "step_mm": component["nominal_step_mm"],
                    "bar_count": component["bar_count"], "demand_bbox_mm": copy.deepcopy(zone["demand_bbox_mm"]),
                    "bar_axis_bbox_mm": list(bounds), "axis_coordinates_mm": list(component["axis_coordinates_mm"]),
                    "placement": copy.deepcopy(component.get("placement")),
                    "center_xy_mm": [x+offset_xy_mm[0], y+offset_xy_mm[1]],
                    "rotation_rad": 0 if key.endswith("X") else math.pi/2,
                    "along_interval_mm": list(along)}
                row["annotation"] = ("{0}\n{1}; L={2:g}; осевая ширина={3:g} мм; Ø{4:g}; "
                    "условный шаг={5:g} мм; {6} стержней.\n"
                    "Шаг может быть неравномерным: точные оси сохранены в JSON.\n"
                    "{7}. Не AreaBoundary; не обрезанные стержни.").format(
                        identity, key, row["length_mm"], row["width_mm"], row["diameter_mm"],
                        row["step_mm"], row["bar_count"], DISCLAIMER)
                rows.append(row)
    if not rows or len(rows) > MAX_COMPONENTS:
        raise ValueError("Selected direction is empty or exceeds the component resource cap")
    return rows


def symbol_catalog(document, DB, role):
    category = DB.BuiltInCategory.OST_DetailComponents if role == "zone" else DB.BuiltInCategory.OST_GenericAnnotation
    result = []
    for symbol in DB.FilteredElementCollector(document).OfClass(DB.FamilySymbol).OfCategory(category):
        placement = text_type(symbol.Family.FamilyPlacementType)
        allowed = ("ViewBased", "CurveBasedDetail") if role == "zone" else ("ViewBased",)
        if placement in allowed:
            result.append({"id": element_id(symbol.Id), "unique_id": text_type(symbol.UniqueId),
                "family": element_name(symbol.Family, DB), "type": element_name(symbol, DB),
                "placement": placement, "role": role})
        if len(result) > 10000:
            raise ValueError("Family catalog resource limit exceeded")
    return sorted(result, key=lambda row: (row["family"], row["type"], row["id"]))


def prefix_candidates(catalog, prefix):
    """Only an unambiguous match is automatic; never silently pick first of many."""
    if not isinstance(prefix, text_type) or len(prefix) > 200 or "\x00" in prefix:
        raise ValueError("Invalid family prefix")
    result = []
    for row in catalog:
        if row["family"].lower().startswith(prefix.strip().lower()):
            result.append(copy.deepcopy(row))
    return {"matches": result, "automatic_id": result[0]["id"] if prefix.strip() and len(result) == 1 else None}


def parameter_catalog(instance, DB):
    """Instance parameters only. Type parameters and semantic guesses are forbidden."""
    result = []
    for parameter in instance.Parameters:
        if parameter.IsReadOnly:
            continue
        kind = None
        if parameter.StorageType == DB.StorageType.Double:
            if parameter.Definition.GetDataType() == DB.SpecTypeId.Length:
                kind = "length_mm"
        elif parameter.StorageType == DB.StorageType.Integer:
            if parameter.Definition.GetDataType() != DB.SpecTypeId.Boolean.YesNo:
                kind = "integer"
        elif parameter.StorageType == DB.StorageType.String:
            kind = "text"
        if kind is not None:
            result.append({"id": element_id(parameter.Id), "name": text_type(parameter.Definition.Name), "kind": kind})
    return sorted(result, key=lambda row: (row["kind"], row["name"], row["id"]))


def validate_binding(binding, catalog, role, placement):
    required = ZONE_FIELDS if role == "zone" else ("annotation",)
    if not isinstance(binding, dict) or set(binding) != set(required):
        raise ValueError("Every required family field needs an explicit binding")
    by_id, used = {}, set()
    for parameter in catalog:
        if parameter["id"] in by_id:
            raise ValueError("Ambiguous instance parameter identity")
        by_id[parameter["id"]] = parameter
    for field in required:
        target = binding[field]
        if field == "length_mm" and target == "curve-length" and placement == "CurveBasedDetail":
            continue
        if isinstance(target, bool) or target not in by_id or target in used:
            raise ValueError("Missing, duplicate or non-instance parameter binding: "+field)
        expected = "length_mm" if field in LENGTH_FIELDS else ("integer" if field == "bar_count" else "text")
        if by_id[target]["kind"] != expected:
            raise ValueError("Wrong parameter units/storage for "+field)
        used.add(target)
    return copy.deepcopy(binding)


def bind_values(instance, DB, row, binding, write):
    parameters = {}
    for parameter in instance.Parameters:
        parameters[element_id(parameter.Id)] = parameter
    values = {}
    for field, target in binding.items():
        if target == "curve-length":
            continue
        parameter = parameters[target]
        expected = row[field]
        if field in LENGTH_FIELDS:
            actual = expected/304.8
        elif field == "bar_count":
            actual = int(expected)
        else:
            actual = text_type(expected).replace("\n", "\r")
        if write and parameter.Set(actual) is False:
            raise ValueError("Family parameter rejected value: "+field)
        if field in LENGTH_FIELDS:
            observed = parameter.AsDouble()*304.8
            if abs(observed-expected) > 0.01:
                raise ValueError("Family length readback differs: "+field)
        elif field == "bar_count":
            observed = parameter.AsInteger()
            if observed != expected:
                raise ValueError("Family count readback differs")
        else:
            observed = text_type(parameter.AsString() or "").replace("\r\n", "\n").replace("\r", "\n")
            if observed != expected:
                raise ValueError("Family text readback differs: "+field)
        values[field] = observed
    return values
