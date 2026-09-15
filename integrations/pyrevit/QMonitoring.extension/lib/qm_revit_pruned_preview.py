# -*- coding: utf-8 -*-
"""Deletion-only graphic transport; never a physical coverage certificate."""
from __future__ import division, unicode_literals

import copy
import hashlib
import json

from qm_revit_probe import text_type
from qm_revit_source_preview import _finite_tree
from qm_trial_input import _reject_constant, _unique_object, exact_keys, number

SCHEMA = "graphic-bar-plan-pruned/v1"


def _metrics(value, count, mass, positions):
    number(value["physical_bar_count"], count, count, integer=True)
    number(value["position_count"], positions, positions, integer=True)
    number(value["mass_kg"], 0, 1e10)
    if abs(value["mass_kg"]-mass) > max(.001, mass*1e-9):
        raise ValueError("Cleanup mass disagrees with unchanged selected geometry")


def _coverage_nonregression(before, after):
    """Compare every original FE cell; tolerate only floating summation noise.

    This checks the *recorded* checker result, not coverage in the current RVT.
    An independent backend certificate must also report empty lost geometry.
    """
    _finite_tree(before)
    _finite_tree(after)
    if set(before) != set(after) or set(before) != set(("status", "uncovered_cell_count", "directions",
            "source_demand_removed", "policy", "shared_tolerance_coverage_status",
            "positive_area_loss_tolerance_mm2", "demand_transfer_used", "original_FE_geometry_changed",
            "weak_offer_As_summation")):
        raise ValueError("Complete original FE coverage record required")
    for field in before:
        if field != "directions" and before[field] != after[field]:
            raise ValueError("Deletion changed original FE coverage policy or status")
    if len(before["directions"]) != len(after["directions"]) or len(before["directions"]) != 4:
        raise ValueError("All four original FE directions required")
    for old_dir, new_dir in zip(before["directions"], after["directions"]):
        if set(old_dir) != set(new_dir) or set(old_dir) != set(("direction", "demanded_cell_count",
                "uncovered_cell_count", "uncovered_area_mm2", "cells")):
            raise ValueError("Complete FE direction coverage required")
        for field in old_dir:
            if field != "cells" and old_dir[field] != new_dir[field]:
                raise ValueError("Deletion changed FE direction summary")
        if len(old_dir["cells"]) != len(new_dir["cells"]) or len(old_dir["cells"]) != old_dir["demanded_cell_count"]:
            raise ValueError("Complete original FE cell inventory required")
        for old, new in zip(old_dir["cells"], new_dir["cells"]):
            if set(old) != set(new) or set(old) != set(("cell_id", "uncovered_area_mm2", "covered")):
                raise ValueError("Complete FE cell coverage required")
            if old["cell_id"] != new["cell_id"] or old["covered"] is not new["covered"]:
                raise ValueError("Deletion changed previously covered FE cell")
            number(old["uncovered_area_mm2"], 0, 1e12)
            number(new["uncovered_area_mm2"], 0, 1e12)
            # 1e-8 mm² is below practical area resolution and covers the observed
            # 3e-11 mm² double-summation discrepancy, never a changed cell status.
            if abs(new["uncovered_area_mm2"]-old["uncovered_area_mm2"]) > 1e-8:
                raise ValueError("Deletion changed original FE uncovered geometry")
            if old["covered"] and (old["uncovered_area_mm2"] != 0 or new["uncovered_area_mm2"] != 0):
                raise ValueError("Previously covered FE geometry was lost")


