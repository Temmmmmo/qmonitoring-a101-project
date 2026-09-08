# -*- coding: utf-8 -*-
"""Strict geometry for ONE rollback-only API experiment, not an engineering exporter."""
from __future__ import division

import math

from qm_probe_geometry import TOLERANCE_MM, bounds, distance, measure_rectangle


def close(actual, expected):
    return actual is not None and abs(actual - expected) <= TOLERANCE_MM


def validate_prism(floor, solid):
    """Accept only the axis-aligned, six-faced, unperforated reference slab."""
    box = floor["bbox_mm"]
    lo, hi = box["min_mm"], box["max_mm"]
    if not all(close(hi[i] - lo[i], size) for i, size in enumerate((23600, 14000, 300))):
        raise ValueError("Reference slab dimensions changed")
    if not all(close(floor["covers"][side]["distance_mm"], 25) for side in ("top", "bottom", "other")):
        raise ValueError("Reference covers must all be 25 mm")
    if len(solid["faces"]) != 6:
        raise ValueError("Only a six-face solid is supported; openings/joins require review")
    seen = set()
    for face in solid["faces"]:
        plane = face.get("plane")
        if plane is None or len(face["edge_loops"]) != 1:
            raise ValueError("Curved faces or inner loops are outside this experiment")
        normal = plane["normal"]
        axis = max(range(3), key=lambda i: abs(normal[i]))
        if abs(abs(normal[axis]) - 1) > 1e-8 or any(
                abs(normal[i]) > 1e-8 for i in range(3) if i != axis):
            raise ValueError("Sloping/rotated faces are not supported")
        side = int(normal[axis] > 0)
        if (axis, side) in seen:
            raise ValueError("Duplicate solid face")
        seen.add((axis, side))
        expected_plane = hi[axis] if side else lo[axis]
        if not close(plane["origin_mm"][axis], expected_plane):
            raise ValueError("Face plane does not match the slab bounds")
        other = [i for i in range(3) if i != axis]
        projected = []
        for curve in face["edge_loops"][0]:
            row = {"kind": curve["kind"]}
            for key in ("start_mm", "end_mm"):
                point = curve[key]
                if not close(point[axis], expected_plane):
                    raise ValueError("Edge is not in the expected face plane")
                row[key] = [point[other[0]], point[other[1]], 0]
            projected.append(row)
        rectangle = measure_rectangle(projected, [1, 0, 0], [0, 1, 0])
        for key, expected in (("along_min_mm", lo[other[0]]), ("along_max_mm", hi[other[0]]),
                              ("across_min_mm", lo[other[1]]), ("across_max_mm", hi[other[1]])):
            if not close(rectangle[key], expected):
                raise ValueError("Face does not span the full box; no bbox approximation allowed")
    expected_volume = (hi[0] - lo[0]) * (hi[1] - lo[1]) * (hi[2] - lo[2])
    if abs(solid["volume_mm3"] - expected_volume) > expected_volume * 1e-6:
        raise ValueError("Solid volume differs from the prism; possible void or unsupported geometry")
    for side, sign, elevation in (("top", 1, hi[2]), ("bottom", -1, lo[2])):
        faces = floor[side + "_faces"]
        if len(faces) != 1 or faces[0]["plane"] is None or len(faces[0]["edge_loops"]) != 1:
            raise ValueError("Requires one unperforated planar host face on each side")
        if not close(faces[0]["plane"]["origin_mm"][2], elevation) or any(
                abs(a - b) > 1e-8 for a, b in zip(faces[0]["plane"]["normal"], [0, 0, sign])):
            raise ValueError("Host top/bottom faces disagree with the solid")
    return {"status": "passed", "scope": "six-face rectangular reference prism only"}


