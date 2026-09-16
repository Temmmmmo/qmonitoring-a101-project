# -*- coding: utf-8 -*-
"""Bound repair transport consistency, NOT a FE proof recomputed in Revit."""
from __future__ import division, unicode_literals
import copy
import hashlib
import json

from qm_revit_probe import text_type
from qm_revit_source_preview import _finite_tree
from qm_revit_pruned_preview import _metrics, _collision_record
from qm_trial_input import _unique_object, _reject_constant, exact_keys, number

SCHEMA = "graphic-bar-plan-repaired/v1"
POLICY = "flat-post-trim-deficit-lane-additions/separate-control40d/v1"
FIELDS = ("id", "steel_class", "diameter_mm", "longitudinal_mm", "coordinate_mm", "source_bar_ids")


def _bound(value, encoded, digest):
    if not isinstance(encoded, text_type) or not 1 <= len(encoded) <= 16*1024*1024:
        raise ValueError("Bounded exact repair source/checker JSON required")
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != digest:
        raise ValueError("Repair source/checker SHA256 differs")
    parsed = json.loads(encoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if parsed != value:
        raise ValueError("Repair source/checker differs from exact hashed bytes")
    return parsed


def _ids(values, allowed=None):
    if not isinstance(values, list) or len(values) != len(set(values)):
        raise ValueError("Unique original FE IDs required")
    for value in values:
        number(value, 0, 1000000000, integer=True)
    if allowed is not None and not set(values) <= set(allowed):
        raise ValueError("Repair references unknown original FE")


def _coverage(record, manifest):
    if (record.get("source_demand_removed") is not False
            or record.get("original_FE_geometry_changed") is not False
            or record.get("weak_offer_As_summation") is not False
            or record.get("demand_transfer_used") is not False
            or record.get("positive_area_loss_tolerance_mm2") != 0):
        raise ValueError("Original FE coverage scope changed")
    seen, total = set(), 0
    if len(record["directions"]) != 4:
        raise ValueError("Four complete fresh FE coverage directions required")
    for row in record["directions"]:
        direction = row["direction"]
        if isinstance(direction, dict):
            direction = direction["layer"]+"-"+direction["axis"]
        if direction not in manifest or direction in seen:
            raise ValueError("Unknown/duplicate FE coverage direction")
        seen.add(direction)
        cells = row["cells"]
        ids = [cell["cell_id"] for cell in cells]
        _ids(ids)
        if set(ids) != set(manifest[direction]) or row["demanded_cell_count"] != len(ids):
            raise ValueError("Fresh checker omitted original demanded FE")
        failed, area = 0, 0.0
        for cell in cells:
            number(cell["uncovered_area_mm2"], 0, 1e12)
            if type(cell["covered"]) is not bool or (cell["uncovered_area_mm2"] == 0) != cell["covered"]:
                raise ValueError("FE covered flag contradicts uncovered area")
            failed += int(not cell["covered"])
            area += cell["uncovered_area_mm2"]
        if row["uncovered_cell_count"] != failed or abs(row["uncovered_area_mm2"]-area) > 1e-8:
            raise ValueError("FE coverage summary contradicts complete cell inventory")
        total += failed
    if record["uncovered_cell_count"] != total or record["status"] != ("fail" if total else "pass"):
        raise ValueError("Fresh FE coverage status contradicts original cell inventory")


def build_repaired_primitives(packet, offset_x_mm, offset_y_mm):
    from qm_revit_plan_preview import _graphic_bar_primitives, _graphic_inventory, _graphic_mass, DIRECTIONS
    exact_keys(packet, ("schema_version", "units", "case_id", "geometry_kind", "source_stage",
        "source_trim_packet", "source_trim_packet_json", "source_trim_packet_sha256", "directions",
        "operations", "additions", "repair", "expected", "checks", "placement_eligible", "engineering_approval"))
    for field in ("directions", "operations", "additions", "expected", "checks"):
        _finite_tree(packet[field])
    if (packet["schema_version"] != SCHEMA or packet["units"] != "mm"
            or packet["geometry_kind"] != "straight-bars-only" or packet["source_stage"] != "flat-trim-deficit-repaired"
            or packet["placement_eligible"] is not False or packet["engineering_approval"] is not False):
        raise ValueError("Exact unapproved repaired graphic transport required")
    original = _bound(packet["source_trim_packet"], packet["source_trim_packet_json"], packet["source_trim_packet_sha256"])
    if original["case_id"] != packet["case_id"]:
        raise ValueError("Repair case differs from immutable source")
    result = _graphic_bar_primitives(original, offset_x_mm, offset_y_mm)
    baseline = _graphic_inventory(original["directions"], after=True)
    immutable = _graphic_inventory(original["before"]["directions"])
    owners = set((key[0], owner) for key, raw in immutable.items() for owner in raw["source_bar_ids"])
    repair = packet["repair"]
    checker = _bound(repair["checks"], repair["checker_json"], repair["checker_sha256"])
    _finite_tree(checker)
    _finite_tree(repair["source_lanes"])
    for value in (repair, checker):
        if value["policy_id"] != POLICY or value["accepted_nonregression"] is not True:
            raise ValueError("Known independently checked repair policy required")
        for flag in ("source_demand_removed", "original_FE_geometry_changed", "weak_As_summation",
                     "placement_eligible", "engineering_approval"):
            if value[flag] is not False:
                raise ValueError("Repair cannot grant permission or change original demand")
    if (repair["schema_version"] != "flat-trim-repair-certificate/v1"
            or checker["schema_version"] != "flat-trim-repair-check/v1" or checker["actual_Revit_checked"] is not False
            or checker["previously_covered_area_lost_mm2"] != {"geometric_presence": 0.0, "control_40d": 0.0}
            or checker["material_boundary_failures_after"] != 0 or checker["host_failures"] != []):
        raise ValueError("Explicit original geometry nonregression and unmeasured Revit required")
    number(checker["maximum_axis_nudge_mm"], 0, .01)
    number(checker["minimum_new_length_mm"], 100, 100)
    number(checker["maximum_new_length_mm"], 11700, 11700)
    number(checker["new_collision_pair_count"], 0, 0, integer=True)
    number(checker["grown_collision_pair_count"], 0, 0, integer=True)
    if checker["diameter_increase_permitted"] is not False:
        raise ValueError("This repair transport only supports unchanged source diameters")
    manifest = repair["source_fe_ids_by_direction"]
    exact_keys(manifest, DIRECTIONS)
    for ids in manifest.values():
        _ids(ids)
    lanes = {}
    for lane in repair["source_lanes"]:
        source = lane["source"]
        direction = source["direction"]["layer"]+"-"+source["direction"]["axis"]
        key = direction, source["id"]
        if direction not in DIRECTIONS or key in lanes or key not in owners:
            raise ValueError("Unknown/duplicate repair source lane")
        _ids(lane["source_fe_ids"], manifest[direction])
        for interval in (lane["axis_window_mm"], source["installed_interval_mm"]):
            if len(interval) != 2 or interval[0] >= interval[1]:
                raise ValueError("Finite positive original lane windows required")
        lanes[key] = lane
    rows, provenance = [], {}
    for row in packet["directions"]:
        raw_rows = []
        direction = row["direction"]["layer"]+"-"+row["direction"]["axis"]
        for raw in row["bars"]:
            exact_keys(raw, FIELDS+("provenance",))
            provenance[(direction, raw["id"])] = raw["provenance"]
            raw_rows.append(dict((field, raw[field]) for field in FIELDS))
        rows.append({"direction": row["direction"], "bars": raw_rows})
    final = _graphic_inventory(rows)
    if not set(baseline) <= set(final):
        raise ValueError("Repair cannot delete any immutable trimmed bar")
    kinds = {}
    for key, raw in final.items():
        p = provenance[key]
        exact_keys(p, ("kind", "policy_id", "source_fe_ids", "lane_ids", "reason"))
        kind = p["kind"]
        if kind not in ("retained", "modified", "added") or p["policy_id"] != POLICY or p["lane_ids"] != raw["source_bar_ids"]:
            raise ValueError("Explicit exact repair ownership/provenance required")
        reasons = {"retained": "unchanged-trimmed-bar", "modified": "exact-deficit-FE-edge-minus-original-service-half-width",
            "added": "finite-source-lane-positive-deficit-gain"}
        if p["reason"] != reasons[kind]:
            raise ValueError("Unknown repair structural operation reason")
        allowed = set()
        for owner in p["lane_ids"]:
            lane = lanes.get((key[0], owner))
            if lane is None:
                raise ValueError("Repair bar references unknown structural source lane")
            source = lane["source"]
            allowed.update(lane["source_fe_ids"])
            if kind != "retained" and (raw["steel_class"] != source["steel_class"]
                    or raw["diameter_mm"] < source["diameter_mm"]
                    or (kind == "added" and raw["diameter_mm"] != source["diameter_mm"])):
                raise ValueError("Repair material differs from original lane")
            if kind != "retained":
                q = raw["coordinate_mm"]
                if not lane["axis_window_mm"][0] <= q <= lane["axis_window_mm"][1]:
                    raise ValueError("Repair axis outside finite original lane window")
                step = source["background_step_mm"]
                number(step, .001, 1000000)
                nearest = source["background_origin_mm"]+round((q-source["background_origin_mm"])/step)*step
                if abs(q-nearest)+1e-6 < (raw["diameter_mm"]+source["background_diameter_mm"])/2:
                    raise ValueError("Repair axis violates original background clearance")
        _ids(p["source_fe_ids"], allowed)
        if kind != "retained" and not p["source_fe_ids"]:
            raise ValueError("New repair requires positive-area original FE provenance")
        if key in baseline:
            if kind == "added" or any(raw[field] != baseline[key][field] for field in FIELDS if field != "coordinate_mm"):
                raise ValueError("Existing repair bar changed interval/material/ownership")
            delta = abs(raw["coordinate_mm"]-baseline[key]["coordinate_mm"])
            if (kind == "retained" and delta != 0) or (kind == "modified" and not 0 < delta <= checker["maximum_axis_nudge_mm"]+1e-9):
                raise ValueError("Repair coordinate differs from explicit tiny nudge policy")
        elif kind != "added":
            raise ValueError("New repair bar cannot impersonate a retained source")
        if kind == "added":
            number(raw["longitudinal_mm"][1]-raw["longitudinal_mm"][0], checker["minimum_new_length_mm"], checker["maximum_new_length_mm"])
        kinds[key] = kind
    mapped = set()
    for op in packet["operations"]:
        key = op["direction"], op["source_bar_id"]
        if key not in baseline or key in mapped or op["output_bar_ids"] != [key[1]] or op["kind"] != kinds[key]:
            raise ValueError("Repair operation mapping differs from complete source inventory")
        p = provenance[key]
        if any(op[field] != p[field] for field in ("reason", "source_fe_ids", "lane_ids")):
            raise ValueError("Operation differs from bar provenance")
        if op["kind"] == "modified" and (op["original_coordinate_mm"] != baseline[key]["coordinate_mm"] or op["coordinate_mm"] != final[key]["coordinate_mm"]):
            raise ValueError("Modified operation coordinate binding differs")
        mapped.add(key)
    if mapped != set(baseline):
        raise ValueError("Every immutable trimmed bar needs an operation")
    added = set()
    for op in packet["additions"]:
        key = op["direction"], op["bar_id"]
        if key not in final or kinds[key] != "added" or key in added or any(op[field] != provenance[key][field] for field in ("reason", "source_fe_ids", "lane_ids", "policy_id")):
            raise ValueError("Addition inventory differs from structural provenance")
        added.add(key)
    if added != set(final)-set(baseline):
        raise ValueError("Every new repair bar needs an explicit addition")
    positions = len(set((b["steel_class"].strip(), b["diameter_mm"], round(b["longitudinal_mm"][1]-b["longitudinal_mm"][0], 6)) for b in final.values()))
    mass = _graphic_mass(final)
    expected = packet["expected"]
    exact_keys(expected, ("physical_bar_count", "additional_mass_kg", "position_count", "source_zone_count"))
    _metrics(dict(physical_bar_count=expected["physical_bar_count"], mass_kg=expected["additional_mass_kg"], position_count=expected["position_count"]), len(final), mass, positions)
    number(expected["source_zone_count"], original["after"]["source_zone_count"], original["after"]["source_zone_count"], integer=True)
    _metrics(checker["physical_metrics"], len(final), mass, positions)
    _metrics(checker["physical_metrics_before"], len(baseline), _graphic_mass(baseline), original["after"]["position_count"])
    number(mass, 0, checker["maximum_mass_kg"]+1e-6)
    _coverage(checker["geometric_presence"], manifest)
    _coverage(checker["coverage_with_control_40d"], manifest)
    _collision_record(checker["collisions"], set(final))
    checks = packet["checks"]
    conditional = checker["collisions"]
    projected = {"coverage": checker["geometric_presence"]["status"], "anchorage_40d": checker["coverage_with_control_40d"]["status"],
        "stock_cutting": checker["stock_cutting"]["status"], "outer_boundary": {"status": "pass", "failure_count": 0},
        "collisions_3d": {"status": "not_checked", "proven_pair_count": None, "uncertain_pair_count": None},
        "conditional_collisions_3d": {"status": conditional["status"], "proven_pair_count": conditional["proven_collision_pair_count"], "uncertain_pair_count": conditional["uncertain_pair_count"]}}
    if checks != projected or checks["stock_cutting"] not in ("pass", "fail", "not_checked"):
        raise ValueError("Fresh check projection differs; actual Revit must stay not_checked")
    bars = []
    for key in sorted(final):
        raw, kind = final[key], kinds[key]
        axis = 0 if key[0].endswith("X") else 1
        start, end = list(result["offset_xy_mm"]), list(result["offset_xy_mm"])
        start[axis] += raw["longitudinal_mm"][0]
        end[axis] += raw["longitudinal_mm"][1]
        start[1-axis] += raw["coordinate_mm"]
        end[1-axis] += raw["coordinate_mm"]
        bars.append(dict(direction=key[0], bar_id=key[1], run_id=key[1], position_index=0,
            start_xy_mm=start, end_xy_mm=end, diameter_mm=raw["diameter_mm"], steel_class=raw["steel_class"],
            source_refs=list(raw["source_bar_ids"]), intersection=False, physically_cut=False,
            axis_nudged=kind == "modified", repair_kind=kind))
    result.update(input_schema=SCHEMA, repaired_input=copy.deepcopy(packet), bars=bars,
        summary=copy.deepcopy(expected), intersection_pair_count=0,
        source_blockers=["repaired-graphic-report-not-engineering-approval"])
    pair_count, budget = 0, 0
    for index, left in enumerate(bars):
        axis = 0 if left["direction"].endswith("X") else 1
        for right in bars[index+1:]:
            if left["direction"] != right["direction"]:
                continue
            budget += 1
            if budget > 2000000:
                raise ValueError("Bounded complete repaired XY pair check required")
            if (min(left["end_xy_mm"][axis], right["end_xy_mm"][axis]) > max(left["start_xy_mm"][axis], right["start_xy_mm"][axis])+1e-6
                    and abs(left["start_xy_mm"][1-axis]-right["start_xy_mm"][1-axis]) < (left["diameter_mm"]+right["diameter_mm"])/2-1e-6):
                left["intersection"] = right["intersection"] = True
                pair_count += 1
    result["intersection_pair_count"] = pair_count
    trim = result["trim_graphics"]
    trim.update(checks=copy.deepcopy(projected), repair_added_count=len(added),
        repair_modified_count=sum(int(k == "modified") for k in kinds.values()),
        physically_cut_piece_count=0, radius_sized_axis_nudged_piece_count=sum(int(k == "modified") for k in kinds.values()))
    return result