def _collision_record(pairs, retained):
    """Check the recorded pair inventory, never attempt a native 3D solve."""
    if (pairs.get("schema_version") != "shaped-collision-check/research-v1"
            or pairs.get("collision_scope") != "every_distinct_pair_of_provided_physical_bars"
            or pairs.get("modeled_cross_direction_and_face_3d_checked") is not True
            or pairs.get("placement_eligible") is not False or pairs.get("production_ready") is not False):
        raise ValueError("Complete research collision scope required")
    number(pairs.get("physical_bar_count"), len(retained), len(retained), integer=True)
    number(pairs.get("additional_bar_count"), len(retained), len(retained), integer=True)
    known, all_pairs = set(), []
    for field, count_field, wanted_status in (("proven_collision_pairs", "proven_collision_pair_count", "collision"),
            ("uncertain_pairs", "uncertain_pair_count", "uncertain")):
        rows = pairs.get(field)
        if not isinstance(rows, list) or len(rows) > 2000000:
            raise ValueError("Complete bounded collision pair list required")
        number(pairs.get(count_field), len(rows), len(rows), integer=True)
        for row in rows:
            if not isinstance(row, dict) or row.get("status") != wanted_status:
                raise ValueError("Collision pair status differs from reported inventory")
            ends = []
            for side in ("first", "second"):
                end = row.get(side)
                if not isinstance(end, dict) or end.get("shape_kind") != "straight" or end.get("role") != "additional":
                    raise ValueError("Collision pair endpoint is not a selected straight additional bar")
                key = end.get("direction"), end.get("bar_id")
                if key not in retained:
                    raise ValueError("Collision pair references removed or unknown physical bar")
                ends.append(key)
            if ends[0] == ends[1]:
                raise ValueError("A bar cannot be its own collision pair")
            identity = tuple(sorted(ends))
            if identity in known:
                raise ValueError("Collision pair listed twice")
            known.add(identity)
            all_pairs.append(identity)
    count = len(all_pairs)
    number(pairs.get("candidate_pairs_checked"), count, 2000000, integer=True)
    if count:
        if pairs.get("status") != "fail" or pairs.get("complete_no_body_collision_proof") is not False:
            raise ValueError("Recorded collisions cannot have a pass certificate")
        status = "fail"
    else:
        if pairs.get("status") not in ("pass", "not_checked"):
            raise ValueError("Zero recorded pairs contradict collision status")
        # A zero-count list alone is not a proof. Even a complete proof only
        # covers the provided inventory, not background/RVT Rebar when absent.
        status = ("pass" if pairs.get("status") == "pass"
            and pairs.get("complete_no_body_collision_proof") is True
            and pairs.get("background_inventory_complete") is True else "not_checked")
    return {"status": status, "proven_pair_count": len(pairs["proven_collision_pairs"]),
        "uncertain_pair_count": len(pairs["uncertain_pairs"])}


