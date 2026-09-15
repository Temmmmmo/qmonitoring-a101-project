# -*- coding: utf-8 -*-
"""New, isolated drafting-view preview. No structural elements or host writes.

IronPython 2.7 / Revit 2024. The initial input adapter is intentionally separate
from rendering: a later physical-plan format must supply its own validation,
not bypass the source-axis checks of physical-bar-plan-trial/v1.
"""
from __future__ import division, unicode_literals

import copy
import datetime
import hashlib
import json
import math
import os
import traceback

from qm_core_trial import label
from qm_physical_packet import DIRECTIONS, _source_certificates, validate_packet
from qm_revit_probe import Probe, element_id, text_type
from qm_revit_trial import failure_recorder, rollback_scope
from qm_trial_input import _reject_constant, _unique_object, exact_keys, number
from qm_trial_worksharing import classify_document
from qm_revit_source_preview import (SCHEMA as SOURCE_SCHEMA, build_source_primitives,
    draw_source_views, readback_source_views, source_graphic_types, _finite_tree, _normal_text)

VERSION = "0.2.2"
GRAPHIC_BAR_SCHEMA = "graphic-bar-plan-draft/v1"
REPORT_SCHEMA = "revit-graphic-plan-preview-report/v1"
DISCLAIMER = "GRAPHIC PREVIEW / НЕ АРМАТУРА / НЕ ВЫДАЧА"
COLORS = {"contained": (40, 100, 190), "outside": (210, 35, 35),
          "unknown": (210, 125, 0), "intersection": (135, 45, 175),
          "relocated": (20, 140, 125), "physically_cut": (200, 115, 0),
          "host": (70, 75, 80), "source": (180, 185, 190)}
MAX_BARS = 20000
MAX_OUTLINE_SEGMENTS = 30000
MAX_INPUT_BYTES = 32 * 1024 * 1024


class PreviewCancelled(Exception):
    pass


def build_preview_primitives(packet, offset_x_mm, offset_y_mm):
    """Lossless XY adapter; all four directions, including ALL blocked bars.

    ``source_frames`` are certificate extents, NOT the original LayoutZone
    demand_bbox. They are retained in the adapter but not drawn by default.
    Coordinates are Revit internal-frame MILLIMETRES, never CAD elevation.
    """
    if isinstance(packet, dict) and packet.get("schema_version") == SOURCE_SCHEMA:
        return build_source_primitives(packet, offset_x_mm, offset_y_mm)
    if isinstance(packet, dict) and packet.get("schema_version") == GRAPHIC_BAR_SCHEMA:
        return _graphic_bar_primitives(packet, offset_x_mm, offset_y_mm)
    if isinstance(packet, dict) and packet.get("schema_version") == "physical-bar-relocation-draft/v1":
        return _relocation_primitives(packet, offset_x_mm, offset_y_mm)
    validate_packet(packet)
    for value in (offset_x_mm, offset_y_mm):
        number(value, -100000000, 100000000)
    offset = [offset_x_mm, offset_y_mm]
    joint_ends = {(end["run_id"], end["bar_index"])
                  for task in packet["manual_joint_tasks"]
                  for end in (task["first"], task["second"])}
    bars, source_frames = [], []
    for direction in packet["directions"]:
        axis = 0 if direction["direction"].endswith("X") else 1
        for run in direction["runs"]:
            for index, owner in enumerate(run["bar_sources"]):
                start = [run["start_xy_mm"][i] + offset[i] for i in range(2)]
                end = [run["end_xy_mm"][i] + offset[i] for i in range(2)]
                start[1-axis] += index * run["spacing_mm"]
                end[1-axis] += index * run["spacing_mm"]
                bars.append({"direction": direction["direction"], "bar_id": owner["bar_id"],
                    "run_id": run["id"], "position_index": index,
                    "start_xy_mm": start, "end_xy_mm": end, "diameter_mm": run["diameter_mm"],
                    "steel_class": run["steel_class"],
                    "intersection": (run["id"], index) in joint_ends,
                    "source_refs": copy.deepcopy(owner["source_refs"])})
    if len(bars) > MAX_BARS:
        raise ValueError("Preview bar limit exceeded; no partial drawing is permitted")
    for zone in packet["source_zones"]:
        axis = 0 if zone["direction"].endswith("X") else 1
        for part in zone["components"]:
            lo, hi = [0, 0], [0, 0]
            lo[axis], hi[axis] = part["required_interval_mm"]
            lo[1-axis], hi[1-axis] = part["axis_coordinates_mm"][0], part["axis_coordinates_mm"][-1]
            source_frames.append({"direction": zone["direction"], "zone_id": zone["zone_id"],
                "component_index": part["component_index"],
                "min_xy_mm": [lo[i] + offset[i] for i in range(2)],
                "max_xy_mm": [hi[i] + offset[i] for i in range(2)]})
    return {"units": "mm", "coordinate_system": "revit-internal-origin-and-axes",
        "placement_eligible": False, "engineering_approval": False,
        "input_schema": packet["schema_version"], "case_id": packet["case_id"],
        "offset_xy_mm": offset, "bars": bars, "source_frames": source_frames,
        "summary": copy.deepcopy(packet["expected"]),
        "source_blockers": copy.deepcopy(packet["source_blockers"]),
        "intersection_pair_count": len(packet["manual_joint_tasks"])}


def load_preview_input(path):
    """Read once, reject duplicate/nonfinite JSON, return (validated input, SHA256)."""
    if os.path.splitext(path)[1].lower() != ".json":
        raise ValueError("Choose the supplied source graphics, physical plan or relocation draft JSON")
    with open(path, "rb") as stream:
        content = stream.read(MAX_INPUT_BYTES+1)
    if not content or len(content) > MAX_INPUT_BYTES:
        raise ValueError("Empty or oversized graphic preview input")
    def finite_float(value):
        result = float(value)
        if math.isnan(result) or math.isinf(result):
            raise ValueError("Non-finite JSON float, including exponent overflow")
        return result
    data = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
        parse_constant=_reject_constant, parse_float=finite_float)
    _validate_primitives(build_preview_primitives(data, 0, 0))
    return data, hashlib.sha256(content).hexdigest()


def _graphic_inventory(rows, after=False):
    """Strict straight-only transport, not a representation of U/arc projection."""
    if not isinstance(rows, list) or len(rows) != 4:
        raise ValueError("Graphic bar input needs all four complete directions")
    directions, result = set(), {}
    fields = ("id", "steel_class", "diameter_mm", "longitudinal_mm", "coordinate_mm", "source_bar_ids")
    if after:
        fields += ("physically_cut", "original_bar_id", "original_longitudinal_mm", "original_coordinate_mm", "axis_nudged")
    for row in rows:
        exact_keys(row, ("direction", "bars"))
        exact_keys(row["direction"], ("layer", "axis"))
        direction = text_type(row["direction"]["layer"])+"-"+text_type(row["direction"]["axis"])
        if direction not in DIRECTIONS or direction in directions:
            raise ValueError("Unknown/duplicate graphic bar direction")
        directions.add(direction)
        if not isinstance(row["bars"], list) or len(result)+len(row["bars"]) > MAX_BARS:
            raise ValueError("Graphic bar resource limit exceeded; nothing omitted")
        for bar in row["bars"]:
            # Extra shape_kind/segments/arc fields are rejected, not projected away.
            exact_keys(bar, fields)
            label(bar["id"])
            label(bar["steel_class"])
            key = (direction, bar["id"])
            if key in result:
                raise ValueError("Duplicate graphic physical-bar ID")
            number(bar["diameter_mm"], 6, 40, integer=True)
            number(bar["coordinate_mm"], -100000000, 100000000)
            interval = bar["longitudinal_mm"]
            if not isinstance(interval, list) or len(interval) != 2:
                raise ValueError("Expected straight longitudinal interval")
            for coordinate in interval:
                number(coordinate, -100000000, 100000000)
            if interval[0] >= interval[1]:
                raise ValueError("Every drawn piece must have a positive actual length")
            owners = bar["source_bar_ids"]
            if not isinstance(owners, list) or not 1 <= len(owners) <= 1000:
                raise ValueError("Original graphic source ownership required")
            for owner in owners:
                label(owner)
            if len(set(owners)) != len(owners):
                raise ValueError("Duplicate source owner within a graphic bar")
            if after:
                label(bar["original_bar_id"])
                number(bar["original_coordinate_mm"], -100000000, 100000000)
                old_interval = bar["original_longitudinal_mm"]
                if not isinstance(old_interval, list) or len(old_interval) != 2:
                    raise ValueError("Expected explicit original straight interval")
                for coordinate in old_interval:
                    number(coordinate, -100000000, 100000000)
            result[key] = bar
    if not result and not after:
        raise ValueError("Empty physical graphic party is not accepted")
    return result


