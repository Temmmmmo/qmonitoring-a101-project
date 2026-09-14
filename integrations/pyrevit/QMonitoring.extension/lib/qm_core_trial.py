# -*- coding: utf-8 -*-
"""Validate one core zone and compare its two physical runs. IronPython 2.7, no API."""
from __future__ import division

import json
import math
import os

from qm_trial_input import _reject_constant, _unique_object, exact_keys, number
from qm_trial_geometry import compare_trial, make_run_plan
from qm_probe_geometry import distance

try:
    string_types = (basestring,)
except NameError:
    string_types = (str,)

SCHEMA = "qmonitoring-core-zone-trial/v1"
BLOCKERS = ["composite-demand-coverage", "composite-minimum-width", "a101-composite-positions",
            "host-boundary-cover-openings", "xy-layer-order",
            "background-and-additions-3d-collisions", "revit-readback"]


def same(actual, expected):
    """Strict shape/types and finite numbers, including refusal of boolean quantities."""
    if isinstance(expected, dict):
        exact_keys(actual, expected)
        for key in expected:
            same(actual[key], expected[key])
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ValueError("Core packet array shape differs")
        for a, b in zip(actual, expected):
            same(a, b)
    elif isinstance(expected, bool) or expected is None:
        if actual is not expected:
            raise ValueError("Core packet policy differs")
    elif isinstance(expected, string_types):
        if not isinstance(actual, string_types) or actual != expected:
            raise ValueError("Unsupported core packet schema/policy")
    else:
        number(actual, expected - 1e-6, expected + 1e-6)


def label(value):
    if not isinstance(value, string_types) or not value.strip() or len(value) > 1024:
        raise ValueError("Expected a bounded nonempty text label")


def validate_core_input(data):
    exact_keys(data, ("schema_version", "mode", "units", "placement_eligible", "binding", "core_export"))
    for key, value in (("schema_version", SCHEMA), ("mode", "commit-readback-rollback"),
                       ("units", "mm"), ("placement_eligible", False)):
        same(data[key], value)
    binding = data["binding"]
    exact_keys(binding, ("host_id", "bar_type_id", "source", "coordinate_system", "offset_x_mm",
                         "offset_y_mm", "z_policy", "background_action", "anchorage_action"))
    for key, value in (("host_id", 407801), ("bar_type_id", 165160)):
        number(binding[key], value, value, integer=True)
    for key, value in (("source", "isolated-laboratory-window-not-project-grid"),
                       ("coordinate_system", "core-xy-plus-offset-from-host-bbox-min"),
                       ("z_policy", "host-top-cover-model-radius"), ("background_action", "do-not-create"),
                       ("anchorage_action", "use-installed-length-without-extra-extension")):
        same(binding[key], value)
    number(binding["offset_x_mm"], 0, 23600)
    number(binding["offset_y_mm"], 0, 14000)
    core = data["core_export"]
    exact_keys(core, ("schema_version", "contract_status", "units", "source_zone_id", "direction",
                      "demand_bbox_mm", "level_index", "recipe", "placement_source", "background",
                      "components", "metrics", "checks", "placement_boundary_policy"))
    same(core["schema_version"], "reinforcement-zone-revit/v2")
    same(core["contract_status"], "draft")
    same(core["units"], "mm")
    same(core["direction"], {"layer": "top", "axis": "X"})
    same(core["placement_boundary_policy"], "body_bbox_is_not_an_area_boundary_or_a_host_clearance_check")
    label(core["source_zone_id"])
    label(core["placement_source"])
    number(core["level_index"], 0, 100, integer=True)
    same(core["recipe"], {"background": {"step": 300, "diameter": 18},
                           "additions": [{"step": 150, "diameter": 18}]})
    bbox = core["demand_bbox_mm"]
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("Expected a core demand rectangle")
    same(bbox[:2], [0, 0])  # First lab profile: local origin, not a DXF/project transform.
    number(bbox[2], 100, 4500)
    number(bbox[3], 300, 4000)
    components = core["components"]
    if not isinstance(components, list) or len(components) != 1:
        raise ValueError("Exactly one addition is supported; never drop other components")
    c = components[0]
    exact_keys(c, ("component_index", "diameter_mm", "nominal_step_mm", "placement", "axis_window_mm",
                   "axis_coordinates_mm", "uniform_runs", "bar_count", "bar_axis_bbox_mm",
                   "straight_bar_body_bbox_mm", "required_length_mm", "anchored_length_mm",
                   "installed_length_mm", "mass_kg"))
    number(c["component_index"], 0, 0, integer=True)
    number(c["diameter_mm"], 18, 18, integer=True)
    number(c["nominal_step_mm"], 150, 150, integer=True)
    placement = c["placement"]
    exact_keys(placement, ("pattern", "origin_mm", "axis_depth_from_face_mm"))
    number(placement["origin_mm"], -10000, 10000)
    same(placement, {"pattern": {"period_mm": 300, "offsets_mm": [100, 200]},
                     "origin_mm": placement["origin_mm"], "axis_depth_from_face_mm": None})
    same(core["background"], {"specification": {"step": 300, "diameter": 18},
        "placement": {"pattern": {"period_mm": 300, "offsets_mm": [0]},
                      "origin_mm": placement["origin_mm"], "axis_depth_from_face_mm": None},
        "included_in_additional_mass": False})
    same(c["axis_window_mm"], [bbox[1], bbox[3]])
    runs = []
    # Independent period enumeration; the exported coordinates/runs do not validate themselves.
    for offset in (100, 200):
        origin = placement["origin_mm"] + offset
        first_k = int(math.ceil((bbox[1] - origin - 1e-6) / 300))
        last_k = int(math.floor((bbox[3] - origin + 1e-6) / 300))
        count = last_k - first_k + 1
        number(count, 2, 16, integer=True)
        runs.append({"first_axis_mm": origin + first_k * 300, "actual_step_mm": 300, "bar_count": count})
    runs.sort(key=lambda run: run["first_axis_mm"])
    same(c["uniform_runs"], runs)
    for run in c["uniform_runs"]:
        number(run["bar_count"], 2, 16, integer=True)
    axes = sorted(run["first_axis_mm"] + i * 300 for run in runs for i in range(run["bar_count"]))
    same(c["axis_coordinates_mm"], axes)
    number(c["bar_count"], len(axes), len(axes), integer=True)
    same(c["required_length_mm"], bbox[2])
    # This isolated profile uses the core's default 40d each end and no cut-length rounding.
    length = bbox[2] + 2 * 40 * 18
    same(c["anchored_length_mm"], length)
    same(c["installed_length_mm"], length)
    same(c["bar_axis_bbox_mm"], [-720, axes[0], bbox[2] + 720, axes[-1]])
    same(c["straight_bar_body_bbox_mm"], [-720, axes[0] - 9, bbox[2] + 720, axes[-1] + 9])
    mass = 0.006165 * 18 ** 2 * (length / 1000) * len(axes)
    same(c["mass_kg"], mass)
    same(core["metrics"], {"zone_count": 1, "component_count": 1, "uniform_run_count": 2,
                            "physical_bar_count": len(axes), "additional_mass_kg": mass,
                            "additional_bar_length_mm": length * len(axes)})
    checks = core["checks"]
    exact_keys(checks, ("geometry_and_schedule", "export_eligible", "blocking_check_ids", "diagnostics"))
    same(checks["geometry_and_schedule"], "pass")
    same(checks["export_eligible"], False)
    same(checks["blocking_check_ids"], BLOCKERS)
    if not isinstance(checks["diagnostics"], list) or not 1 <= len(checks["diagnostics"]) <= 20:
        raise ValueError("Missing core diagnostic limits")
    for diagnostic in checks["diagnostics"]:
        label(diagnostic)
    return data