def build_pruned_primitives(packet, offset_x_mm, offset_y_mm):
    # Lazy import avoids a module cycle with the public dispatch boundary.
    from qm_revit_plan_preview import _graphic_bar_primitives, _graphic_inventory, _graphic_mass
    exact_keys(packet, ("schema_version", "units", "case_id", "geometry_kind", "source_stage",
        "source_trim_packet", "source_trim_packet_json", "source_trim_packet_sha256",
        "retained_bar_ids", "expected", "cleanup", "placement_eligible", "engineering_approval"))
    if (packet["schema_version"] != SCHEMA or packet["units"] != "mm"
            or packet["geometry_kind"] != "straight-bars-only"
            or packet["source_stage"] != "redundant-trimmed-bars-pruned"
            or packet["placement_eligible"] is not False or packet["engineering_approval"] is not False):
        raise ValueError("Exact unapproved deletion-only graphic schema required")
    encoded = packet["source_trim_packet_json"]
    if not isinstance(encoded, text_type) or not 1 <= len(encoded) <= 16*1024*1024:
        raise ValueError("Bounded exact original trim JSON string required")
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != packet["source_trim_packet_sha256"]:
        raise ValueError("Exact original trim bytes SHA256 differs")
    original = json.loads(encoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if original != packet["source_trim_packet"] or original.get("case_id") != packet["case_id"]:
        raise ValueError("Embedded original trim packet differs from the hashed input")
    primitives = _graphic_bar_primitives(original, offset_x_mm, offset_y_mm)
    if original.get("respect_openings") is not True:
        raise ValueError("Deletion cleanup must retain the measured openings policy")
    source = _graphic_inventory(original["directions"], after=True)
    rows, retained = packet["retained_bar_ids"], set()
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(source):
        raise ValueError("Complete bounded nonempty retained selection required")
    for row in rows:
        exact_keys(row, ("direction", "bar_id"))
        key = row["direction"], row["bar_id"]
        if key not in source or key in retained:
            raise ValueError("Unknown or duplicate retained physical bar")
        retained.add(key)
    cleanup = packet["cleanup"]
    _finite_tree(cleanup)
    if (cleanup["schema_version"] != "trimmed-length-cleanup-check/v1" or cleanup["units"] != "mm"
            or cleanup["accepted_nonregression"] is not True or cleanup["retained_geometry_exactly_unchanged"] is not True
            or cleanup["prior_stock_pass_preserved"] is not True or cleanup["openings_retained"] is not True
            or cleanup["previously_covered_geometry_lost"] != []):
        raise ValueError("Explicit independently checked deletion-only nonregression required")
    for flag in ("placement_eligible", "engineering_approval", "original_FE_geometry_changed",
                 "source_demand_removed", "weak_As_summation", "lengths_rounded_or_extended",
                 "concrete_cover_included", "all_TZ_requirements_certified"):
        if cleanup[flag] is not False:
            raise ValueError("Cleanup cannot change scope or grant engineering permission")
    number(cleanup["removed_bar_count"], len(source)-len(retained), len(source)-len(retained), integer=True)
    number(cleanup["material_boundary_failures_after"], 0, 0, integer=True)
    if original["checks"]["material_boundary"]["status"] != "pass":
        raise ValueError("Deletion cleanup cannot borrow an invalid source host certificate")
    mapping, seen = cleanup["piece_mapping"], set()
    if not isinstance(mapping, list) or len(mapping) != len(source):
        raise ValueError("Every trimmed source piece needs a kept/removed mapping")
    for row in mapping:
        exact_keys(row, ("direction", "input_bar_id", "output_bar_ids", "source_bar_ids", "reason"))
        key = row["direction"], row["input_bar_id"]
        if key not in source or key in seen:
            raise ValueError("Unknown or duplicate cleanup source piece")
        seen.add(key)
        expected = [key[1]] if key in retained else []
        reason = "unchanged" if expected else "redundant_for_both_original_FE_coverage_policies"
        if (row["output_bar_ids"] != expected or row["source_bar_ids"] != source[key]["source_bar_ids"]
                or row["reason"] != reason):
            raise ValueError("Cleanup mapping disagrees with selected unchanged pieces")
    selected = dict((key, source[key]) for key in retained)
    mass = _graphic_mass(selected)
    positions = len(set((b["steel_class"].strip(), b["diameter_mm"],
        round(b["longitudinal_mm"][1]-b["longitudinal_mm"][0], 6)) for b in selected.values()))
    expected = packet["expected"]
    exact_keys(expected, ("physical_bar_count", "additional_mass_kg", "position_count", "source_zone_count"))
    _metrics({"physical_bar_count": expected["physical_bar_count"], "mass_kg": expected["additional_mass_kg"],
              "position_count": expected["position_count"]}, len(selected), mass, positions)
    number(expected["source_zone_count"], original["after"]["source_zone_count"],
           original["after"]["source_zone_count"], integer=True)
    _metrics(cleanup["physical_metrics_before"], len(source), _graphic_mass(source), original["after"]["position_count"])
    _metrics(cleanup["physical_metrics"], len(selected), mass, positions)
    trim = primitives["trim_graphics"]
    for name, key in (("geometric_presence", "coverage"), ("control_40d", "anchorage_40d")):
        before, after = cleanup["coverage_before"][name], cleanup["coverage_after"][name]
        if before["status"] != original["checks"][key]:
            raise ValueError("Deletion-only cleanup cannot change original FE coverage results")
        _coverage_nonregression(before, after)
        trim["checks"][key] = after["status"]
    for name, original_key in (("stock_cutting_before", "stock_cutting"),):
        if cleanup[name]["status"] != original["checks"][original_key]:
            raise ValueError("Cleanup source cutting status differs")
    if cleanup["stock_cutting"]["status"] not in ("pass", "fail", "not_checked"):
        raise ValueError("Explicit final stock status required")
    trim["checks"]["stock_cutting"] = cleanup["stock_cutting"]["status"]
    trim["checks"]["collisions_3d"] = _collision_record(cleanup["collisions"], retained)
    primitives["bars"] = [bar for bar in primitives["bars"] if (bar["direction"], bar["bar_id"]) in retained]
    # Recompute visible XY overlaps after deletion rather than carrying old red flags.
    xy_pairs, operations = 0, 0
    for bar in primitives["bars"]:
        bar["intersection"] = False
    for index, left in enumerate(primitives["bars"]):
        axis = 0 if left["direction"].endswith("X") else 1
        for right in primitives["bars"][index+1:]:
            if left["direction"] != right["direction"]:
                continue
            operations += 1
            if operations > 2000000:
                raise ValueError("Bounded complete XY pair check required")
            if (min(left["end_xy_mm"][axis], right["end_xy_mm"][axis]) >
                    max(left["start_xy_mm"][axis], right["start_xy_mm"][axis])+1e-6
                    and abs(left["start_xy_mm"][1-axis]-right["start_xy_mm"][1-axis]) <
                    (left["diameter_mm"]+right["diameter_mm"])/2-1e-6):
                left["intersection"] = right["intersection"] = True
                xy_pairs += 1
    trim["physically_cut_piece_count"] = sum(int(b["physically_cut"]) for b in primitives["bars"])
    trim["radius_sized_axis_nudged_piece_count"] = sum(int(b["axis_nudged"]) for b in primitives["bars"])
    trim["cleanup_removed_count"] = len(source)-len(selected)
    trim["cleanup_source_piece_count"] = len(source)
    trim["cleanup_preserves_recorded_FE_coverage"] = True
    primitives.update(input_schema=SCHEMA, pruned_input=copy.deepcopy(packet),
        summary=copy.deepcopy(expected), intersection_pair_count=xy_pairs,
        source_blockers=["pruned-cut-draft-not-engineering-acceptance"])
    return primitives