def _graphic_mass(bars):
    values = []
    for bar in bars.values():
        values.append(0.000006165*bar["diameter_mm"]**2*(bar["longitudinal_mm"][1]-bar["longitudinal_mm"][0]))
    return math.fsum(values)


def _graphic_bar_primitives(packet, offset_x_mm, offset_y_mm):
    """Actual post-trim straight pieces; old source/40d certificates never reused."""
    _finite_tree(packet)
    openings = packet.get("respect_openings") is True
    exact_keys(packet, ("schema_version", "units", "case_id", "placement_eligible", "engineering_approval",
        "geometry_kind", "source_stage", "source_report_sha256", "source_host_report_sha256", "provenance_status",
        "crop_policy_id", "source_to_revit_xy_mm", "binding_source", "coverage_policy",
        "original_FE_geometry_changed", "source_demand_removed", "checks", "before", "after", "directions", "piece_mapping",
        "radius_sized_edge_axis_nudge_enabled", "removed_wholly_external_bars") +
        (("respect_openings", "removed_input_bars") if openings else ()))
    if (packet["schema_version"] != GRAPHIC_BAR_SCHEMA or packet["units"] != "mm" or packet["placement_eligible"] is not False or packet["engineering_approval"] is not False
            or packet["geometry_kind"] != "straight-bars-only" or packet["source_stage"] !=
                ("physically-trimmed-to-outer-and-openings" if openings else "physically-trimmed-to-outer-contour")
            or packet["original_FE_geometry_changed"] is not False or packet["source_demand_removed"] is not False
            or packet["coverage_policy"] != "physical-main-leg-presence-NOT-anchorage"):
        raise ValueError("Expected unapproved straight-only physical trim graphics, no shapes or dropped demand")
    if type(packet["radius_sized_edge_axis_nudge_enabled"]) is not bool:
        raise ValueError("Explicit radius-sized edge-axis nudge flag required")
    for field in ("case_id", "crop_policy_id", "binding_source"):
        label(packet[field])
    if openings and packet["crop_policy_id"] != "user-physical-outer-and-openings-trim-and-split/2026-09-15-v1":
        raise ValueError("Explicit approved openings cut policy required")
    if packet["provenance_status"] != "sha256_recorded":
        raise ValueError("Trim graphics requires explicit source/report SHA metadata")
    for field in ("source_report_sha256", "source_host_report_sha256"):
        digest = packet[field]
        if not isinstance(digest, text_type) or len(digest) != 64:
            raise ValueError("Expected recorded SHA256")
        for char in digest:
            if char not in "0123456789abcdefABCDEF":
                raise ValueError("Invalid recorded SHA256")
    offset = [offset_x_mm, offset_y_mm]
    if not isinstance(packet["source_to_revit_xy_mm"], list) or len(packet["source_to_revit_xy_mm"]) != 2:
        raise ValueError("Explicit source XY binding pair required")
    for value in offset+packet["source_to_revit_xy_mm"]:
        number(value, -100000000, 100000000)
    exact_keys(packet["checks"], ("anchorage_40d", "coverage", "stock_cutting", "outer_boundary", "collisions_3d") +
               (("material_boundary", "openings") if openings else ()))
    for field in ("anchorage_40d", "coverage", "stock_cutting"):
        status = packet["checks"][field]
        if status not in ("pass", "fail", "not_checked"):
            raise ValueError("Explicit 40d, geometric-presence and cutting statuses required")
    for field, count_fields in (("outer_boundary", ("failure_count",)),
            ("collisions_3d", ("proven_pair_count", "uncertain_pair_count"))) + (
            (("material_boundary", ("failure_count",)), ("openings", ("failure_count",))) if openings else ()):
        entry = packet["checks"][field]
        exact_keys(entry, ("status",)+count_fields)
        if entry["status"] not in ("pass", "fail", "not_checked"):
            raise ValueError("Explicit outer-boundary and backend 3D status required")
        count = 0
        for count_field in count_fields:
            if entry["status"] == "not_checked":
                if entry[count_field] is not None:
                    raise ValueError("Unchecked outer/3D counts must be null, not invented zero")
            else:
                number(entry[count_field], 0, 1000000000, integer=True)
                count += entry[count_field]
        if entry["status"] != "not_checked" and (entry["status"] == "pass") != (count == 0):
            raise ValueError("Backend outer/3D status disagrees with its explicit failure counts")
    exact_keys(packet["before"], ("physical_bar_count", "additional_mass_kg", "directions"))
    exact_keys(packet["after"], ("physical_bar_count", "additional_mass_kg", "position_count", "source_zone_count"))
    before = _graphic_inventory(packet["before"]["directions"])
    after = _graphic_inventory(packet["directions"], after=True)
    outer_check, pairs_check = packet["checks"]["outer_boundary"], packet["checks"]["collisions_3d"]
    if outer_check["status"] != "not_checked" and outer_check["failure_count"] > len(after):
        raise ValueError("Outer failure count exceeds the complete actual party")
    if openings:
        for name in ("material_boundary", "openings"):
            entry = packet["checks"][name]
            if entry["status"] == "not_checked" or entry["failure_count"] > len(after):
                raise ValueError("Openings cut requires bounded fresh material checks")
        material_count = packet["checks"]["material_boundary"]["failure_count"]
        if outer_check["status"] == "not_checked" or material_count < max(
                outer_check["failure_count"], packet["checks"]["openings"]["failure_count"]):
            raise ValueError("Material check cannot hide outer or opening failures")
    if pairs_check["status"] != "not_checked" and pairs_check["proven_pair_count"]+pairs_check["uncertain_pair_count"] > len(after)*(len(after)-1)//2:
        raise ValueError("3D pair count exceeds the complete actual party")
    for recorded, inventory in ((packet["before"], before), (packet["after"], after)):
        number(recorded["physical_bar_count"], len(inventory), len(inventory), integer=True)
        mass = _graphic_mass(inventory)
        number(recorded["additional_mass_kg"], 0, 1e10)
        if abs(recorded["additional_mass_kg"]-mass) > max(0.001, mass*1e-9):
            raise ValueError("Graphic mass differs from ALL actual straight lengths; shaped mass is unsupported")
    mapping = packet["piece_mapping"]
    if not isinstance(mapping, list) or len(mapping) != len(before):
        raise ValueError("Every original bar must retain an explicit complete piece mapping")
    removed = packet["removed_input_bars"] if openings else packet["removed_wholly_external_bars"]
    if not isinstance(removed, list) or len(removed) > len(before):
        raise ValueError("Explicit bounded whole-external-bar removal records required")
    removed_ids = set()
    for record in removed:
        exact_keys(record, ("direction", "bar_id", "reason"))
        label(record["reason"])
        label(record["bar_id"])
        key = (record["direction"], record["bar_id"])
        if key not in before or key in removed_ids:
            raise ValueError("Unknown/duplicate wholly-external removal record")
        removed_ids.add(key)
    if openings:
        external = packet["removed_wholly_external_bars"]
        if not isinstance(external, list) or any(row not in removed for row in external):
            raise ValueError("External removals must be a subset of all no-material removals")
        if len(set((row["direction"], row["bar_id"]) for row in external)) != len(external):
            raise ValueError("Duplicate external removal record")
    mapped_originals, mapped_pieces, changed_originals = set(), set(), set()
    for row in mapping:
        exact_keys(row, ("direction", "source_bar_id", "piece_ids", "policy"))
        label(row["policy"])
        if openings and row["policy"] != packet["crop_policy_id"]:
            raise ValueError("Piece mapping uses another cut policy")
        key = (row["direction"], row["source_bar_id"])
        if key not in before or key in mapped_originals:
            raise ValueError("Unknown/duplicate original bar in piece mapping")
        mapped_originals.add(key)
        old = before[key]
        pieces = row["piece_ids"]
        if not isinstance(pieces, list) or len(pieces) > 4096:
            raise ValueError("Expected bounded explicit piece list")
        if (len(pieces) == 0) != (key in removed_ids):
            raise ValueError("Zero-piece originals require an exact wholly-external removal record, without hidden loss")
        intervals, piece_axes = [], set()
        for identifier in pieces:
            label(identifier)
            piece_key = (row["direction"], identifier)
            if piece_key not in after or piece_key in mapped_pieces:
                raise ValueError("Missing/repeated actual cut piece")
            mapped_pieces.add(piece_key)
            bar = after[piece_key]
            if (bar["original_bar_id"] != old["id"] or bar["original_longitudinal_mm"] != old["longitudinal_mm"]
                    or bar["source_bar_ids"] != old["source_bar_ids"] or bar["steel_class"] != old["steel_class"]
                    or bar["diameter_mm"] != old["diameter_mm"] or bar["original_coordinate_mm"] != old["coordinate_mm"]):
                raise ValueError("Cut piece changed source identity, material or original geometry")
            delta = bar["coordinate_mm"]-old["coordinate_mm"]
            nudged = abs(delta) > 1e-6
            if (type(bar["axis_nudged"]) is not bool or bar["axis_nudged"] != nudged
                    or (nudged and (not packet["radius_sized_edge_axis_nudge_enabled"]
                        or abs(abs(delta)-bar["diameter_mm"]/2) > 1e-6))):
                raise ValueError("Only explicit zero or one-radius original-axis nudge is supported")
            piece_axes.add(bar["coordinate_mm"])
            lo, hi = bar["longitudinal_mm"]
            if not old["longitudinal_mm"][0] <= lo < hi <= old["longitudinal_mm"][1]:
                raise ValueError("Cut piece is not a positive subset of its original bar")
            changed = bar["longitudinal_mm"] != old["longitudinal_mm"]
            if type(bar["physically_cut"]) is not bool or bar["physically_cut"] != changed:
                raise ValueError("Physical cut flag disagrees with actual endpoints")
            if not changed and bar["id"] != old["id"]:
                raise ValueError("Unchanged original must retain its physical ID")
            if changed:
                changed_originals.add(key)
            intervals.append((lo, hi))
        if pieces and len(piece_axes) != 1:
            raise ValueError("Pieces of the same original bar cannot acquire different axes")
        intervals.sort()
        for index in range(1, len(intervals)):
            if intervals[index][0] < intervals[index-1][1]:
                raise ValueError("Cut pieces of one original overlap or duplicate steel")
    if mapped_originals != set(before) or mapped_pieces != set(after):
        raise ValueError("Piece mapping omitted an original or an actual output bar")
    positions, bars, cut_count, nudge_count = set(), [], 0, 0
    for key in sorted(after):
        direction, identifier = key
        raw = after[key]
        axis = 0 if direction.endswith("X") else 1
        start, end = list(offset), list(offset)
        start[axis] += raw["longitudinal_mm"][0]
        end[axis] += raw["longitudinal_mm"][1]
        start[1-axis] += raw["coordinate_mm"]
        end[1-axis] += raw["coordinate_mm"]
        bars.append({"direction": direction, "bar_id": identifier, "run_id": identifier, "position_index": 0,
            "start_xy_mm": start, "end_xy_mm": end, "diameter_mm": raw["diameter_mm"], "steel_class": raw["steel_class"],
            "intersection": False, "source_refs": list(raw["source_bar_ids"]), "physically_cut": raw["physically_cut"],
            "original_bar_id": raw["original_bar_id"], "axis_nudged": raw["axis_nudged"]})
        cut_count += int(raw["physically_cut"])
        nudge_count += int(raw["axis_nudged"])
        positions.add((raw["steel_class"].strip(), raw["diameter_mm"], round(raw["longitudinal_mm"][1]-raw["longitudinal_mm"][0], 6)))
    number(packet["after"]["position_count"], len(positions), len(positions), integer=True)
    number(packet["after"]["source_zone_count"], 1, 20000, integer=True)
    pair_count, pair_budget = 0, 0
    for direction in DIRECTIONS:
        ordered = []
        axis = 0 if direction.endswith("X") else 1
        for bar in bars:
            if bar["direction"] == direction:
                ordered.append(bar)
        ordered.sort(key=lambda item: item["start_xy_mm"][axis])
        for index, left in enumerate(ordered):
            for right in ordered[index+1:]:
                if right["start_xy_mm"][axis] >= left["end_xy_mm"][axis]-1e-6:
                    break
                pair_budget += 1
                if pair_budget > 2000000:
                    raise ValueError("Graphic same-direction intersection budget exceeded; no partial success")
                if abs(left["start_xy_mm"][1-axis]-right["start_xy_mm"][1-axis]) < (left["diameter_mm"]+right["diameter_mm"])/2-1e-6:
                    left["intersection"] = right["intersection"] = True
                    pair_count += 1
    summary = copy.deepcopy(packet["after"])
    result = {"units": "mm", "coordinate_system": "revit-internal-origin-and-axes", "placement_eligible": False,
        "engineering_approval": False, "input_schema": GRAPHIC_BAR_SCHEMA, "case_id": packet["case_id"],
        "offset_xy_mm": offset, "bars": bars, "source_frames": [], "summary": summary,
        "source_blockers": ["physical-cut-draft-not-engineering-acceptance"], "intersection_pair_count": pair_count,
        "graphic_input": copy.deepcopy(packet),
        "trim_graphics": {"checks": copy.deepcopy(packet["checks"]), "coverage_policy": packet["coverage_policy"],
            "crop_policy_id": packet["crop_policy_id"], "source_report_sha256": packet["source_report_sha256"],
            "source_host_report_sha256": packet["source_host_report_sha256"], "before_count": len(before),
            "before_mass_kg": _graphic_mass(before), "physically_cut_piece_count": cut_count,
            "before_inventory": copy.deepcopy(packet["before"]["directions"]),
            "removed_wholly_external_bars": copy.deepcopy(packet["removed_wholly_external_bars"]),
            "removed_wholly_external_bar_count": len(packet["removed_wholly_external_bars"]),
            "removed_input_bar_count": len(removed_ids), "removed_input_bars": copy.deepcopy(removed),
            "respect_openings": openings,
            "radius_sized_axis_nudged_piece_count": nudge_count,
            "changed_original_bar_count": len(changed_originals), "piece_mapping": copy.deepcopy(mapping),
            "recorded_binding_matches_entered": offset == packet["source_to_revit_xy_mm"],
            "scope": "All straight cut pieces and geometric presence only; recorded checks are not independently recomputed in Revit"}}
    return result


