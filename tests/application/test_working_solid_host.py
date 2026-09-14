from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import runpy
import sys

import pytest

from rebar.application.working_solid_host import inspect_working_solid, review_working_solid_bars
from rebar.application.working_host import load_working_host_json
from rebar.optimization.services.solid_host import outside_box_volume_mm3


def stepped_snapshot():
    """Independent voxel oracle: 47 boxes, one through hole and one half-height recess."""
    voxels = {(x, y, z) for x in range(5) for y in range(5) for z in range(2)
              if (x, y) != (2, 2) and (x, y, z) != (4, 2, 1)}
    scale = (300, 300, 100)
    faces = []
    for voxel in sorted(voxels):
        for axis in range(3):
            other = [i for i in range(3) if i != axis]
            for sign in (-1, 1):
                neighbor = list(voxel)
                neighbor[axis] += sign
                if tuple(neighbor) in voxels:
                    continue
                points = []
                for first, second in ((0, 0), (1, 0), (1, 1), (0, 1)):
                    point = [v*s for v, s in zip(voxel, scale)]
                    point[axis] += scale[axis] if sign == 1 else 0
                    point[other[0]] += first * scale[other[0]]
                    point[other[1]] += second * scale[other[1]]
                    points.append(point)
                normal = [0, 0, 0]
                normal[axis] = sign
                faces.append({"plane": {"normal": normal, "origin_mm": points[0]},
                    "edge_loops": [[{"kind": "Line", "start_mm": a, "end_mm": b, "length_mm": math.dist(a, b)}
                                    for a, b in zip(points, points[1:] + points[:1])]]})
    floor = {"element_id": 42, "bbox_mm": {"min_mm": [0, 0, 0], "max_mm": [1500, 1500, 200]},
             "covers": {side: {"distance_mm": 25} for side in ("top", "bottom", "other")}}
    for side, sign, z in (("bottom", -1, 0), ("top", 1, 200)):
        floor[side + "_faces"] = deepcopy([f for f in faces if f["plane"]["normal"] == [0, 0, sign]
                                           and f["plane"]["origin_mm"][2] == z])
    return {"schema_version": "revit-full-plate-trial-report/v1", "units": "mm",
        "coordinate_system": "revit-internal-origin-and-axes", "placement_eligible": False,
        "status": "blocked_setup", "host_id": 42, "read_issues": [], "host": floor,
        "host_solid": {"faces": faces, "volume_mm3": len(voxels) * math.prod(scale)}}


def geometry_packet():
    """Geometry-only fixture, deliberately not a complete source certificate."""
    directions = []
    for layer in ("bottom", "top"):
        for axis in ("X", "Y"):
            directions.append({"direction": f"{layer}-{axis}", "runs": [{"id": f"{layer}-{axis}",
                "start_xy_mm": [50, 50], "end_xy_mm": [250, 50] if axis == "X" else [50, 250],
                "bar_count": 1, "spacing_mm": 0, "diameter_mm": 10}]})
    return {"schema_version": "physical-bar-plan-trial/v1", "units": "mm", "placement_eligible": False,
            "expected": {"physical_bar_count": 4}, "directions": directions}


def test_all_faces_reconstruct_voxel_solid_and_exact_recess_volume():
    report = stepped_snapshot()
    before = deepcopy(report)
    host, result = inspect_working_solid(report)
    assert report == before
    assert [s.footprint.area for s in host.sections] == [24*300**2, 23*300**2]
    assert host.volume_mm3 == 47*300**2*100
    assert len(host.sections) == 2 and len(host.sections[0].footprint.interiors) == 1
    assert outside_box_volume_mm3(host, (50, 50, 25), (250, 250, 175)) == pytest.approx(0)
    assert outside_box_volume_mm3(host, (610, 610, 25), (620, 620, 175)) == pytest.approx(15000)
    assert outside_box_volume_mm3(host, (1250, 650, 50), (1350, 750, 150)) == pytest.approx(500000)
    assert outside_box_volume_mm3(host, (50, 50, -20), (250, 250, 220)) == pytest.approx(1600000)
    assert not result["placement_eligible"] and "DXF-to-host-binding" in result["not_checked"]


