# -*- coding: utf-8 -*-
"""Pure readback measurements. Keep compatible with IronPython 2.7; no Revit imports."""
from __future__ import division

import math

TOLERANCE_MM = 0.1


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def subtract(a, b):
    return [x - y for x, y in zip(a, b)]


def distance(a, b):
    return math.sqrt(dot(subtract(a, b), subtract(a, b)))


def bounds(points):
    if not points:
        raise ValueError("Empty point collection")
    return {
        "min_mm": [min(p[i] for p in points) for i in range(3)],
        "max_mm": [max(p[i] for p in points) for i in range(3)],
    }


def measure_parallel_bars(bars):
    """Measure actual straight horizontal bars, never generate missing axes from spacing."""
    if not bars or any(len(b["curves"]) != 1 or b["curves"][0]["kind"] != "Line"
                       for b in bars):
        return {"status": "not_checked", "reason": "Expected nonempty single-line bars"}
    lines = [b["curves"][0] for b in bars]
    vector = subtract(lines[0]["end_mm"], lines[0]["start_mm"])
    length = math.sqrt(dot(vector, vector))
    if length < TOLERANCE_MM or abs(vector[2]) > TOLERANCE_MM:
        return {"status": "not_checked", "reason": "Bars are not horizontal straight lines"}
    along = [v / length for v in vector]
    # A canonical sign makes the report independent of the first curve's endpoint order.
    if along[0] < -1e-9 or (abs(along[0]) <= 1e-9 and along[1] < 0):
        along = [-v for v in along]
    across = [-along[1], along[0], 0.0]
    rows = []
    for bar, line in zip(bars, lines):
        a, b = line["start_mm"], line["end_mm"]
        if (distance(a, b) < TOLERANCE_MM or abs(a[2] - b[2]) > TOLERANCE_MM
                or abs(dot(subtract(b, a), across)) > TOLERANCE_MM):
            return {"status": "not_checked", "reason": "Bars are not parallel/horizontal"}
        rows.append({
            "system_id": bar["system_id"], "position_index": bar["position_index"],
            "axis_coordinate_mm": dot(a, across),
            "longitudinal_min_mm": min(dot(a, along), dot(b, along)),
            "longitudinal_max_mm": max(dot(a, along), dot(b, along)),
            "z_mm": a[2], "length_mm": line["length_mm"],
        })
    rows.sort(key=lambda row: row["axis_coordinate_mm"])
    coordinates = [r["axis_coordinate_mm"] for r in rows]
    return {
        "status": "measured", "bar_count": len(rows),
        "along_unit_vector": along, "across_unit_vector": across,
        "axes": rows,
        "actual_spacings_mm": [b - a for a, b in zip(coordinates, coordinates[1:])],
        "extreme_axis_distance_mm": coordinates[-1] - coordinates[0],
        "common_longitudinal_ends": all(
            max(r[key] for r in rows) - min(r[key] for r in rows) <= TOLERANCE_MM
            for key in ("longitudinal_min_mm", "longitudinal_max_mm", "z_mm")
        ),
    }


def measure_rectangle(curves, along, across):
    """Only accept an actual four-edge rectangle, not its bounding-box approximation."""
    if len(curves) != 4 or any(c["kind"] != "Line" for c in curves):
        raise ValueError("Boundary is not a four-line rectangle")
    points = [c[key] for c in curves for key in ("start_mm", "end_mm")]
    u = [dot(p, along) for p in points]
    v = [dot(p, across) for p in points]
    u0, u1, v0, v1 = min(u), max(u), min(v), max(v)
    if min(u1 - u0, v1 - v0) <= TOLERANCE_MM:
        raise ValueError("Degenerate rectangle")
    if max(p[2] for p in points) - min(p[2] for p in points) > TOLERANCE_MM:
        raise ValueError("Boundary is not horizontal")
    edges = []
    for curve in curves:
        corners = []
        for key in ("start_mm", "end_mm"):
            p = curve[key]
            pu, pv = dot(p, along), dot(p, across)
            if (min(abs(pu - u0), abs(pu - u1)) > TOLERANCE_MM
                    or min(abs(pv - v0), abs(pv - v1)) > TOLERANCE_MM):
                raise ValueError("Boundary has non-rectangular vertices")
            corners.append((int(abs(pu - u1) < abs(pu - u0)),
                            int(abs(pv - v1) < abs(pv - v0))))
        if sum(a != b for a, b in zip(corners[0], corners[1])) != 1:
            raise ValueError("Boundary has a diagonal/degenerate edge")
        edges.append(tuple(sorted(corners)))
    if len(set(edges)) != 4:
        raise ValueError("Boundary has duplicated/missing edges")
    return {"length_mm": u1 - u0, "width_mm": v1 - v0,
            "across_min_mm": v0, "across_max_mm": v1,
            "along_min_mm": u0, "along_max_mm": u1}


