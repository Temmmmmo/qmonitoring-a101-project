# -*- coding: utf-8 -*-
"""Physical-bar diagnostic contract, not a LayoutZone export. IronPython 2.7."""
from __future__ import division

import copy
import hashlib
import json
import math
import os

from qm_core_trial import label
from qm_plate_packet import DIRECTIONS, material_key as material_key, validate_packet as validate_execution
from qm_trial_input import _reject_constant, _unique_object, exact_keys, number

VERSION = "0.1.1"
SCHEMA = "physical-bar-plan-trial/v1"
MAX_BYTES = 8 * 1024 * 1024
TOLERANCE = 1e-6


def _digest(value):
    label(value)
    if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Invalid source SHA256")


def _array(value, minimum, maximum, description):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError("Invalid bounded array: " + description)


def _projection(data):
    """Private transport only: synthetic group ids NEVER represent source zones."""
    result = {key: copy.deepcopy(data[key]) for key in (
        "mode", "units", "placement_eligible", "case_id", "source_report_sha256", "source_blockers")}
    result["schema_version"] = "qmonitoring-full-plate-trial/v1"
    result["directions"] = []
    for item in data["directions"]:
        runs = []
        for run in item["runs"]:
            value = {key: copy.deepcopy(run[key]) for key in (
                "id", "steel_class", "diameter_mm", "start_xy_mm", "end_xy_mm", "bar_count", "spacing_mm")}
            value.update(zone_id=run["execution_group_id"], component_index=0)
            runs.append(value)
        result["directions"].append({"direction": item["direction"], "source": item["source"], "runs": runs})
    result["expected"] = {key: data["expected"][key] for key in (
        "run_count", "physical_bar_count", "additional_mass_kg")}
    result["expected"]["zone_count"] = data["expected"]["execution_group_count"]
    return result


def _source_certificates(data):
    zones, references = set(), {}
    _array(data["source_zones"], 1, 512, "source zones")
    for zone in data["source_zones"]:
        exact_keys(zone, ("direction", "zone_id", "components"))
        if zone["direction"] not in DIRECTIONS:
            raise ValueError("Unknown source direction")
        label(zone["zone_id"])
        key = (zone["direction"], zone["zone_id"])
        if key in zones:
            raise ValueError("Duplicate source zone")
        zones.add(key)
        _array(zone["components"], 1, 2, "source components")
        indices = set()
        for part in zone["components"]:
            exact_keys(part, ("component_index", "source_bar_count", "diameter_mm", "required_interval_mm",
                             "axis_coordinates_mm", "steel_class", "background_diameter_mm", "background_origin_mm"))
            number(part["component_index"], 0, 1, integer=True)
            if part["component_index"] in indices:
                raise ValueError("Duplicate source component")
            indices.add(part["component_index"])
            number(part["source_bar_count"], 1, 5000, integer=True)
            number(part["diameter_mm"], 6, 40, integer=True)
            number(part["background_diameter_mm"], 6, 40, integer=True)
            number(part["background_origin_mm"], -100000000, 100000000)
            label(part["steel_class"])
            interval = part["required_interval_mm"]
            _array(interval, 2, 2, "source required interval")
            for value in interval:
                number(value, -100000000, 100000000)
            if interval[1] <= interval[0]:
                raise ValueError("Nonpositive source required interval")
            axes = part["axis_coordinates_mm"]
            _array(axes, part["source_bar_count"], part["source_bar_count"], "source axes")
            for index, coordinate in enumerate(axes):
                number(coordinate, -100000000, 100000000)
                if index and coordinate <= axes[index-1] + TOLERANCE:
                    raise ValueError("Source axes must be strictly increasing")
                references[key + (part["component_index"], index)] = (part, coordinate)
            if len(references) > 20000:
                raise ValueError("Source certificate limit exceeded; never truncate")
    return zones, references


def _actual_bars(data, sources):
    used, bar_ids, groups, positions = set(), set(), {}, set()
    actual = {}
    for item in data["directions"]:
        direction = item["direction"]
        axis = 0 if direction.endswith("X") else 1
        for run in item["runs"]:
            exact_keys(run, ("id", "execution_group_id", "steel_class", "diameter_mm", "start_xy_mm",
                             "end_xy_mm", "bar_count", "spacing_mm", "bar_sources"))
            label(run["execution_group_id"])
            lower, upper = run["start_xy_mm"][axis], run["end_xy_mm"][axis]
            group_key = (direction, run["execution_group_id"])
            group_value = (run["steel_class"], run["diameter_mm"], lower, upper)
            if group_key in groups and groups[group_key] != group_value:
                raise ValueError("Execution group must have identical material and longitudinal interval")
            groups[group_key] = group_value
            positions.add((run["steel_class"], run["diameter_mm"], round(upper-lower, 6)))
            _array(run["bar_sources"], run["bar_count"], run["bar_count"], "physical bar provenance")
            for index, ownership in enumerate(run["bar_sources"]):
                exact_keys(ownership, ("bar_id", "source_refs"))
                label(ownership["bar_id"])
                physical_id = (direction, ownership["bar_id"])
                if physical_id in bar_ids:
                    raise ValueError("Duplicate physical bar id within direction")
                bar_ids.add(physical_id)
                coordinate = run["start_xy_mm"][1-axis] + index * run["spacing_mm"]
                _array(ownership["source_refs"], 1, 20000, "source references")
                for ref in ownership["source_refs"]:
                    exact_keys(ref, ("zone_id", "component_index", "bar_index"))
                    label(ref["zone_id"])
                    number(ref["component_index"], 0, 1, integer=True)
                    number(ref["bar_index"], 0, 4999, integer=True)
                    key = (direction, ref["zone_id"], ref["component_index"], ref["bar_index"])
                    if key not in sources or key in used:
                        raise ValueError("Unknown or duplicate source certificate")
                    used.add(key)
                    part, original_coordinate = sources[key]
                    if (abs(coordinate-original_coordinate) > TOLERANCE
                            or run["steel_class"] != part["steel_class"]
                            or run["diameter_mm"] < part["diameter_mm"]):
                        raise ValueError("Source axis, steel class or minimum diameter changed")
                    required = part["required_interval_mm"]
                    anchorage = 40 * run["diameter_mm"]
                    if lower > required[0]-anchorage+TOLERANCE or upper < required[1]+anchorage-TOLERANCE:
                        raise ValueError("Source demand interval or full NEW diameter 40d was shortened")
                    # This diagnostic schema is explicitly limited to background @300.
                    origin = part["background_origin_mm"]
                    nearest = origin + math.floor((coordinate-origin)/300.0 + 0.5) * 300.0
                    old_clear = abs(original_coordinate-nearest) - (part["background_diameter_mm"]+part["diameter_mm"])/2.0
                    new_clear = abs(coordinate-nearest) - (part["background_diameter_mm"]+run["diameter_mm"])/2.0
                    if new_clear < -TOLERANCE or (abs(old_clear) <= TOLERANCE and abs(new_clear) > TOLERANCE):
                        raise ValueError("Source background contact broken or penetrated")
                actual[(run["id"], index)] = {"direction": direction, "coordinate": coordinate,
                    "lower": lower, "upper": upper, "diameter": run["diameter_mm"]}
    if used != set(sources):
        raise ValueError("Incomplete source certificate assignment; never omit original demand")
    return actual, groups, positions