def make_trial_plan(floor, bar_type, trial_input=None):
    """Explicit local window; never search, clip, infer DXF Z, or append anchorage."""
    from qm_trial_input import validate_trial_input

    zone = validate_trial_input(trial_input)["zone"] if trial_input is not None else {}
    if not close(bar_type["nominal_diameter_mm"], 25) or not close(bar_type["model_diameter_mm"], 25):
        raise ValueError("The selected type must have nominal AND model diameter 25 mm")
    lo, hi = floor["bbox_mm"]["min_mm"], floor["bbox_mm"]["max_mm"]
    top = floor["top_faces"][0]["plane"]["origin_mm"][2]
    z = top - floor["covers"]["top"]["distance_mm"] - 12.5
    x = lo[0] + zone.get("first_axis_offset_x_mm", 1000)
    y = lo[1] + zone.get("first_axis_offset_y_mm", 1012.5)
    count, spacing, length = zone.get("bar_count", 9), zone.get("spacing_mm", 96.875), zone.get("length_mm", 3900)
    axes = [{"start_mm": [x, y + i * spacing, z],
             "end_mm": [x + length, y + i * spacing, z]} for i in range(count)]
    box = bounds([p for axis in axes for p in (axis["start_mm"], axis["end_mm"])])
    # A conservative body envelope (also expands the straight cut ends by radius).
    body = {"min_mm": [v - 12.5 for v in box["min_mm"]],
            "max_mm": [v + 12.5 for v in box["max_mm"]]}
    margins = [("other", body["min_mm"][0] - lo[0]), ("other", hi[0] - body["max_mm"][0]),
               ("other", body["min_mm"][1] - lo[1]), ("other", hi[1] - body["max_mm"][1]),
               ("bottom", body["min_mm"][2] - lo[2]), ("top", hi[2] - body["max_mm"][2])]
    if any(gap < floor["covers"][side]["distance_mm"] - TOLERANCE_MM for side, gap in margins):
        raise ValueError("Trial body envelope violates host cover; no clipping allowed")
    # Reserve the whole slab depth plus 100 mm, not merely the new top bar plane.
    reservation = {"min_mm": [body["min_mm"][0] - 100, body["min_mm"][1] - 100, lo[2] - 100],
                   "max_mm": [body["max_mm"][0] + 100, body["max_mm"][1] + 100, hi[2] + 100]}
    return {"host_id": floor["element_id"], "bar_type_id": bar_type["element_id"],
            "bar_count": count, "diameter_mm": 25.0, "length_mm": length, "spacing_mm": spacing,
            "layout_rule": "NumberWithSpacing", "normal": [0, 1, 0], "axes": axes,
            "body_envelope_mm": body, "reservation_mm": reservation,
            "source": ("explicit test-only JSON zone" if trial_input is not None else
                       "confirmed manual geometry translated to an isolated test window"),
            "z_source": "host top face minus top cover minus model radius",
            "anchorage_added_mm": 0, "placement_eligible": False}


def compare_trial(plan, readback):
    """Check absolute endpoints, not just lengths/spacings that could hide a translation."""
    checks = []

    def check(name, actual, expected, numeric=True):
        checks.append({"id": name, "actual": actual, "expected": expected,
                       "matches": close(actual, expected) if numeric else actual == expected})

    check("host_id", readback["host_id"], plan["host_id"], False)
    check("bar_type_id", readback["bar_type"]["element_id"], plan["bar_type_id"], False)
    check("quantity", readback["quantity"], plan["bar_count"], False)
    check("position_count", readback["number_of_bar_positions"], plan["bar_count"], False)
    check("physical_count", len(readback["bars"]), plan["bar_count"], False)
    check("layout_rule", readback["layout_rule"], "NumberWithSpacing", False)
    for key in ("nominal_diameter_mm", "model_diameter_mm"):
        check(key, readback["bar_type"][key], plan["diameter_mm"])
    for end, hook in enumerate(readback["hook_type_ids"]):
        check("hook_{0}".format(end), hook, -1, False)
    bars = readback["bars"]
    if any(len(b["curves"]) != 1 or b["curves"][0]["kind"] != "Line" for b in bars):
        check("single_line_bars", False, True, False)
    else:
        lines = [b["curves"][0] for b in bars]
        lines.sort(key=lambda c: min(c["start_mm"][1], c["end_mm"][1]))
        for i, (actual, expected) in enumerate(zip(lines, plan["axes"])):
            ends = sorted([actual["start_mm"], actual["end_mm"]])
            check("axis_{0}_start_error_mm".format(i), distance(ends[0], expected["start_mm"]), 0)
            check("axis_{0}_end_error_mm".format(i), distance(ends[1], expected["end_mm"]), 0)
            check("axis_{0}_length_mm".format(i), actual["length_mm"], plan["length_mm"])
            check("axis_{0}_endpoint_length_mm".format(i), distance(*ends), plan["length_mm"])
            check("axis_{0}_finite".format(i), all(
                not math.isnan(v) and not math.isinf(v) for p in ends for v in p), True, False)
    return {"status": "matches" if all(c["matches"] for c in checks) else "differs",
            "tolerance_mm": TOLERANCE_MM, "checks": checks,
            "scope": "uncommitted test set; no engineering or commit-time acceptance"}
