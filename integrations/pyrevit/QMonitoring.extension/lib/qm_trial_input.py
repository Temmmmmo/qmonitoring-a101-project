# -*- coding: utf-8 -*-
"""Bounded, test-only JSON input. NOT a production v1/v2 reinforcement exporter."""
from __future__ import division

import json
import math
import os

SCHEMA = "qmonitoring-single-zone-trial/v1"
MAX_BYTES = 16384
try:
    integer_types = (int, long)
except NameError:
    integer_types = (int,)


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError("Unexpected or missing JSON fields; use the supplied trial sample")


def number(value, minimum, maximum, integer=False):
    types = integer_types if integer else integer_types + (float,)
    if isinstance(value, bool) or not isinstance(value, types):
        raise ValueError("Expected a JSON number, not a string or boolean")
    if not minimum <= value <= maximum or math.isnan(value) or math.isinf(value):
        raise ValueError("Trial numeric value is non-finite or outside the supported range")


def validate_trial_input(data):
    exact_keys(data, ("schema_version", "units", "mode", "placement_eligible", "zone"))
    if (data["schema_version"] != SCHEMA or data["units"] != "mm"
            or data["mode"] != "commit-readback-rollback" or data["placement_eligible"] is not False):
        raise ValueError("Only the explicit rollback-only trial schema in mm is accepted")
    zone = data["zone"]
    exact_keys(zone, ("host_id", "bar_type_id", "direction", "layout_rule", "diameter_mm",
                      "bar_count", "length_mm", "spacing_mm", "first_axis_offset_x_mm",
                      "first_axis_offset_y_mm", "coordinate_system", "z_policy", "anchorage_added_mm"))
    for key, expected in (("host_id", 407801), ("bar_type_id", 165163), ("diameter_mm", 25),
                           ("anchorage_added_mm", 0)):
        number(zone[key], expected, expected, integer=key.endswith("_id"))
    for key, expected in (("direction", "top-X"), ("layout_rule", "NumberWithSpacing"),
                           ("coordinate_system", "host-bbox-min-xy-revit-internal-axes"),
                           ("z_policy", "host-top-cover-model-radius")):
        if zone[key] != expected:
            raise ValueError("Unsupported trial policy: " + key)
    number(zone["bar_count"], 2, 32, integer=True)
    number(zone["length_mm"], 100, 6000)
    number(zone["spacing_mm"], 25, 300)
    number(zone["first_axis_offset_x_mm"], 0, 23600)
    number(zone["first_axis_offset_y_mm"], 0, 14000)
    return data


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field: " + key)
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON constant: " + value)


def load_trial_input(path):
    if os.path.splitext(path)[1].lower() != ".json":
        raise ValueError("Choose the supplied trial .json file, not a report or RVT")
    with open(path, "rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise ValueError("Trial input exceeds 16 KiB")
    data = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant)
    return validate_trial_input(data)