def _relocation_primitives(packet, offset_x_mm, offset_y_mm):
    """Separate graphic-only adapter: shifted axes NEVER enter the old validator."""
    exact_keys(packet, ("schema_version", "units", "placement_eligible", "case_id", "source_packet_sha256",
        "source_report_sha256", "source_host_report_sha256", "source_to_revit_xy_mm", "binding_source",
        "raw_bars_by_direction", "original_source_zones", "expected", "status",
        "structural_placement_supported", "correction"))
    if (packet["units"] != "mm" or packet["placement_eligible"] is not False
            or packet["structural_placement_supported"] is not False):
        raise ValueError("Relocation input is a graphic research draft, never approved structural transport")
    label(packet["case_id"])
    label(packet["binding_source"])
    if packet["status"] not in ("blocked_working_host", "checked_research_draft"):
        raise ValueError("Unknown relocation draft status")
    for name in ("source_packet_sha256", "source_report_sha256", "source_host_report_sha256"):
        label(packet[name])
        if len(packet[name]) != 64 or any(c not in "0123456789abcdef" for c in packet[name]):
            raise ValueError("Invalid source SHA256")
    recorded_offset = packet["source_to_revit_xy_mm"]
    if not isinstance(recorded_offset, list) or len(recorded_offset) != 2:
        raise ValueError("Expected explicit two-dimensional recorded binding")
    for value in (offset_x_mm, offset_y_mm) + tuple(recorded_offset):
        number(value, -100000000, 100000000)
    exact_keys(packet["raw_bars_by_direction"], DIRECTIONS)
    zones, sources = _source_certificates({"source_zones": packet["original_source_zones"]})
    source_by_id = {(key[0], "{0}/{1}/{2}".format(*key[1:])): (key, value) for key, value in sources.items()}
    used, bars, positions = set(), [], set()
    offset = [offset_x_mm, offset_y_mm]
    for direction in DIRECTIONS:
        raw = packet["raw_bars_by_direction"][direction]
        if not isinstance(raw, list) or len(raw) > MAX_BARS:
            raise ValueError("Expected bounded complete direction bar array")
        axis = 0 if direction.endswith("X") else 1
        for bar in raw:
            exact_keys(bar, ("id", "steel_class", "diameter_mm", "coordinate_mm", "longitudinal_mm", "source_bar_ids"))
            label(bar["id"])
            label(bar["steel_class"])
            number(bar["diameter_mm"], 6, 40, integer=True)
            number(bar["coordinate_mm"], -100000000, 100000000)
            interval = bar["longitudinal_mm"]
            if not isinstance(interval, list) or len(interval) != 2:
                raise ValueError("Invalid straight bar interval")
            for value in interval:
                number(value, -100000000, 100000000)
            if interval[0] >= interval[1]:
                raise ValueError("Nonpositive bar length")
            owners = bar["source_bar_ids"]
            if not isinstance(owners, list) or not 1 <= len(owners) <= 20000:
                raise ValueError("Missing or unbounded original source owners")
            references, original_axes = [], set()
            for identifier in owners:
                label(identifier)
                lookup = (direction, identifier)
                if lookup not in source_by_id or lookup in used:
                    raise ValueError("Unknown or repeated original source owner")
                used.add(lookup)
                key, (part, original_axis) = source_by_id[lookup]
                original_axes.add(original_axis)
                if (bar["steel_class"] != part["steel_class"] or bar["diameter_mm"] < part["diameter_mm"]
                        or interval[0] > part["required_interval_mm"][0]-40*bar["diameter_mm"]+1e-6
                        or interval[1] < part["required_interval_mm"][1]+40*bar["diameter_mm"]-1e-6):
                    raise ValueError("Original material or full new-diameter longitudinal 40d was lost")
                references.append({"zone_id": key[1], "component_index": key[2], "bar_index": key[3]})
            if len(original_axes) != 1:
                raise ValueError("Original merged source owners disagree on their original axis")
            start, end = list(offset), list(offset)
            start[axis] += interval[0]
            end[axis] += interval[1]
            start[1-axis] += bar["coordinate_mm"]
            end[1-axis] += bar["coordinate_mm"]
            bars.append({"direction": direction, "bar_id": bar["id"], "run_id": None, "position_index": None,
                "start_xy_mm": start, "end_xy_mm": end, "diameter_mm": bar["diameter_mm"],
                "steel_class": bar["steel_class"], "source_refs": references, "intersection": False,
                "original_axis_mm": next(iter(original_axes)), "relocated": abs(bar["coordinate_mm"]-next(iter(original_axes))) > 1e-6})
            positions.add((bar["steel_class"], bar["diameter_mm"], round(interval[1]-interval[0], 6)))
            if len(bars) > MAX_BARS:
                raise ValueError("Complete preview bar budget exceeded")
    if used != set(source_by_id):
        raise ValueError("Original source ownership omitted; no partial plan accepted")
    summary = packet["expected"]
    exact_keys(summary, ("physical_bar_count", "additional_mass_kg", "source_zone_count", "position_count"))
    number(summary["source_zone_count"], len(zones), len(zones), integer=True)
    number(summary["position_count"], len(positions), len(positions), integer=True)
    correction = packet["correction"]
    exact_keys(correction, ("policy_id", "moved_bar_count", "moves", "host_blocked_before", "host_blocked_after", "new_same_direction_body_pairs"))
    label(correction["policy_id"])
    moved = {(bar["direction"], bar["bar_id"]): bar for bar in bars if bar["relocated"]}
    number(correction["moved_bar_count"], len(moved), len(moved), integer=True)
    moves = correction["moves"]
    if not isinstance(moves, list) or len(moves) != len(moved):
        raise ValueError("Correction annotations must cover every shifted bar")
    move_ids = set()
    for move in moves:
        exact_keys(move, ("direction", "bar_id", "axis_shift_mm", "longitudinal_shift_mm", "opening_indexes",
            "opening_bboxes_mm", "original_axis_mm", "corrected_axis_mm"))
        key = (move["direction"], move["bar_id"])
        if key not in moved or key in move_ids:
            raise ValueError("Unknown or duplicate shifted-axis annotation")
        move_ids.add(key)
        for name in ("axis_shift_mm", "longitudinal_shift_mm", "original_axis_mm", "corrected_axis_mm"):
            number(move[name], -100000000, 100000000)
        bar = moved[key]
        axis = 0 if bar["direction"].endswith("X") else 1
        q = bar["start_xy_mm"][1-axis]-offset[1-axis]
        if (abs(q-move["corrected_axis_mm"]) > 1e-6
                or abs(bar["original_axis_mm"]-move["original_axis_mm"]) > 1e-6
                or abs(q-bar["original_axis_mm"]-move["axis_shift_mm"]) > 1e-6):
            raise ValueError("Correction annotation disagrees with actual shifted axis")
    for name in ("host_blocked_before", "host_blocked_after"):
        number(correction[name], 0, len(bars), integer=True)
    number(correction["new_same_direction_body_pairs"], 0, 0, integer=True)
    pair_count, budget = 0, 0
    for direction in DIRECTIONS:
        axis = 0 if direction.endswith("X") else 1
        ordered = sorted((b for b in bars if b["direction"] == direction), key=lambda b: b["start_xy_mm"][axis])
        for index, left in enumerate(ordered):
            for right in ordered[index+1:]:
                if right["start_xy_mm"][axis] >= left["end_xy_mm"][axis]-1e-6:
                    break
                budget += 1
                if budget > 2000000:
                    raise ValueError("Graphic collision-marker budget exceeded; nothing truncated")
                if abs(left["start_xy_mm"][1-axis]-right["start_xy_mm"][1-axis]) < (left["diameter_mm"]+right["diameter_mm"])/2-1e-6:
                    left["intersection"] = right["intersection"] = True
                    pair_count += 1
    result = {"units": "mm", "coordinate_system": "revit-internal-origin-and-axes",
        "placement_eligible": False, "engineering_approval": False, "input_schema": packet["schema_version"],
        "case_id": packet["case_id"], "offset_xy_mm": offset, "bars": bars,
        "original_source_zones": copy.deepcopy(packet["original_source_zones"]), "source_frames": [],
        "summary": copy.deepcopy(summary), "source_blockers": [packet["status"], "research-draft-no-structural-transport"],
        "intersection_pair_count": pair_count, "correction": copy.deepcopy(correction),
        "recorded_source_to_revit_xy_mm": list(recorded_offset),
        "recorded_binding_matches_entered": offset == recorded_offset,
        "validation_scope": "Graphic data, ownership, count, mass, longitudinal 40d, corrected-axis annotations; NOT coverage, opening eligibility or engineering acceptance"}
    return _validate_primitives(result)


