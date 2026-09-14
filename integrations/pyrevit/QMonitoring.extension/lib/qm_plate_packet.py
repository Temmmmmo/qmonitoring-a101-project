# -*- coding: utf-8 -*-
"""Complete four-direction trial input and readback. IronPython 2.7; no Revit API."""
from __future__ import division

import json
import math
import os

from qm_core_trial import label
from qm_probe_geometry import bounds, distance
from qm_trial_input import _reject_constant, _unique_object, exact_keys, number

VERSION = "0.6.1"
SCHEMA = "qmonitoring-full-plate-trial/v1"
DIRECTIONS = ("bottom-X", "bottom-Y", "top-X", "top-Y")
MAX_BYTES = 8 * 1024 * 1024
MAX_BARS = 5000
TOLERANCE = 0.1
MASS_PER_MM_PER_DIAMETER_SQUARED = 0.000006165


def material_key(run):
    return "{0}|{1}".format(run["steel_class"], run["diameter_mm"])


def validate_packet(data):
    exact_keys(data, ("schema_version", "mode", "units", "placement_eligible", "case_id",
                      "source_report_sha256", "source_blockers", "directions", "expected"))
    if (data["schema_version"] != SCHEMA or data["mode"] != "commit-readback-rollback"
            or data["units"] != "mm" or data["placement_eligible"] is not False):
        raise ValueError("Expected full-plate rollback-only packet in mm")
    label(data["case_id"])
    digest = data["source_report_sha256"]
    label(digest)
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("Invalid source report SHA256")
    if not isinstance(data["source_blockers"], list) or len(data["source_blockers"]) > 100:
        raise ValueError("Source blockers must be preserved")
    for item in data["source_blockers"]:
        label(item)
    directions = data["directions"]
    if not isinstance(directions, list) or len(directions) != 4:
        raise ValueError("Exactly four complete directions required")
    ids, zones, count, mass = set(), set(), 0, []
    for item, expected_direction in zip(directions, DIRECTIONS):
        exact_keys(item, ("direction", "source", "runs"))
        if item["direction"] != expected_direction:
            raise ValueError("Missing, duplicate or reordered direction")
        label(item["source"])
        if not isinstance(item["runs"], list) or len(item["runs"]) > 2000:
            raise ValueError("Too many Rebar runs")
        for run in item["runs"]:
            exact_keys(run, ("id", "zone_id", "component_index", "steel_class", "diameter_mm",
                             "start_xy_mm", "end_xy_mm", "bar_count", "spacing_mm"))
            for key in ("id", "zone_id", "steel_class"):
                label(run[key])
            if run["id"] in ids:
                raise ValueError("Duplicate Rebar run id")
            ids.add(run["id"])
            zones.add((expected_direction, run["zone_id"]))
            number(run["component_index"], 0, 1, integer=True)
            number(run["diameter_mm"], 6, 40, integer=True)
            number(run["bar_count"], 1, 1000, integer=True)
            number(run["spacing_mm"], 25, 1000)
            for key in ("start_xy_mm", "end_xy_mm"):
                if not isinstance(run[key], list) or len(run[key]) != 2:
                    raise ValueError("Expected a finite XY point")
                for value in run[key]:
                    number(value, -100000000, 100000000)
            axis = 0 if expected_direction.endswith("X") else 1
            a, b = run["start_xy_mm"], run["end_xy_mm"]
            if abs(a[1-axis] - b[1-axis]) > 1e-6:
                raise ValueError("Run is not parallel to its declared direction")
            length = b[axis] - a[axis]
            number(length, 100, 11700.000001)
            count += run["bar_count"]
            mass.append(MASS_PER_MM_PER_DIAMETER_SQUARED * run["diameter_mm"] ** 2 * length * run["bar_count"])
    if not 1 <= count <= MAX_BARS or len(ids) > 2000 or len(zones) > 512:
        raise ValueError("Full packet exceeds run/bar/zone limits; no truncation allowed")
    expected = data["expected"]
    exact_keys(expected, ("zone_count", "run_count", "physical_bar_count", "additional_mass_kg"))
    for key, actual in (("zone_count", len(zones)), ("run_count", len(ids)), ("physical_bar_count", count)):
        number(expected[key], actual, actual, integer=True)
    number(expected["additional_mass_kg"], math.fsum(mass) - 1e-5, math.fsum(mass) + 1e-5)
    return data