def test_box_volume_matches_independent_voxel_intersection_oracle():
    import random
    host, _ = inspect_working_solid(stepped_snapshot())
    rng = random.Random(19)
    voxels = [(x*300, y*300, z*100) for x in range(5) for y in range(5) for z in range(2)
              if (x, y) != (2, 2) and (x, y, z) != (4, 2, 1)]
    for _ in range(100):
        lo = [rng.uniform(-200, 1500), rng.uniform(-200, 1500), rng.uniform(-100, 200)]
        hi = [a + rng.uniform(1, width) for a, width in zip(lo, (500, 500, 300))]
        volume = math.prod(b-a for a, b in zip(lo, hi))
        inside = sum(math.prod(max(0, min(b, v+s) - max(a, v))
                              for a, b, v, s in zip(lo, hi, voxel, (300, 300, 100))) for voxel in voxels)
        assert outside_box_volume_mm3(host, lo, hi) == pytest.approx(volume-inside, abs=0.00001)


def test_edge_order_and_winding_do_not_define_material():
    report = stepped_snapshot()
    for face in report["host_solid"]["faces"]:
        for ring in face["edge_loops"]:
            ring.reverse()
            for edge in ring[::2]:
                edge["start_mm"], edge["end_mm"] = edge["end_mm"], edge["start_mm"]
    assert inspect_working_solid(report)[0].volume_mm3 == 423000000


@pytest.mark.parametrize("bad", ["missing_wall", "duplicate_wall", "opposite_normal", "missing_top", "missing_step",
    "volume", "slope", "arc", "edge_length", "open_loop", "bbox", "floor_face", "read_issues", "host_id",
    "rollback", "cover", "nan", "null_face", "null_edge"])
def test_corrupt_incomplete_or_unsupported_solid_never_becomes_bbox(bad):
    report = stepped_snapshot()
    solid, floor = report["host_solid"], report["host"]
    wall = next(f for f in solid["faces"] if f["plane"]["normal"][0] == -1)
    if bad == "missing_wall":
        solid["faces"].remove(wall)
    elif bad == "duplicate_wall":
        solid["faces"].append(deepcopy(wall))
    elif bad == "opposite_normal":
        wall["plane"]["normal"][0] = 1
    elif bad in ("missing_top", "missing_step"):
        elevation = 200 if bad == "missing_top" else 100
        solid["faces"].remove(next(f for f in solid["faces"] if f["plane"]["normal"] == [0, 0, 1]
                                  and f["plane"]["origin_mm"][2] == elevation))
    elif bad == "volume":
        solid["volume_mm3"] += 1000
    elif bad == "slope":
        wall["plane"]["normal"] = [0.6, 0, 0.8]
    elif bad == "arc":
        wall["edge_loops"][0][0]["kind"] = "Arc"
    elif bad == "edge_length":
        wall["edge_loops"][0][0]["length_mm"] += 1
    elif bad == "open_loop":
        wall["edge_loops"][0].pop()
    elif bad == "bbox":
        floor["bbox_mm"]["max_mm"][0] += 1
    elif bad == "floor_face":
        floor["top_faces"].pop()
    elif bad == "read_issues":
        report["read_issues"] = ["unreadable geometry"]
    elif bad == "host_id":
        report["host_id"] = 43
    elif bad == "rollback":
        report["status"] = "passed_rolled_back"
    elif bad == "cover":
        floor["covers"]["top"]["distance_mm"] = -1
    elif bad == "null_face":
        solid["faces"][0] = None
    elif bad == "null_edge":
        wall["edge_loops"][0][0] = None
    else:
        wall["plane"]["origin_mm"][0] = float("nan")
    with pytest.raises(ValueError):
        inspect_working_solid(report)


def test_no_implicit_depths_and_explicit_translation_does_not_mutate_packet():
    report, packet = stepped_snapshot(), geometry_packet()
    before = deepcopy(packet)
    result = review_working_solid_bars(report, packet, offset_x_mm=0, offset_y_mm=0, binding_source="test hypothesis")
    assert result["status"] == "axis_depths_required"
    assert result["bar_check"]["totals"]["outside_solid_with_cover"] is None
    assert not result["bar_check"]["packet_source_certificates_checked"]
    assert not result["binding"]["live_binding_verified"] and packet == before
    depths = dict.fromkeys((d["direction"] for d in packet["directions"]), 50)
    contained = review_working_solid_bars(report, packet, offset_x_mm=0, offset_y_mm=0,
                                        binding_source="test hypothesis", axis_depths_mm=depths)
    assert contained["status"] == "geometry_contained_under_explicit_profile"
    assert contained["bar_check"]["totals"]["outside_solid_with_cover"] == 0
    assert not contained["placement_eligible"]
    depths["top-X"] = 5
    failed = review_working_solid_bars(report, packet, offset_x_mm=0, offset_y_mm=0,
                                     binding_source="test hypothesis", axis_depths_mm=depths)
    assert failed["bar_check"]["totals"]["outside_solid_with_cover"] == 1
    shifted = review_working_solid_bars(report, packet, offset_x_mm=1500, offset_y_mm=0,
                                      binding_source="test hypothesis")
    assert shifted["bar_check"]["totals"]["outside_even_in_xy_projection"] == 4
    assert packet == before