def _validate_primitives(primitives):
    """Rendering boundary, not an engineering/source certificate validator."""
    if isinstance(primitives, dict) and primitives.get("input_schema") == SOURCE_SCHEMA:
        rebuilt = build_source_primitives(primitives["source_packet"], *primitives["offset_xy_mm"])
        if rebuilt != primitives:
            raise ValueError("Original source preview primitives changed after validation")
        return primitives
    if isinstance(primitives, dict) and primitives.get("input_schema") == GRAPHIC_BAR_SCHEMA:
        rebuilt = _graphic_bar_primitives(primitives["graphic_input"], *primitives["offset_xy_mm"])
        if rebuilt != primitives:
            raise ValueError("Actual cut graphic primitives changed after validation")
    if (not isinstance(primitives, dict) or primitives.get("units") != "mm"
            or primitives.get("placement_eligible") is not False
            or primitives.get("engineering_approval") is not False
            or primitives.get("coordinate_system") != "revit-internal-origin-and-axes"):
        raise ValueError("Expected an explicitly unapproved graphic XY plan in mm")
    bars = primitives.get("bars")
    minimum_bars = 0 if primitives.get("input_schema") == GRAPHIC_BAR_SCHEMA else 1
    if not isinstance(bars, list) or not minimum_bars <= len(bars) <= MAX_BARS:
        raise ValueError("Expected a complete bounded nonempty bar list")
    seen = set()
    for bar in bars:
        if bar.get("direction") not in DIRECTIONS:
            raise ValueError("Unknown direction")
        label(bar["bar_id"])
        identifier = (bar["direction"], bar["bar_id"])
        if identifier in seen:
            raise ValueError("Missing or duplicate physical bar id")
        seen.add(identifier)
        axis = 0 if bar["direction"].endswith("X") else 1
        for name in ("start_xy_mm", "end_xy_mm"):
            values = bar.get(name)
            if not isinstance(values, list) or len(values) != 2:
                raise ValueError("Expected two-dimensional XY endpoints")
            for value in values:
                number(value, -100000000, 100000000)
        start, end = bar["start_xy_mm"], bar["end_xy_mm"]
        if start[1-axis] != end[1-axis] or start[axis] >= end[axis]:
            raise ValueError("Expected a positive straight axis-aligned bar")
        number(bar["diameter_mm"], 6, 40, integer=True)
        if type(bar.get("intersection")) is not bool:
            raise ValueError("Explicit intersection marker required")
    summary = primitives.get("summary", {})
    number(summary.get("physical_bar_count"), len(bars), len(bars), integer=True)
    number(summary.get("additional_mass_kg"), 0, 1e10)
    mass = math.fsum(0.000006165 * bar["diameter_mm"] ** 2 *
        (bar["end_xy_mm"][0 if bar["direction"].endswith("X") else 1] -
         bar["start_xy_mm"][0 if bar["direction"].endswith("X") else 1]) for bar in bars)
    if abs(mass-summary["additional_mass_kg"]) > max(0.001, mass*1e-9):
        raise ValueError("Graphic summary disagrees with the full drawn physical party")
    for key in ("source_zone_count", "position_count"):
        minimum = 0 if key == "position_count" and not bars and minimum_bars == 0 else 1
        number(summary.get(key), minimum, 20000, integer=True)
    number(primitives.get("intersection_pair_count"), 0, 10000, integer=True)
    return primitives