def load_packet(path):
    if os.path.splitext(path)[1].lower() != ".json":
        raise ValueError("Choose full-plate-trial.json")
    with open(path, "rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if not content or len(content) > MAX_BYTES:
        raise ValueError("Empty or oversized full-plate packet")
    return validate_packet(json.loads(content.decode("utf-8-sig"),
        object_pairs_hook=_unique_object, parse_constant=_reject_constant))


def make_plan(packet, floor, bar_types, placement):
    validate_packet(packet)
    exact_keys(placement, ("offset_x_mm", "offset_y_mm", "axis_depths_mm", "confirmed"))
    if placement["confirmed"] is not True:
        raise ValueError("Explicit XY and depth confirmation required")
    for key in ("offset_x_mm", "offset_y_mm"):
        number(placement[key], -100000000, 100000000)
    exact_keys(placement["axis_depths_mm"], DIRECTIONS)
    for value in placement["axis_depths_mm"].values():
        number(value, 1, 10000)
    faces = {}
    for side, sign in (("top", 1), ("bottom", -1)):
        items = floor[side + "_faces"]
        if not items or any(item.get("plane") is None for item in items):
            raise ValueError("Horizontal top and bottom host faces are required")
        for item in items:
            for key in ("origin_mm", "normal"):
                if len(item["plane"][key]) != 3:
                    raise ValueError("Expected a 3D host plane")
                for value in item["plane"][key]:
                    number(value, -100000000, 100000000)
        elevations = [item["plane"]["origin_mm"][2] for item in items]
        if max(elevations) - min(elevations) > TOLERANCE or any(
                abs(v - w) > 1e-8 for item in items
                for v, w in zip(item["plane"]["normal"], (0, 0, sign))):
            raise ValueError("Sloping or stepped top/bottom faces require a separate placement profile")
        faces[side] = elevations[0]
    if faces["top"] <= faces["bottom"]:
        raise ValueError("Invalid host face elevations")
    for side in ("top", "bottom", "other"):
        number(floor["covers"][side]["distance_mm"], 0, 1000)
    plans, bars = [], []
    for item in packet["directions"]:
        direction = item["direction"]
        layer, axis_name = direction.split("-")
        axis = 0 if axis_name == "X" else 1
        depth = placement["axis_depths_mm"][direction]
        z = faces[layer] + (depth if layer == "bottom" else -depth)
        for run in item["runs"]:
            bar_type = bar_types[material_key(run)]
            for key in ("nominal_diameter_mm", "model_diameter_mm"):
                number(bar_type[key], run["diameter_mm"] - 0.001, run["diameter_mm"] + 0.001)
            radius = bar_type["model_diameter_mm"] / 2
            if (faces["top"] - z - radius < floor["covers"]["top"]["distance_mm"] - TOLERANCE
                    or z - radius - faces["bottom"] < floor["covers"]["bottom"]["distance_mm"] - TOLERANCE):
                raise ValueError("Explicit axis depth violates top/bottom cover: " + direction)
            axes = []
            for index in range(run["bar_count"]):
                a, b = list(run["start_xy_mm"]), list(run["end_xy_mm"])
                for point in (a, b):
                    point[0] += placement["offset_x_mm"]
                    point[1] += placement["offset_y_mm"]
                    point[1-axis] += index * run["spacing_mm"]
                    point.append(z)
                axes.append({"start_mm": a, "end_mm": b})
                box = bounds((a, b))
                expansion = [0 if i == axis else radius for i in range(3)]
                bars.append({"run_id": run["id"], "direction": direction, "position_index": index,
                    "radius_mm": radius, "axis": axes[-1], "body_bbox_mm": {
                        "min_mm": [v-r for v, r in zip(box["min_mm"], expansion)],
                        "max_mm": [v+r for v, r in zip(box["max_mm"], expansion)]}})
            plans.append(dict(run, direction=direction, host_id=floor["element_id"], bar_type_id=bar_type["element_id"],
                normal=[0, 1, 0] if axis == 0 else [1, 0, 0], axes=axes,
                length_mm=distance(axes[0]["start_mm"], axes[0]["end_mm"]),
                layout_rule="Single" if run["bar_count"] == 1 else "NumberWithSpacing"))
    return {"runs": plans, "bars": bars, "expected": packet["expected"], "faces_mm": faces}


def compare_readback(plan, readback):
    if len(readback["sets"]) != len(plan["runs"]):
        return {"status": "differs", "reason": "missing_or_extra_sets"}
    issues, count, mass = [], 0, []
    for expected, actual in zip(plan["runs"], readback["sets"]):
        errors = []
        for key in ("quantity", "number_of_bar_positions"):
            number(actual[key], 0, 1000, integer=True)
        for key, value in (("host_id", expected["host_id"]), ("quantity", expected["bar_count"]),
                           ("number_of_bar_positions", expected["bar_count"]), ("layout_rule", expected["layout_rule"]),
                           ("hook_type_ids", [-1, -1])):
            if actual.get(key) != value:
                errors.append(key)
        if actual["bar_type"]["element_id"] != expected["bar_type_id"]:
            errors.append("bar_type_id")
        for key in ("nominal_diameter_mm", "model_diameter_mm"):
            number(actual["bar_type"][key], 1, 100)
            if abs(actual["bar_type"][key] - expected["diameter_mm"]) > 0.001:
                errors.append(key)
        bars = actual["bars"]
        count += len(bars)
        if len(bars) != expected["bar_count"] or any(
                len(b["curves"]) != 1 or b["curves"][0]["kind"] != "Line" for b in bars):
            errors.append("physical_bars")
        else:
            for bar in bars:
                for key in ("start_mm", "end_mm"):
                    point = bar["curves"][0][key]
                    if len(point) != 3:
                        raise ValueError("Readback axis must be 3D")
                    for value in point:
                        number(value, -100000000, 100000000)
            lines = sorted(sorted((b["curves"][0]["start_mm"], b["curves"][0]["end_mm"])) for b in bars)
            targets = sorted(sorted((a["start_mm"], a["end_mm"])) for a in expected["axes"])
            if any(distance(a, b) > TOLERANCE for ends, target in zip(lines, targets) for a, b in zip(ends, target)):
                errors.append("absolute_axes")
            for a, b in lines:
                mass.append(MASS_PER_MM_PER_DIAMETER_SQUARED * expected["diameter_mm"] ** 2 * distance(a, b))
            for bar in bars:
                curve = bar["curves"][0]
                number(curve["length_mm"], 0, 11700.1)
                if abs(curve["length_mm"] - distance(curve["start_mm"], curve["end_mm"])) > TOLERANCE:
                    errors.append("api_curve_length")
        if errors:
            issues.append({"run_id": expected["id"], "checks": errors})
    total = math.fsum(mass)
    matched = not issues and count == plan["expected"]["physical_bar_count"] and abs(total - plan["expected"]["additional_mass_kg"]) <= 0.01
    return {"status": "matches" if matched else "differs", "issues": issues,
            "physical_bar_count": count, "mass_from_axes_kg": total, "tolerance_mm": TOLERANCE}
