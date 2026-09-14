from copy import deepcopy
import json
import math
from pathlib import Path

import pytest

from rebar.application.composite_host_review import (
    HOST_COORDINATE_POLICY, rectangular_host_from_reference, review_composite_host,
)

from test_composite_layout_review import recipe_mosaic, request_sample


def rectangle(box, z):
    x, y, right, top = box
    points = [[x, y, z], [right, y, z], [right, top, z], [x, top, z]]
    return [{"kind": "Line", "start_mm": a, "end_mm": b, "length_mm": math.dist(a, b)}
            for a, b in zip(points, (*points[1:], points[0]))]


def reference_sample(*, openings=False):
    floor = {"element_id": 42, "bbox_mm": {"min_mm": [-2000, -2000, -300], "max_mm": [10000, 10000, 0]},
             "covers": {k: {"distance_mm": 25} for k in ("top", "bottom", "other")}}
    for side, z, sign in (("top", 0, 1), ("bottom", -300, -1)):
        loops = [rectangle((-2000, -2000, 10000, 10000), z)]
        if openings:
            loops.insert(0, rectangle((5000, 5000, 6000, 6000), z))
        floor[side + "_faces"] = [{"plane": {"origin_mm": [0, 0, z], "normal": [0, 0, sign]}, "edge_loops": loops}]
    return {"schema_version": "revit-reference-probe/v1", "units": "mm", "status": "collected",
            "coordinate_system": "revit-internal-origin-and-axes", "read_only": True,
            "placement_eligible": False, "issues": [], "floor": floor}


@pytest.mark.parametrize("openings", [False, True])
def test_reference_uses_geometry_not_loop_order_and_keeps_solid_unverified(openings):
    report = reference_sample(openings=openings)
    before = deepcopy(report)
    host = rectangular_host_from_reference(report)
    assert len(host.openings_mm) == int(openings) and "Solid not verified" in host.source
    result = review_composite_host(recipe_mosaic(), request_sample(), report, coordinate_policy=HOST_COORDINATE_POLICY)
    assert result["checks"]["planar_host_and_openings"] == "pass"
    assert result["checks"]["live_host_geometry"] == "not_checked" and not result["placement_eligible"]
    assert result["live_coordinate_binding"] == "not_checked" and report == before


@pytest.mark.parametrize("bad", ["partial", "issues", "units", "writable", "direction", "curve", "missing_edge",
                                "duplicate_edge", "length", "nan", "different_faces", "bbox", "opening", "slope"])
def test_unsupported_snapshot_cannot_become_a_bbox_approximation(bad):
    report = reference_sample()
    floor = report["floor"]
    face = floor["top_faces"][0]
    if bad in ("partial", "issues", "units", "writable"):
        key, value = {"partial": ("status", "partial"), "issues": ("issues", ["error"]),
                      "units": ("units", "m"), "writable": ("read_only", False)}[bad]
        report[key] = value
    elif bad in ("direction", "slope"):
        face["plane"]["normal"] = [0, 0, -1] if bad == "direction" else [0.01, 0, 1]
    elif bad == "curve":
        face["edge_loops"][0][0]["kind"] = "Arc"
    elif bad == "missing_edge":
        face["edge_loops"][0].pop()
    elif bad == "duplicate_edge":
        face["edge_loops"][0][1] = deepcopy(face["edge_loops"][0][0])
    elif bad == "length":
        face["edge_loops"][0][0]["length_mm"] = 1
    elif bad == "nan":
        face["edge_loops"][0][0]["start_mm"][0] = float("nan")
    elif bad == "bbox":
        floor["bbox_mm"]["max_mm"][0] = 9999
    else:
        face["edge_loops"].append(rectangle((0, 0, 100, 100), 0))
        if bad == "opening":
            face["edge_loops"].append(rectangle((0, 0, 100, 100), 0))
    with pytest.raises(ValueError):
        rectangular_host_from_reference(report)


def test_reference_coordinates_are_an_explicit_research_assumption():
    with pytest.raises(ValueError, match="XY"):
        review_composite_host(recipe_mosaic(), request_sample(), reference_sample(), coordinate_policy="auto")


def test_actual_051_reference_has_matching_rectangular_faces_but_not_a_full_solid_proof():
    path = Path(__file__).resolve().parents[2] / "revit_info/051/qmonitoring-reference-20260910-095803-020000.json"
    if not path.exists():
        pytest.skip("private Revit snapshot unavailable")
    report = json.loads(path.read_text())
    host = rectangular_host_from_reference(report)
    assert host.outer_mm == pytest.approx((0, 0, 23600, 14000), abs=0.01)
    assert host.openings_mm == () and host.top_cover_mm == host.bottom_cover_mm == host.side_cover_mm == 25