def test_xy_projection_does_not_hide_half_height_recess():
    packet = geometry_packet()
    for direction in packet["directions"]:
        run = direction["runs"][0]
        run["start_xy_mm"] = [1250, 650]
        run["end_xy_mm"] = [1350, 650] if direction["direction"].endswith("X") else [1250, 750]
    result = review_working_solid_bars(stepped_snapshot(), packet, offset_x_mm=0, offset_y_mm=0,
                                     binding_source="test", axis_depths_mm=dict.fromkeys(
                                         (d["direction"] for d in packet["directions"]), 50))
    assert result["bar_check"]["totals"]["outside_even_in_xy_projection"] == 0
    assert result["bar_check"]["totals"]["outside_full_height_common_footprint"] == 4
    assert result["bar_check"]["totals"]["outside_solid_with_cover"] == 2


@pytest.mark.parametrize("bad", ["directions", "axes", "count", "zero", "diameter", "nan", "depth", "source"])
def test_bad_physical_geometry_fails_without_truncation(bad):
    packet = geometry_packet()
    kwargs = {"offset_x_mm": 0, "offset_y_mm": 0, "binding_source": "test"}
    run = packet["directions"][0]["runs"][0]
    if bad == "directions":
        packet["directions"].pop()
    elif bad == "axes":
        run["end_xy_mm"][1] += 100
    elif bad == "count":
        packet["expected"]["physical_bar_count"] = 5
    elif bad == "zero":
        run["bar_count"] = 0
    elif bad == "diameter":
        run["diameter_mm"] = 0
    elif bad == "nan":
        kwargs["offset_x_mm"] = float("nan")
    elif bad == "depth":
        kwargs["axis_depths_mm"] = {"top-X": 50}
    else:
        kwargs["binding_source"] = ""
    with pytest.raises(ValueError):
        review_working_solid_bars(stepped_snapshot(), packet, **kwargs)


def test_real_private_working_floor_supports_47_holes_and_intermediate_ledge():
    path = Path(__file__).resolve().parents[2] / "revit_info/060_notest/qmonitoring-full-plate-20260914-102249-013000.json"
    if not path.exists():
        pytest.skip("private Revit host snapshot not available")
    host, result = inspect_working_solid(json.loads(path.read_bytes()))
    assert host.face_count == 241 and len(host.sections) == 2
    assert [s["opening_count"] for s in result["geometry"]["sections"]] == [47, 47]
    assert [s.footprint.area for s in host.sections] == pytest.approx([305660300, 305606300])
    assert [s.bottom_z_mm for s in host.sections] == [6910, 7010]
    assert host.volume_mm3 == pytest.approx(61126660000)
    assert result["host_id"] == 11020633
    assert (host.top_cover_mm, host.bottom_cover_mm, host.side_cover_mm) == (40, 40, 25)


def test_geometry_cli_hashes_input_and_never_overwrites(tmp_path, monkeypatch):
    source, output = tmp_path / "host.json", tmp_path / "check.json"
    raw = json.dumps(stepped_snapshot()).encode()
    source.write_bytes(raw)
    script = Path(__file__).resolve().parents[2] / "scripts/check_working_revit_host.py"
    monkeypatch.setattr(sys, "argv", [str(script), "--report", str(source), "--output", str(output)])
    runpy.run_path(str(script), run_name="__main__")
    saved = output.read_bytes()
    assert json.loads(saved)["source_report_sha256"] == hashlib.sha256(raw).hexdigest()
    with pytest.raises(SystemExit):
        runpy.run_path(str(script), run_name="__main__")
    assert output.read_bytes() == saved and source.read_bytes() == raw