def _trim_caption(primitives):
    trim, after = primitives["trim_graphics"], primitives["summary"]
    checks = trim["checks"]
    text = ("ФИЗИЧЕСКАЯ ОБРЕЗКА ПО ВНЕШНЕМУ КОНТУРУ / НЕ АРМАТУРА / НЕ ВЫДАЧА\n"
        "Исходно: {0} стержней / {1:.3f} кг. После обработки: {2} прямых отрезков / {3:.3f} кг / {4} позиций.\n"
        "Укорочено исходных стержней: {5}; укороченных отрезков: {6}. Все отрезки показаны, скрытого клиппинга нет.\n"
        "40d (статус исходного отчёта): {7}. Геометрическое наличие стержней БЕЗ анкеровки: {8}. Раскрой 11700: {9}.\n"
        "Физическое наличие НЕ означает достаточное анкеровочное покрытие. Revit эти расчёты не повторяет.\n"
        "Янтарный: укороченный отрезок или сдвиг оси D/2; красный: native Solid/cover-диагностика; фиолетовый: XY-пересечение добавок.\n"
        "Ни цвет контура, ни этот вид не разрешают размещение. Показаны только прямые детали; U/дуги не поддержаны.\n"
        "Политика: {10}; привязка XY совпала с записью: {11}. SHA фиксирует происхождение, не подтверждает текущий RVT.").format(
            trim["before_count"], trim["before_mass_kg"], after["physical_bar_count"], after["additional_mass_kg"],
            after["position_count"], trim["changed_original_bar_count"], trim["physically_cut_piece_count"],
            checks["anchorage_40d"], checks["coverage"], checks["stock_cutting"], trim["crop_policy_id"],
            trim["recorded_binding_matches_entered"])
    text += "\nВНЕШНИЙ КОНТУР по backend: {0}; остаточных непрошедших стержней: {1}.".format(
        checks["outer_boundary"]["status"], checks["outer_boundary"]["failure_count"])
    if trim.get("respect_openings"):
        text += "\nОТВЕРСТИЯ УЧТЕНЫ РАЗРЕЗАНИЕМ. Пересечений с отверстиями: {0}; отказов по материалу плиты: {1}. Защитный слой не добавлен к резу.".format(
            checks["openings"]["failure_count"], checks["material_boundary"]["failure_count"])
        text += "\nБЕЗ ПЕРЕСЕЧЕНИЯ С МАТЕРИАЛОМ: {0} исходных стержней не оставили отрезков; все ID сохранены, КЭ не удалены.".format(
            trim["removed_input_bar_count"])
    text += "\nЦЕЛИКОМ СНАРУЖИ: для {0} исходных стержней не оставлено ни одного отрезка. Их ID, исходная геометрия и причины сохранены в JSON; КЭ не удалены.".format(
        trim["removed_wholly_external_bar_count"])
    text += "\n3D по профилю backend: {0}; доказанных пар: {1}; непроверенных пар: {2}. Это НЕ проверка существующей арматуры RVT.".format(
        checks["collisions_3d"]["status"], checks["collisions_3d"]["proven_pair_count"], checks["collisions_3d"]["uncertain_pair_count"])
    if not trim["recorded_binding_matches_entered"]:
        text += "\nВНИМАНИЕ: введённый XY отличается от привязки расчёта; его контурные/3D-статусы на этот вид не переносятся."
    return text+"\nОтрезков со сдвигом оси ровно D/2: {0}. Это отдельно от укорачивания; None = не проверено, не ноль.".format(
        trim["radius_sized_axis_nudged_piece_count"])


def _progress(callback, stage, index, total):
    if callback is not None and callback(stage, index, total) is False:
        raise PreviewCancelled("Preview cancelled; no partial view is retained")


def read_preview_host(probe, floor):
    """Read real Solid and ALL projected edges, including internal height steps.

    The outlines do not pretend to be one classified slice; short projected
    vertical edges vanish in XY, curves retain explicitly marked tessellation.
    No temporary geometry is attached to the document.
    """
    DB = probe.DB
    options = DB.Options()
    options.DetailLevel = DB.ViewDetailLevel.Fine
    geometry = floor.get_Geometry(options)
    solids = []
    for item in geometry:
        if isinstance(item, DB.GeometryInstance):
            raise ValueError("Nested floor geometry is unsupported; no bbox replacement is made")
        if isinstance(item, DB.Solid) and item.Volume > 0:
            solids.append(item)
    if len(solids) != 1:
        raise ValueError("Expected exactly one positive-volume selected-floor Solid")
    solid, outlines, seen, curve_count = solids[0], [], set(), 0
    for face in solid.Faces:
        for loop in face.EdgeLoops:
            for edge in loop:
                curve = edge.AsCurve()
                is_line = isinstance(curve, DB.Line)
                if is_line:
                    points = [probe.point(curve.GetEndPoint(i)) for i in (0, 1)]
                else:
                    curve_count += 1
                    points = [probe.point(point) for point in curve.Tessellate()]
                    if len(points) < 2:
                        raise ValueError("Empty host curve tessellation; contours are incomplete")
                for start, end in zip(points, points[1:]):
                    a, b = start[:2], end[:2]
                    if sum((a[i]-b[i])**2 for i in range(2)) <= 1e-12:
                        continue  # Exactly vertical edges have zero XY projection.
                    key = tuple(sorted(((round(a[0], 6), round(a[1], 6)),
                                        (round(b[0], 6), round(b[1], 6)))))
                    if key in seen:
                        continue
                    seen.add(key)
                    outlines.append({"start_xy_mm": a, "end_xy_mm": b})
                    if len(outlines) > MAX_OUTLINE_SEGMENTS:
                        raise ValueError("Complete host outline limit exceeded; nothing truncated")
    if not outlines:
        raise ValueError("No projected floor contours were read")
    return {"solid": solid, "geometry_owner": geometry, "options": options,
        "outlines": outlines, "curved_edge_occurrences": curve_count,
        "outline_scope": "XY projection of ALL real Solid edges, not a single classified slice; curved edges tessellated"}