def _joint_pairs(actual):
    pairs = set()
    for direction in DIRECTIONS:
        values = sorted(((key, bar) for key, bar in actual.items() if bar["direction"] == direction),
                        key=lambda item: item[1]["lower"])
        for i, (left_id, left) in enumerate(values):
            for right_id, right in values[i+1:]:
                if right["lower"] >= left["upper"]-TOLERANCE:
                    break
                if abs(left["coordinate"]-right["coordinate"]) < (left["diameter"]+right["diameter"])/2.0-TOLERANCE:
                    pairs.add(tuple(sorted((left_id, right_id))))
    return pairs


def validate_packet(data):
    exact_keys(data, ("schema_version", "mode", "units", "placement_eligible", "case_id", "source_report_sha256",
                      "raw_report_sha256", "source_blockers", "source_zones", "directions", "expected", "manual_joint_tasks"))
    if (data["schema_version"] != SCHEMA or data["mode"] != "commit-readback-rollback"
            or data["units"] != "mm" or data["placement_eligible"] is not False):
        raise ValueError("Expected a physical-bar diagnostic rollback packet, never an approved layout")
    _digest(data["source_report_sha256"])
    _digest(data["raw_report_sha256"])
    _array(data["source_blockers"], 1, 100, "unresolved source blockers")
    exact_keys(data["expected"], ("source_zone_count", "execution_group_count", "run_count",
                                 "physical_bar_count", "additional_mass_kg", "position_count"))
    _array(data["directions"], 4, 4, "four directions")
    for item in data["directions"]:
        exact_keys(item, ("direction", "source", "runs"))
        _array(item["runs"], 0, 2000, "execution runs")
        for run in item["runs"]:
            if not isinstance(run, dict):
                raise ValueError("Expected physical run object")
            exact_keys(run, ("id", "execution_group_id", "steel_class", "diameter_mm", "start_xy_mm",
                             "end_xy_mm", "bar_count", "spacing_mm", "bar_sources"))
    validate_execution(_projection(data))
    zones, sources = _source_certificates(data)
    actual, groups, positions = _actual_bars(data, sources)
    for key, value in (("source_zone_count", len(zones)), ("execution_group_count", len(groups)),
                       ("position_count", len(positions))):
        number(data["expected"][key], value, value, integer=True)
    _array(data["manual_joint_tasks"], 0, 10000, "unresolved joint tasks")
    ids, reported_pairs = set(), set()
    for task in data["manual_joint_tasks"]:
        exact_keys(task, ("id", "direction", "first", "second", "status", "kind"))
        label(task["id"])
        if task["id"] in ids or task["status"] != "unresolved" or task["kind"] != "body_intersection":
            raise ValueError("Duplicate task or unsupported resolution claim")
        ids.add(task["id"])
        ends = []
        for key in ("first", "second"):
            ref = task[key]
            exact_keys(ref, ("run_id", "bar_index"))
            label(ref["run_id"])
            number(ref["bar_index"], 0, 999, integer=True)
            target = (ref["run_id"], ref["bar_index"])
            if target not in actual or task["direction"] != actual[target]["direction"]:
                raise ValueError("Joint task references unknown physical bar or direction")
            ends.append(target)
        pair = tuple(sorted(ends))
        if pair in reported_pairs:
            raise ValueError("Duplicate unresolved intersection pair")
        reported_pairs.add(pair)
    if reported_pairs != _joint_pairs(actual):
        raise ValueError("Manual tasks must list ALL actual same-direction body intersections, without extras")
    return data


def private_execution_packet(data):
    """Validated, derived transport for existing rollback runner; NEVER public output."""
    validate_packet(data)
    return _projection(data)


def load_packet_with_sha256(path):
    if os.path.splitext(path)[1].lower() != ".json":
        raise ValueError("Choose physical-bar-plan-trial.json")
    with open(path, "rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if not content or len(content) > MAX_BYTES:
        raise ValueError("Empty or oversized physical plan packet")
    packet = validate_packet(json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                                        parse_constant=_reject_constant))
    return packet, hashlib.sha256(content).hexdigest()


def load_packet(path):
    return load_packet_with_sha256(path)[0]