def test_physical_cli_validates_certificates_and_matches_native_envelopes(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "integrations/pyrevit/QMonitoring.extension/lib"))
    monkeypatch.syspath_prepend(str(root / "tests/integrations"))
    from test_physical_plan_trial import small_physical_packet
    from qm_physical_packet import private_execution_packet
    from qm_plate_packet import make_plan
    source, packet_path, output = (tmp_path / name for name in ("host.json", "physical.json", "check.json"))
    report, packet = stepped_snapshot(), small_physical_packet()
    source.write_text(json.dumps(report))
    raw = json.dumps(packet).encode()
    packet_path.write_bytes(raw)
    script = root / "scripts/check_working_revit_host.py"
    monkeypatch.setattr(sys, "argv", [str(script), "--report", str(source), "--packet", str(packet_path),
        "--offset-x-mm", "12", "--offset-y-mm", "-30", "--binding-source", "offline test",
        "--axis-depths-mm", "50", "80", "50", "80", "--output", str(output)])
    runpy.run_path(str(script), run_name="__main__")
    result = json.loads(output.read_bytes())
    assert result["bar_check"]["packet_source_certificates_checked"]
    assert result["packet_sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["bar_check"]["totals"]["physical_bar_count"] == 11
    plan = make_plan(private_execution_packet(packet), report["host"],
        {"A500|18": {"element_id": 123, "nominal_diameter_mm": 18, "model_diameter_mm": 18}},
        {"offset_x_mm": 12, "offset_y_mm": -30, "axis_depths_mm": result["binding"]["axis_depths_mm"], "confirmed": True})
    for actual, expected in zip(result["bar_check"]["bars"], plan["bars"], strict=True):
        for key, sign in (("min_mm", -1), ("max_mm", 1)):
            expected_envelope = [v + sign*25 for v in expected["body_bbox_mm"][key]]
            assert actual["envelope_mm"][key] == pytest.approx(expected_envelope)
    packet["source_zones"][0]["components"][0]["required_interval_mm"][0] = -9999
    packet_path.write_text(json.dumps(packet))
    output_before = output.read_bytes()
    with pytest.raises(SystemExit):
        runpy.run_path(str(script), run_name="__main__")
    assert output.read_bytes() == output_before


def working_probe_snapshot():
    return {**stepped_snapshot(), "schema_version": "revit-working-host-cad-probe/v1",
            "status": "collected", "read_only": True, "engineering_approval": False,
            "host_read_issues": [], "document": {"is_modified_before": False, "is_modified_after": False}}


@pytest.mark.parametrize("cad_partial", [False, True])
def test_new_read_only_probe_host_independent_of_unverified_cad(cad_partial):
    report = working_probe_snapshot()
    if cad_partial:
        report.update(status="partial", read_issues=[{"section": "cad", "message": "layer unresolved"}])
    _, result = inspect_working_solid(report)
    assert result["source_snapshot_status"] == report["status"]
    assert result["source_read_issues"] == report["read_issues"]
    assert result["status"] == "geometry_checked_placement_not_checked"
    assert not result["placement_eligible"]


@pytest.mark.parametrize("bad", ["read_only", "approved", "host_error", "modified", "unknown_state", "host_id", "unknown_status"])
def test_read_only_probe_host_requires_unchanged_model_and_complete_host_read(bad):
    report = working_probe_snapshot()
    if bad == "read_only":
        report["read_only"] = False
    elif bad == "approved":
        report["engineering_approval"] = True
    elif bad == "host_error":
        report["host_read_issues"] = ["missing face"]
    elif bad == "modified":
        report["document"]["is_modified_after"] = True
    elif bad == "unknown_state":
        report["document"]["is_modified_before"] = None
    elif bad == "host_id":
        report["host_id"] = True
    else:
        report["status"] = "approved"
    with pytest.raises(ValueError):
        inspect_working_solid(report)


def test_large_host_plus_two_cad_readings_has_explicit_bounded_loader():
    raw = json.dumps({"payload": "x" * (9*1024*1024)}).encode()
    with pytest.raises(ValueError):
        load_working_host_json(raw)
    assert len(load_working_host_json(raw, maximum_bytes=32*1024*1024)["payload"]) == 9*1024*1024
    with pytest.raises(ValueError):
        load_working_host_json(raw, maximum_bytes=33*1024*1024)