def check_preview_host(probe, host_data, host_geometry, bars, loop_list_factory, progress=None):
    """Necessary/conservative whole-height XY test, NEVER structural Z approval.

    Rectangle ends have SIDE cover, transverse edges have SIDE cover + radius.
    Every actual Solid height is checked; a ledge can conservatively reject a
    bar that would fit a particular, not-yet-specified Z. Boolean API failures
    mark an axis unknown rather than dropping it or approving it.
    """
    DB = probe.DB
    side = host_data["covers"]["other"]["distance_mm"]
    number(side, 0, 1000)
    bbox = host_data["bbox_mm"]
    lower_z, upper_z = bbox["min_mm"][2], bbox["max_mm"][2]
    if not upper_z > lower_z:
        raise ValueError("Invalid actual floor height")

    def point(values):
        return DB.XYZ(*[DB.UnitUtils.ConvertToInternalUnits(v, DB.UnitTypeId.Millimeters) for v in values])

    result = []
    for index, bar in enumerate(bars):
        _progress(progress, "Проверка реального Solid", index, len(bars))
        axis = 0 if bar["direction"].endswith("X") else 1
        lo, hi = list(bar["start_xy_mm"]), list(bar["end_xy_mm"])
        for i in range(2):
            extra = side + (bar["diameter_mm"]/2 if i != axis else 0)
            lo[i] -= extra
            hi[i] += extra
        row = {"direction": bar["direction"], "bar_id": bar["bar_id"],
            "status": "unknown", "outside_volume_mm3": None}
        if any(lo[i] < bbox["min_mm"][i]-0.01 or hi[i] > bbox["max_mm"][i]+0.01 for i in range(2)):
            row.update(status="outside", reason="outside_host_bbox_with_side_cover")
        else:
            loop, envelope, difference = None, None, None
            try:
                loop = DB.CurveLoop()
                points = [(lo[0], lo[1], lower_z), (hi[0], lo[1], lower_z),
                          (hi[0], hi[1], lower_z), (lo[0], hi[1], lower_z)]
                for a, b in zip(points, points[1:] + points[:1]):
                    loop.Append(DB.Line.CreateBound(point(a), point(b)))
                loops = loop_list_factory()
                loops.Add(loop)
                envelope = DB.GeometryCreationUtilities.CreateExtrusionGeometry(loops, DB.XYZ.BasisZ,
                    DB.UnitUtils.ConvertToInternalUnits(upper_z-lower_z, DB.UnitTypeId.Millimeters))
                difference = DB.BooleanOperationsUtils.ExecuteBooleanOperation(
                    envelope, host_geometry["solid"], DB.BooleanOperationsType.Difference)
                volume = float(difference.Volume) * probe.mm(1.0)**3
                if math.isnan(volume) or math.isinf(volume) or volume < -0.1:
                    raise ValueError("Invalid Boolean volume")
                row.update(status="outside" if volume > 0.1 else "contained", outside_volume_mm3=volume,
                    reason="whole_height_side_cover_envelope_minus_actual_solid")
            except Exception as exc:
                row.update(status="unknown", reason="native_boolean_failed", error=text_type(exc))
            finally:
                for temporary in (difference, envelope, loop):
                    if temporary is not None:
                        temporary.Dispose()
        result.append(row)
    _progress(progress, "Проверка реального Solid", len(bars), len(bars))
    return {"scope": "Conservative XY whole-height envelope with side cover; NOT 3D placement or engineering approval",
        "bars": result, "checked_physical_bar_count": len(result),
        "outside_count": sum(row["status"] == "outside" for row in result),
        "unknown_count": sum(row["status"] == "unknown" for row in result),
        "contained_count": sum(row["status"] == "contained" for row in result)}


