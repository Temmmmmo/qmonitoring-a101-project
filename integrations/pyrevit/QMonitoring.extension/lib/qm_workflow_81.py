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
from qm_revit_source_preview import DIRECTIONS, build_source_primitives, _finite_tree, _polygon, _bbox, _integer, _text
from qm_trial_input import number

VERSION = "0.1.3"
REQUEST_SCHEMA = "qmonitoring-workflow-request/v1"
REPORT_SCHEMA = "qmonitoring-workflow-view-families/v1"
DISCLAIMER = "ИСХОДНЫЕ ПАРАМЕТРИЧЕСКИЕ ЗОНЫ / НЕ ФИЗИЧЕСКАЯ ПАРТИЯ / НЕ REBAR"
ALGORITHMS = ("genetic-pareto", "bsp", "greedy-priority")
LENGTH_FIELDS = ("length_mm", "width_mm", "diameter_mm", "step_mm")
ZONE_FIELDS = LENGTH_FIELDS + ("bar_count", "source_id")
MAX_COMPONENTS = 4000
KNOWN_ZONE_PROFILE = "eu-zone-one-bar-v1"
KNOWN_ZONE_FAMILY = u"ЭУ_Зона доп.армирования"
KNOWN_ZONE_TYPE = u"Один стержень с зоной"
KNOWN_ANNOTATION_FAMILY = u"ТипАн_Фоновое армирование"
KNOWN_ZONE_BINDINGS = {"length_mm": u"мод_Габарит А", "width_mm": u"мод_Габарит Б",
    "diameter_mm": u"мод_Диаметр стержней", "source_id": u"Комментарии"}
KNOWN_ANNOTATION_BINDINGS = {"annotation": u"Марка (шаг)"}
# Decoration/interoperability parameters that look writable and correctly typed but
# drive annotation graphics or model coordination. Writing an engineering value into
# one silently re-flexes the family (this is how 0.1.1 lost мод_Габарит Б) or corrupts
# the customer's IFC/identity data, so they are never offered and never accepted.
FORBIDDEN_BINDING_TOKENS = (u"ifc", u"кружка", u"до стержня", u"до стрелки", u"uniqueid", u"guid")
FORBIDDEN_BINDING_NAMES = (u"id", u"uniqueid", u"ifcguid")


def forbidden_parameter(name):
    lowered = text_type(name).strip().lower()
    return lowered in FORBIDDEN_BINDING_NAMES or any(token in lowered for token in FORBIDDEN_BINDING_TOKENS)


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
    sources = source_rows(packet, offset_xy_mm)
    if direction not in DIRECTIONS:
        raise ValueError("Select one direction")
    rows = []
    for source in sources:
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
                if packet.get("schema_version") == "qmonitoring-workflow-analysis/v1":
                    row["annotation"] += "\n"+analysis_caption(packet)
                rows.append(row)
    if not rows or len(rows) > MAX_COMPONENTS:
        raise ValueError("Selected direction is empty or exceeds the component resource cap")
    return rows


def analysis_caption(packet):
    checks = packet["checks"]
    return ("СТО-покрытие: {0}; раскрой: {1}; пары в одной плоскости: {2}. "
        "Host/Z/Revit: НЕ ПРОВЕРЕНЫ. Исходные КЭ не удалены.").format(
            checks["coverage"]["status"], checks["stock_cutting"]["status"],
            checks["same_plane_conflicts"]["body_intersection_count"])