def compare_reference(measurement, boundary, bars, systems, requested_spacing_mm=None):
    """Numerical reference comparison only, not host safety or approval to place bars."""
    if measurement.get("status") != "measured":
        return {"status": "not_checked", "reason": measurement.get("reason")}
    try:
        rectangle = measure_rectangle(boundary, measurement["along_unit_vector"],
                                      measurement["across_unit_vector"])
    except ValueError as exc:
        return {"status": "not_checked", "reason": str(exc)}
    checks = []

    def check(name, actual, expected):
        checks.append({"id": name, "actual": actual, "expected": expected,
                       "matches": actual is not None and abs(actual - expected) <= TOLERANCE_MM})

    check("physical_bar_count", measurement["bar_count"], 9)
    check("boundary_length_mm", rectangle["length_mm"], 3900.0)
    check("boundary_width_mm", rectangle["width_mm"], 800.0)
    check("extreme_axis_distance_mm", measurement["extreme_axis_distance_mm"], 775.0)
    axes = measurement["axes"]
    check("first_axis_boundary_offset_mm",
          axes[0]["axis_coordinate_mm"] - rectangle["across_min_mm"], 12.5)
    check("last_axis_boundary_offset_mm",
          rectangle["across_max_mm"] - axes[-1]["axis_coordinate_mm"], 12.5)
    for i, gap in enumerate(measurement["actual_spacings_mm"]):
        check("actual_spacing_{0}_mm".format(i), gap, 96.875)
    for i, bar in enumerate(bars):
        check("bar_{0}_nominal_diameter_mm".format(i), bar["nominal_diameter_mm"], 25.0)
        check("bar_{0}_model_diameter_mm".format(i), bar["model_diameter_mm"], 25.0)
    for i, axis in enumerate(axes):
        check("bar_{0}_length_mm".format(i), axis["length_mm"], 3900.0)
        check("bar_{0}_start_offset_mm".format(i),
              axis["longitudinal_min_mm"] - rectangle["along_min_mm"], 0.0)
        check("bar_{0}_end_offset_mm".format(i),
              rectangle["along_max_mm"] - axis["longitudinal_max_mm"], 0.0)
    check("area_requested_top_major_spacing_mm", requested_spacing_mm, 100.0)
    checks.append({"id": "common_longitudinal_ends_and_elevation",
                   "actual": measurement["common_longitudinal_ends"], "expected": True,
                   "matches": measurement["common_longitudinal_ends"]})
    status = "matches" if all(c["matches"] for c in checks) else "differs"
    if requested_spacing_mm is None:
        status = "not_checked"
    return {"status": status,
            "tolerance_mm": TOLERANCE_MM, "rectangle": rectangle, "checks": checks,
            "system_max_spacings_mm": [{"element_id": s["element_id"],
                                       "max_spacing_mm": s.get("max_spacing_mm")} for s in systems],
            "scope": "Numerical manual-zone comparison; no engineering approval"}


def plane_depths(curves, faces, radius_mm):
    """Signed depth inside an outward face plane; not distance to its bounded polygon."""
    if len(faces) != 1 or faces[0].get("plane") is None:
        return {"status": "not_checked", "reason": "Requires exactly one planar face"}
    plane = faces[0]["plane"]
    if abs(abs(plane["normal"][2]) - 1.0) > 1e-8:
        return {"status": "not_checked", "reason": "Sloping face is outside this probe"}
    points = [p for curve in curves for p in curve["tessellated_points_mm"]]
    if not points:
        return {"status": "not_checked", "reason": "No curve points"}
    depths = [-dot(subtract(p, plane["origin_mm"]), plane["normal"]) for p in points]
    return {"status": "measured_to_infinite_face_plane",
            "axis_depth_min_mm": min(depths), "axis_depth_max_mm": max(depths),
            "body_clearance_min_mm": None if radius_mm is None else min(depths) - radius_mm,
            "host_containment_checked": False}