def panel_layout(primitives, host_data):
    """Four separate direction panels preserve original absolute XY in the report."""
    points = [host_data["bbox_mm"][key][:2] for key in ("min_mm", "max_mm")]
    points += [bar[key] for bar in primitives["bars"] for key in ("start_xy_mm", "end_xy_mm")]
    lo, hi = [], []
    for axis in range(2):
        coordinates = [point[axis] for point in points]
        lo.append(min(coordinates))
        hi.append(max(coordinates))
    width, height, gap = max(hi[0]-lo[0], 5000), max(hi[1]-lo[1], 5000), 8000
    panels = {}
    for index, direction in enumerate(DIRECTIONS):
        panels[direction] = {"translation_xy_mm": [index % 2 * (width+gap)-lo[0],
            -(index//2)*(height+gap)-lo[1]], "bounds_xy_mm": [lo, hi]}
    return {"panels": panels, "width_mm": width, "height_mm": height, "gap_mm": gap}


def _types(document, DB):
    families = [item for item in DB.FilteredElementCollector(document).OfClass(DB.ViewFamilyType)
                if item.ViewFamily == DB.ViewFamily.Drafting]
    texts = list(DB.FilteredElementCollector(document).OfClass(DB.TextNoteType))
    if not families or not texts:
        raise ValueError("An existing Drafting ViewFamilyType and TextNoteType are required; no types are created or edited")
    # Existing small text, without changing a shared type. Deterministic fallback.
    def rank(item):
        try:
            size = item.get_Parameter(DB.BuiltInParameter.TEXT_SIZE).AsDouble()*304.8
            if not 0 < size < 100:
                raise ValueError("Invalid text size")
            return (abs(size-2.5), element_id(item.Id))
        except Exception:
            return (1000, element_id(item.Id))
    return sorted(families, key=lambda item: element_id(item.Id))[0], sorted(texts, key=rank)[0]


def _draw(document, DB, view, primitives, host_data, host_geometry, check, text_type_id, report, progress):
    layout = panel_layout(primitives, host_data)
    report["panel_layout"] = layout
    styles = {}
    for key, rgb in COLORS.items():
        styles[key] = DB.OverrideGraphicSettings().SetProjectionLineColor(DB.Color(*rgb)).SetProjectionLineWeight(
            1 if key in ("host", "source") else 3)
    toler = float(document.Application.ShortCurveTolerance)*304.8
    created, short_edges = [], 0

    def point(xy):
        return DB.XYZ(xy[0]/304.8, xy[1]/304.8, 0)

    def line(start, end, translation, style, is_bar=False):
        a = [start[0]+translation[0], start[1]+translation[1]]
        b = [end[0]+translation[0], end[1]+translation[1]]
        if math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2) <= toler:
            if is_bar:
                raise ValueError("A physical bar is shorter than Revit ShortCurveTolerance; no partial drawing")
            return None
        curve = DB.Line.CreateBound(point(a), point(b))
        element = document.Create.NewDetailCurve(view, curve)
        if element is None or element.OwnerViewId != view.Id:
            raise ValueError("New detail curve not owned by the NEW preview view")
        view.SetElementOverrides(element.Id, styles[style])
        return element_id(element.Id)

    def note(xy, text):
        element = DB.TextNote.Create(document, view.Id, point(xy), text, text_type_id)
        if element is None or element.OwnerViewId != view.Id:
            raise ValueError("New note not owned by the NEW preview view")
        return element_id(element.Id)

    status_by_id = {}
    for row in check["bars"]:
        status_by_id[(row["direction"], row["bar_id"])] = row["status"]
    labels = {"bottom-X": "НИЗ X", "bottom-Y": "НИЗ Y", "top-X": "ВЕРХ X", "top-Y": "ВЕРХ Y"}
    try:
        for direction in DIRECTIONS:
            panel = layout["panels"][direction]
            shift = panel["translation_xy_mm"]
            for outline in host_geometry["outlines"]:
                result = line(outline["start_xy_mm"], outline["end_xy_mm"], shift, "host")
                if result is None:
                    short_edges += 1
                else:
                    created.append(result)
            # IronPython 2.7.12 failed here with a MutableTuple cast for a
            # generator inside a dict comprehension. Keep this closure-heavy
            # drawing path and its committed readback free of comprehensions.
            bars = []
            counts = {"outside": 0, "unknown": 0}
            for candidate in primitives["bars"]:
                if candidate["direction"] == direction:
                    bars.append(candidate)
                    status = status_by_id[(direction, candidate["bar_id"])]
                    if status in counts:
                        counts[status] += 1
            for bar in bars:
                _progress(progress, "Рисование полной раскладки", len(report["bar_element_mapping"]), len(primitives["bars"]))
                style = status_by_id[(direction, bar["bar_id"])]
                if style == "contained" and bar["intersection"]:
                    style = "intersection"
                elif style == "contained" and (bar.get("physically_cut", False) or bar.get("axis_nudged", False)):
                    style = "physically_cut"
                elif style == "contained" and bar.get("relocated", False):
                    style = "relocated"
                identifier = line(bar["start_xy_mm"], bar["end_xy_mm"], shift, style, is_bar=True)
                created.append(identifier)
                report["bar_element_mapping"].append({"direction": direction, "bar_id": bar["bar_id"],
                    "element_id": identifier, "style": style, "relocated": bar.get("relocated", False),
                    "physically_cut": bar.get("physically_cut", False),
                    "axis_nudged": bar.get("axis_nudged", False),
                    "host_status": status_by_id[(direction, bar["bar_id"])]})
            left, top = panel["bounds_xy_mm"][0][0]+shift[0], panel["bounds_xy_mm"][1][1]+shift[1]
            created.append(note([left, top+1500], "{0}: {1} стержней; вне контура {2}; не проверено {3}".format(
                labels[direction], len(bars), counts["outside"], counts["unknown"])))
        summary = primitives["summary"]
        header = ("{0}\n{1}\nПлита id={2}. Полная партия: {3} стержней; {4:.2f} кг; {5} исходных зон; {6} типоразмеров.\n"
            "Синий: контур пройден ТОЛЬКО в XY. Красный: вне Solid/защитного слоя. Оранжевый: проверка не выполнена.\n"
            "Фиолетовый: известное пересечение добавок. Всего нерешённых пар: {7}; конфликтов host: {8}; не проверено: {9}.\n"
            "Серый: проекция ВСЕХ рёбер реальной плиты, включая проёмы и высотные уступы; не одно сечение.\n"
            "Все проблемные стержни сохранены. Высоты, фон и инженерный допуск НЕ проверены. Масса — расчёт, не установленная арматура.\n"
            "XY-сдвиг DXF: {10}; {11} мм — введён вручную, автоматически не подтверждён. Панели разнесены ТОЛЬКО графически.\n"
            "Созданы только линии и текст НОВОГО чертёжного вида; нет Rebar, размещения в плите или изменений существующих видов.").format(
                DISCLAIMER, primitives["case_id"], host_data["element_id"], summary["physical_bar_count"],
                summary["additional_mass_kg"], summary["source_zone_count"], summary["position_count"],
                primitives["intersection_pair_count"], check["outside_count"], check["unknown_count"],
                primitives["offset_xy_mm"][0], primitives["offset_xy_mm"][1])
        if short_edges:
            header += "\nМелких отрезков контура ниже графического допуска Revit не показано: {0}; стержни не пропущены.".format(short_edges)
        if "correction" in primitives:
            header += "\nБирюзовый: сдвинутая ось. Черновик обхода отверстий: сдвинуто {0} стержней; формы и длины не менялись. Не инженерное разрешение.".format(
                primitives["correction"]["moved_bar_count"])
            if not primitives["recorded_binding_matches_entered"]:
                header += "\nВНИМАНИЕ: введённый XY отличается от привязки расчёта коррекции; исходные host-проверки не переносятся на этот вид."
        created.append(note([0, layout["height_mm"]+10500], header))
        if "trim_graphics" in primitives:
            caption = _trim_caption(primitives).replace("\n", "\r")
            identifier = note([0, layout["height_mm"]+14500], caption)
            created.append(identifier)
            report["trim_caption_id"] = identifier
        report["created_annotation_ids"] = created
        report["short_outline_segments_not_drawn"] = short_edges
        if len(report["bar_element_mapping"]) != len(primitives["bars"]):
            raise ValueError("Not every physical bar was drawn exactly once")
        _progress(progress, "Рисование полной раскладки", len(primitives["bars"]), max(1, len(primitives["bars"])))
    finally:
        for style in styles.values():
            style.Dispose()


def _matching_endpoints(actual, expected):
    """Accept either orientation, with the same 0.01 mm XYZ tolerance.

    Explicit loops avoid nested generator closure scopes in IronPython 2.7.
    The positive <= comparison deliberately rejects NaN as well as infinity.
    """
    for order in ((0, 1), (1, 0)):
        matched = True
        for endpoint in (0, 1):
            for axis in (0, 1, 2):
                if not abs(actual[endpoint][axis]-expected[order[endpoint]][axis]) <= 0.01:
                    matched = False
                    break
            if not matched:
                break
        if matched:
            return True
    return False


def _readback_preview(document, DB, primitives, report):
    """Read the COMMITTED new annotation geometry; no structural Rebar claim."""
    view = document.GetElement(DB.ElementId(report["attempted_view_id"]))
    if not isinstance(view, DB.ViewDrafting):
        raise ValueError("Committed preview view is missing or not a DraftingView")
    bars = {}
    for bar in primitives["bars"]:
        bars[(bar["direction"], bar["bar_id"])] = bar
    checked = set()
    for row in report["bar_element_mapping"]:
        key = (row["direction"], row["bar_id"])
        if key not in bars or key in checked:
            raise ValueError("Committed drawing has duplicate or unknown physical axes")
        checked.add(key)
        if "trim_graphics" in primitives and (row.get("physically_cut") != bars[key]["physically_cut"]
                or row.get("axis_nudged") != bars[key]["axis_nudged"]):
            raise ValueError("Committed graphic cut/nudge annotation differs from the actual source piece")
        element = document.GetElement(DB.ElementId(row["element_id"]))
        if not isinstance(element, DB.DetailCurve) or element.OwnerViewId != view.Id:
            raise ValueError("Committed graphic axis missing or belongs to a different view")
        curve = element.GeometryCurve
        if not isinstance(curve, DB.Line):
            raise ValueError("Committed graphic axis is not a straight line")
        shift = report["panel_layout"]["panels"][row["direction"]]["translation_xy_mm"]
        expected = []
        for name in ("start_xy_mm", "end_xy_mm"):
            xy = bars[key][name]
            expected.append([xy[0]+shift[0], xy[1]+shift[1], 0])
        actual = []
        for index in (0, 1):
            point = curve.GetEndPoint(index)
            actual.append([float(point.X)*304.8, float(point.Y)*304.8, float(point.Z)*304.8])
        if not _matching_endpoints(actual, expected):
            raise ValueError("Committed graphic endpoints differ: "+text_type(key))
    if checked != set(bars):
        raise ValueError("Committed drawing omitted physical bars")
    if "trim_graphics" in primitives:
        caption = document.GetElement(DB.ElementId(report.get("trim_caption_id", -1)))
        if (caption is None or caption.OwnerViewId != view.Id
                or _normal_text(caption.Text) != _normal_text(_trim_caption(primitives))):
            raise ValueError("Committed physical-cut/40d/coverage/stock disclaimer is missing or differs")
    return {"status": "matches", "checked_bar_line_count": len(checked), "endpoint_tolerance_mm": 0.01,
        "scope": "Committed DetailCurve XY with panel translation only, not physical Rebar or host placement"}


def create_plan_preview(document, DB, floor, primitives, loop_list_factory, confirmed=False, progress=None):
    """Commit isolated annotation views after consent; outer group protects readback.

    No CheckoutElements, type edits, active-workset changes, Save/Sync or Rebar.
    A live local model may later synchronize the NEW view by normal user action.
    """
    report = {"schema_version": REPORT_SCHEMA, "version": VERSION, "units": "mm",
        "created_utc": datetime.datetime.utcnow().isoformat()+"Z", "status": "blocked_preflight",
        "mode": "new-drafting-view-only", "placement_eligible": False, "engineering_approval": False,
        "disclaimer": DISCLAIMER, "structural_elements_created": 0, "checkout_requested": False,
        "save_requested": False, "synchronization_requested": False, "issues": [], "commit_failures": [],
        "bar_element_mapping": [], "created_annotation_ids": [], "created_view_id": None,
        "transaction": {"status": "not_started"},
        "not_checked": ["engineering-coverage-acceptance", "actual-bar-z-and-layer-order",
            "existing-background-and-model-collisions", "structural-placement", "live-DXF-binding"]}
    probe, transaction, group, committed, host_geometry = Probe(document, DB), None, None, False, None
    report["read_issues"] = probe.issues
    try:
        _validate_primitives(primitives)
        report["expected"] = copy.deepcopy(primitives["summary"])
        report["input_schema"] = primitives["input_schema"]
        if "trim_graphics" in primitives:
            report["trim_graphics"] = copy.deepcopy(primitives["trim_graphics"])
        report["source_blockers"] = copy.deepcopy(primitives["source_blockers"])
        report["offset_xy_mm"] = list(primitives["offset_xy_mm"])
        source_mode = primitives["input_schema"] == SOURCE_SCHEMA
        report["mode"] = "four-original-source-drafting-views-only" if source_mode else report["mode"]
        if confirmed is not True:
            raise ValueError("Explicit consent to create a NEW graphic-only drafting view is required")
        if (document.IsFamilyDocument or document.IsReadOnly or document.IsModifiable
                or text_type(document.Application.VersionNumber) != "2024"):
            raise ValueError("Open a writable Revit 2024 project without an active transaction")
        if not isinstance(floor, DB.Floor) or floor.Document != document:
            raise ValueError("Select one native Floor in the active document")
        report["worksharing"] = classify_document(document, DB)
        if report["worksharing"]["status"] == "blocked":
            raise ValueError("Unsupported worksharing mode: "+"; ".join(report["worksharing"]["issues"]))
        report["worksharing"]["preview_scope"] = "Only NEW views and their own annotations; no Floor or workset checkout is requested"
        active_workset = (int(document.GetWorksetTable().GetActiveWorksetId().IntegerValue)
                          if document.IsWorkshared else None)
        def floor_snapshot():
            if source_mode:
                return {"id": element_id(floor.Id), "unique_id": text_type(floor.UniqueId),
                    "scope": "Selected native Floor identity only; geometry, holes and cover NOT evaluated"}
            return probe.floor(floor)
        host_data = floor_snapshot()
        report["host"] = host_data
        report["host_id"] = element_id(floor.Id)
        if probe.issues:
            raise ValueError("Selected floor read incomplete; no view created")
        if source_mode:
            report["geometry_check"] = {"status": "not_checked", "scope": "Original source graphics only; not a host, cover or TZ acceptance check"}
            region_type, pattern = source_graphic_types(document, DB)
        else:
            host_geometry = read_preview_host(probe, floor)
            report["outline_scope"] = host_geometry["outline_scope"]
            report["curved_edge_occurrences"] = host_geometry["curved_edge_occurrences"]
            report["geometry_check"] = check_preview_host(probe, host_data, host_geometry,
                primitives["bars"], loop_list_factory, progress)
            report["geometry_check"]["acceptance_scope"] = "Native solid/cover diagnostic, not the user-approved TZ outer-boundary gate"
        family, text = _types(document, DB)
        # Snapshot is read-only. No host/workset permission is needed to annotate a NEW view.
        if floor_snapshot() != host_data or probe.issues:
            raise ValueError("Floor changed during preflight; rerun the complete preview")
        group = DB.TransactionGroup(document, "QMonitoring GRAPHIC PREVIEW - new view only")
        group.IsFailureHandlingForcedModal = True
        if group.Start() != DB.TransactionStatus.Started:
            raise ValueError("Preview rollback group did not start")
        transaction = DB.Transaction(document, "QMonitoring GRAPHIC PREVIEW - not reinforcement")
        if transaction.Start() != DB.TransactionStatus.Started:
            raise ValueError("Preview transaction did not start")
        report["transaction"] = {"status": "started"}
        recorder = failure_recorder(DB, report["commit_failures"])
        options = transaction.GetFailureHandlingOptions().SetClearAfterRollback(True)
        options = options.SetForcedModalHandling(True).SetFailuresPreprocessor(recorder)
        transaction.SetFailureHandlingOptions(options)
        if source_mode:
            def source_progress(stage, value, total):
                _progress(progress, stage, value, total)
            draw_source_views(document, DB, primitives, family.Id, text.Id, region_type, pattern,
                loop_list_factory, report, source_progress)
            report["attempted_view_id"] = report["attempted_source_views"][0]["view_id"]
            report["view_name"] = report["attempted_source_views"][0]["name"]
        else:
            view = DB.ViewDrafting.Create(document, family.Id)
            view.ViewTemplateId = DB.ElementId.InvalidElementId
            view.Name = "QMonitoring GRAPHIC PREVIEW - НЕ АРМАТУРА - {0} - {1}".format(
                datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f"), element_id(view.Id))
            view.Scale = 100
            report["attempted_view_id"] = element_id(view.Id)
            report["view_name"] = text_type(view.Name)
            _draw(document, DB, view, primitives, host_data, host_geometry,
                report["geometry_check"], text.Id, report, progress)
        document.Regenerate()
        if floor_snapshot() != host_data or probe.issues:
            raise ValueError("Floor snapshot changed during graphic preview; rolling back")
        after_workset = (int(document.GetWorksetTable().GetActiveWorksetId().IntegerValue)
                         if document.IsWorkshared else None)
        if active_workset != after_workset:
            raise ValueError("Active workset changed; rolling back the new view")
        report["active_workset_unchanged"] = True
        report["selected_floor_snapshot_unchanged"] = True
        returned = transaction.Commit()
        actual = transaction.GetStatus()
        report["transaction"] = {"status": "committed" if returned == actual == DB.TransactionStatus.Committed
            else "unconfirmed", "returned": text_type(returned), "final": text_type(actual)}
        if returned != DB.TransactionStatus.Committed or actual != returned:
            raise ValueError("Graphic preview commit not confirmed")
        report["readback"] = (readback_source_views(document, DB, primitives, report, _matching_endpoints)
            if source_mode else _readback_preview(document, DB, primitives, report))
        # There are no UI callbacks after Commit and before validation. A failed
        # readback rolls back the whole group, including already committed lines.
        returned = group.Assimilate()
        if returned != DB.TransactionStatus.Committed or group.GetStatus() != returned:
            raise ValueError("Preview transaction-group completion not confirmed")
        committed = True
        report["created_view_id"] = report["attempted_view_id"]
        if source_mode:
            report["created_source_views"] = copy.deepcopy(report["attempted_source_views"])
        report["status"] = "graphic_preview_created"
    except Exception as exc:
        report["issues"].append({"stage": "preview", "message": text_type(exc), "traceback": traceback.format_exc()})
        if isinstance(exc, PreviewCancelled):
            report["status"] = "cancelled"
        if group is not None and not committed:
            inner_ended = True
            if transaction is not None:
                inner_ended, inner = rollback_scope(transaction, DB, allow_committed=True)
                transaction = None
                report["inner_transaction_cleanup"] = inner
            ended, rollback = rollback_scope(group, DB) if inner_ended else (False, {"status": "unconfirmed_inner_pending"})
            group = None
            report["transaction"] = rollback
            report["status"] = ("cancelled_rolled_back" if isinstance(exc, PreviewCancelled) else "failed_rolled_back") if ended else "rollback_unconfirmed"
            if ended:
                report["rolled_back_annotation_ids"] = report["created_annotation_ids"]
                report["created_annotation_ids"] = []
                report["bar_element_mapping"] = []
                for mapping in ("source_cell_mapping", "source_curve_mapping", "source_note_mapping"):
                    if mapping in report:
                        report["rolled_back_"+mapping] = report[mapping]
                        report[mapping] = []
    finally:
        cleanup = [value for value in (transaction, group) if value is not None]
        if host_geometry is not None:
            cleanup += [host_geometry["geometry_owner"], host_geometry["options"]]
        for value in cleanup:
            try:
                value.Dispose()
            except Exception as exc:
                report["issues"].append({"stage": "dispose", "message": text_type(exc)})
    return report