def source_rows(packet, offset_xy_mm=(0, 0)):
    """Separate one-direction result adapter; never invent three missing sources."""
    if packet.get("schema_version") != "qmonitoring-workflow-analysis/v1":
        build_source_primitives(packet, *offset_xy_mm)
        return packet["directions"]
    from qm_workflow_81_transport import decode_analysis
    import json
    decode_analysis(json.dumps(packet, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    if len(offset_xy_mm) != 2:
        raise ValueError("Expected XY offset")
    for value in offset_xy_mm:
        number(value, -1e8, 1e8)
    if packet.get("source_stage") != "original-parametric-zones-before-physical-normalization":
        raise ValueError("Not original source components")
    row = packet["source"]
    direction = row["direction"]["layer"]+"-"+row["direction"]["axis"]
    if direction not in DIRECTIONS or direction != packet["request"]["direction"]:
        raise ValueError("Analysis direction differs from the request")
    _bbox(row["source_bbox_mm"])
    cells, zones, legend = row["cells"], row["zone_drafts"], row["legend"]
    if (not isinstance(cells, list) or not 1 <= len(cells) <= 12000
            or not isinstance(zones, list) or len(zones) > 1000
            or not isinstance(legend, list) or not 1 <= len(legend) <= 256):
        raise ValueError("Analysis source resource cap exceeded")
    levels, ids = {}, set()
    for band in legend:
        index = _integer(band["level_index"], 0, 10000)
        if index in levels:
            raise ValueError("Duplicate source level")
        levels[index] = band["aci"]
    for cell in cells:
        identifier = _text(text_type(cell["cell_id"]))
        if identifier in ids or cell["level_index"] not in levels or cell["aci"] != levels[cell["level_index"]]:
            raise ValueError("Duplicate FE or mismatched legend")
        ids.add(identifier)
        _polygon(cell["polygon_mm"])
    if len(cells) != packet["source_cell_count"] or len(zones) != packet["metrics"]["source_zone_count"]:
        raise ValueError("Full source counts differ")
    zone_ids, count, mass, positions = set(), 0, 0, set()
    for zone in zones:
        identity = _text(zone["source_zone_id"])
        if (identity in zone_ids or zone["direction"] != row["direction"]
                or zone["schema_version"] != "reinforcement-zone-revit/v2" or zone["units"] != "mm"):
            raise ValueError("Duplicate/invalid parametric zone identity")
        zone_ids.add(identity)
        _bbox(zone["demand_bbox_mm"])
        if zone["level_index"] not in levels or len(zone["components"]) != 1:
            raise ValueError("Workflow v1 requires a single original addition per zone")
        for part in zone["components"]:
            _integer(part["component_index"], 0, 1000)
            n = _integer(part["bar_count"], 1, 20000)
            axes, bounds = part["axis_coordinates_mm"], part["bar_axis_bbox_mm"]
            d, length = part["diameter_mm"], part["installed_length_mm"]
            number(d, 1, 100)
            number(length, .001, 11700.001)
            number(part["nominal_step_mm"], 1, 10000)
            if not isinstance(axes, list) or len(axes) != n or not isinstance(bounds, list) or len(bounds) != 4:
                raise ValueError("Original component axes/count missing")
            prior = None
            for value in axes:
                number(value, -1e8, 1e8)
                if prior is not None and value <= prior:
                    raise ValueError("Nonincreasing original axes")
                prior = value
            for value in bounds:
                number(value, -1e8, 1e8)
            along = 0 if direction.endswith("X") else 1
            if (abs(bounds[along+2]-bounds[along]-length) > .001
                    or abs(bounds[1-along]-axes[0]) > .001 or abs(bounds[3-along]-axes[-1]) > .001):
                raise ValueError("Source component envelope differs from axes/length")
            count += n
            # Same published kernel convention as bar_schedule/bar_mass_kg:
            # 0.006165*d^2 kg/m. Do not silently substitute a different density
            # formula or loosen the independently checked mass tolerance.
            mass += n*length/1000*.006165*d*d
            positions.add((d, round(length, 6)))
    if (count != packet["metrics"]["physical_bar_count"] or count > 100000
            or abs(mass-packet["metrics"]["additional_mass_kg"]) > .01
            or len(positions) != packet["metrics"]["position_count"]):
        raise ValueError("Full source component mass/count/positions differ")
    analysis_caption(packet)  # Required failure statuses are not optional annotations.
    return [row]


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


def known_semantic_profile(symbol, catalog, role):
    """Return reviewed writable names only; never infer a near match."""
    names = set(row["name"] for row in catalog)
    expected = KNOWN_ZONE_BINDINGS if role == "zone" else KNOWN_ANNOTATION_BINDINGS
    if role == "zone" and (symbol.get("family") != KNOWN_ZONE_FAMILY or symbol.get("type") != KNOWN_ZONE_TYPE):
        return None
    if role == "annotation" and symbol.get("family") != KNOWN_ANNOTATION_FAMILY:
        return None
    if all(name in names for name in expected.values()):
        return {"id": KNOWN_ZONE_PROFILE, "binding": dict((field, next(row["id"] for row in catalog if row["name"] == name)) for field, name in expected.items())}
    return None


def validate_binding(binding, catalog, role, placement, profile_id=None):
    required = ZONE_FIELDS if role == "zone" else ("annotation",)
    optional = ("step_mm", "bar_count") if role == "zone" and profile_id == KNOWN_ZONE_PROFILE else ()
    if not isinstance(binding, dict) or not set(binding).issubset(set(required)) or any(field not in binding for field in required if field not in optional):
        raise ValueError("Every required family field needs an explicit binding")
    by_id, used = {}, set()
    for parameter in catalog:
        if parameter["id"] in by_id:
            raise ValueError("Ambiguous instance parameter identity")
        by_id[parameter["id"]] = parameter
    for field in required:
        if field not in binding:
            continue
        target = binding[field]
        if field == "length_mm" and target == "curve-length" and placement == "CurveBasedDetail":
            continue
        if isinstance(target, bool) or target not in by_id or target in used:
            raise ValueError("Missing, duplicate or non-instance parameter binding: "+field)
        expected = "length_mm" if field in LENGTH_FIELDS else ("integer" if field == "bar_count" else "text")
        if by_id[target]["kind"] != expected:
            raise ValueError("Wrong parameter units/storage for "+field)
        if forbidden_parameter(by_id[target]["name"]):
            raise ValueError("Forbidden semantic parameter binding: {0} -> {1}".format(field, by_id[target]["name"]))
        used.add(target)
    return copy.deepcopy(binding)


def bind_values(instance, DB, row, binding, write):
    parameters = {}
    for parameter in instance.Parameters:
        parameters[element_id(parameter.Id)] = parameter
    # Fixed field order, never dict order: a family may re-flex an already written
    # dimension while a later one is applied, so both the write sequence and the
    # verdict have to be reproducible run to run.
    targets = []
    for field in ZONE_FIELDS+("annotation",):
        if field in binding and binding[field] != "curve-length":
            targets.append((field, parameters[binding[field]], row[field]))
    if write:
        # Every value is written before any readback. A per-field set-then-check
        # reports whichever parameter happened to be verified before its neighbour
        # disturbed it; the whole-instance state is what actually has to hold.
        for field, parameter, expected in targets:
            if field in LENGTH_FIELDS:
                actual = expected/304.8
            elif field == "bar_count":
                actual = int(expected)
            else:
                actual = text_type(expected).replace("\n", "\r")
            if parameter.Set(actual) is False:
                raise ValueError("Family parameter rejected value: {0} -> {1}".format(field, text_type(parameter.Definition.Name)))
    values = {}
    for field, parameter, expected in targets:
        if field in LENGTH_FIELDS:
            observed = parameter.AsDouble()*304.8
            if abs(observed-expected) > 0.01:
                raise ValueError("Family length readback differs: {0}; expected={1:g}; observed={2:g}; parameter={3}; id={4}".format(field, expected, observed, text_type(parameter.Definition.Name), element_id(parameter.Id)))
        elif field == "bar_count":
            observed = parameter.AsInteger()
            if observed != expected:
                raise ValueError("Family count readback differs: expected={0}; observed={1}; parameter={2}; id={3}".format(expected, observed, text_type(parameter.Definition.Name), element_id(parameter.Id)))
        else:
            observed = text_type(parameter.AsString() or "").replace("\r\n", "\n").replace("\r", "\n")
            if observed != expected:
                raise ValueError("Family text readback differs: "+field)
        values[field] = observed
    return values