def load_core_input(path):
    if os.path.splitext(path)[1].lower() != ".json":
        raise ValueError("Choose the core-axis-trial.json input")
    with open(path, "rb") as stream:
        content = stream.read(65537)
    if len(content) > 65536:
        raise ValueError("Core trial JSON exceeds 64 KiB")
    return validate_core_input(json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                                          parse_constant=_reject_constant))


def make_core_plan(floor, bar_type, data):
    validate_core_input(data)
    binding, core = data["binding"], data["core_export"]
    if floor["element_id"] != binding["host_id"] or bar_type["element_id"] != binding["bar_type_id"]:
        raise ValueError("Resolved host/type IDs differ from the explicit binding")
    c = core["components"][0]
    plans = [make_run_plan(floor, bar_type, 18, run["bar_count"], c["installed_length_mm"],
                          run["actual_step_mm"], binding["offset_x_mm"] + c["bar_axis_bbox_mm"][0],
                          binding["offset_y_mm"] + run["first_axis_mm"]) for run in c["uniform_runs"]]
    result = {"host_id": binding["host_id"], "bar_type_id": binding["bar_type_id"], "runs": plans,
              "source_zone_id": core["source_zone_id"], "physical_bar_count": c["bar_count"],
              "mass_kg": c["mass_kg"], "placement_eligible": False, "background_created": False}
    for key in ("body_envelope_mm", "reservation_mm"):
        result[key] = {"min_mm": [min(p[key]["min_mm"][i] for p in plans) for i in range(3)],
                       "max_mm": [max(p[key]["max_mm"][i] for p in plans) for i in range(3)]}
    return result


def compare_core_trial(plan, readback):
    sets = readback["sets"]
    comparisons = [compare_trial(p, b) for p, b in zip(plan["runs"], sets)]
    count = sum(len(b["bars"]) for b in sets)
    unique = len(set(b["element_id"] for b in sets)) == len(sets)
    # Independent physical mass from FINAL endpoints (not the API Length field or core mass).
    mass = sum(0.006165 * b["bar_type"]["model_diameter_mm"] ** 2
               * distance(curve["start_mm"], curve["end_mm"]) / 1000
               for b in sets for bar in b["bars"] for curve in bar["curves"])
    good = (len(sets) == len(plan["runs"]) and unique and count == plan["physical_bar_count"]
            and abs(mass - plan["mass_kg"]) < 1e-6
            and all(c["status"] == "matches" for c in comparisons))
    return {"status": "matches" if good else "differs", "runs": comparisons, "unique_sets": unique,
            "physical_bar_count": count, "mass_from_readback_kg": mass,
            "expected_mass_kg": plan["mass_kg"], "mass_matches": abs(mass - plan["mass_kg"]) < 1e-6,
            "scope": "one core addition; no engineering approval or background placement"}
