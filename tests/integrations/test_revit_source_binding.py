"""Source-authoritative snapshot checks, independent of actual Autodesk availability."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from rebar.application.revit_dxf_calibration import verify_dxf_calibration
from rebar.application.revit_source_binding import (
    COMPARISON_ISSUE, COORDINATES, INSTANCE_METHOD, NO_TRUSTED_MESH, SOURCE_BINDING_POLICY,
    SYMBOL_METHOD, UNKNOWN_LAYERS, verify_source_binding,
)
from rebar.models import Band, Rebar, ReinforcementRecipe
from test_revit_dxf_calibration import mosaic_sample, report_sample

ROOT = Path(__file__).resolve().parents[2]


def source_sample():
    mosaic = mosaic_sample()
    recipe = ReinforcementRecipe(Rebar(300, 18), (Rebar(150, 18), Rebar(300, 25)))
    band = Band(0, 1, "s300d18+s150d18+s300d25", 42, recipe.background, None, recipe)
    mosaic.legend = [band]
    for cell in mosaic.cells:
        cell.band = band
    return mosaic


def snapshot_report(mosaic=None, *, copies=2, solids=True, angle=0, origin=(0, 0, 0), mixed_diagonals=False):
    mosaic = mosaic or source_sample()
    report = report_sample(mosaic, angle=angle, origin=origin)
    triangles = report["cad_geometry"]["triangles_mm"]
    report.update(probe_version="0.5.1", units="mm", coordinate_system=COORDINATES)
    report["cad"]["linked_file_status"] = "Loaded"
    for key, method in (("cad_geometry", SYMBOL_METHOD), ("cad_geometry_instance", INSTANCE_METHOD)):
        records, meshes, issues = [], [], []
        for index in range(copies):
            row = {"path": f"/0/{index}", "kind": "Mesh", "layer": None, "graphics_style_id": -1}
            records.append(row)
            payload = triangles
            if index == 1 and mixed_diagonals:
                payload = report_sample(mosaic, angle=angle, origin=origin, other_diagonal=True)["cad_geometry"]["triangles_mm"]
            meshes.append({**row, "read_status": "collected", "triangles_mm": copy.deepcopy(payload),
                           "declared_triangle_count": len(payload), "triangle_count": len(payload),
                           "trusted_triangle_range": None})
        if solids:
            path = "/0/empty"
            records.append({"path": path, "kind": "Solid", "layer": None})
            issues.append({"stage": "cad_geometry", "path": path, "message": "Empty CAD solid"})
        issues.extend({"stage": "cad_geometry", "path": "/", "message": m} for m in (UNKNOWN_LAYERS, NO_TRUSTED_MESH))
        objects = [{"kind": "Mesh", "layer": "<unresolved>", "count": copies}]
        if solids:
            objects.append({"kind": "Solid", "layer": "<unresolved>", "count": 1})
        report[key] = {"method": method, "status": "partial", "units": "mm", "coordinate_system": COORDINATES,
            "placement_eligible": False, "triangles_mm": [], "triangle_count": 0, "meshes": meshes,
            "object_records": records, "objects": objects, "visited_object_count": len(records),
            "all_mesh_triangle_count": len(triangles)*copies, "unresolved_mesh_count": copies,
            "layer_resolution_complete": False, "mesh_read_complete": not solids, "limit_exceeded": None,
            "issues": issues, "issue_count": len(issues)}
    report["issues"] = copy.deepcopy(report["cad_geometry"]["issues"])
    comparison = {"status": "not_checked" if solids else "matches",
                  "placement_eligible": False, "layer_identity_verified": False}
    if solids:
        comparison["reason"] = "Mesh reading incomplete or coordinate systems differ"
        report["issues"].append({"stage": "cad_geometry_comparison", "message": COMPARISON_ISSUE})
    report.update(status="partial", cad_geometry_comparison=comparison)
    return report


def check(mosaic, report, **kwargs):
    defaults = dict(policy_id=SOURCE_BINDING_POLICY, expected_mosaic_copies=2,
                    source_sha256="a"*64, shk_sha256="b"*64, report_sha256="c"*64)
    return verify_source_binding(mosaic, report, **{**defaults, **kwargs})


def recount(geometry):
    for mesh in geometry["meshes"]:
        mesh["triangle_count"] = mesh["declared_triangle_count"] = len(mesh["triangles_mm"])
    geometry["all_mesh_triangle_count"] = sum(m["triangle_count"] for m in geometry["meshes"])


@pytest.mark.parametrize("copies", [1, 2])
@pytest.mark.parametrize("solids", [False, True])
@pytest.mark.parametrize("angle,origin", [(0, (0, 0, 0)), (math.pi/2, (5000, -3000, 8970))])
def test_explicit_snapshot_binding_preserves_unknowns_and_composite_recipe(copies, solids, angle, origin):
    mosaic = source_sample()
    report = snapshot_report(mosaic, copies=copies, solids=solids, angle=angle, origin=origin)
    original_mosaic, original_report = copy.deepcopy(mosaic), copy.deepcopy(report)
    result = check(mosaic, report, expected_mosaic_copies=copies)
    assert result["status"] == "source_snapshot_matches", result["issues"]
    assert result["geometry"]["source_cell_count"] == 3
    assert result["geometry"]["matched_source_vertex_count"] == result["geometry"]["source_vertex_count"]
    assert result["geometry"]["mismatched_cell_count"] == 0
    assert result["binding_candidate"]["recorded_transform"] == report["cad"]["instance_transform"]
    assert result["original_probe"]["status"] == "partial"
    assert result["original_probe"]["issues"] == report["issues"]
    assert len(result["demand_source"]["levels"][0]["recipe"]["additions"]) == 2
    assert result["demand_source"]["cells_by_level"] == {"0": 3}
    for flag in ("placement_eligible", "cad_layer_identity_verified", "cached_cad_colours_verified",
                 "complete_cad_geometry_verified", "live_binding_verified"):
        assert result[flag] is False
    assert "host-cover-openings-containment" in result["remaining_check_ids"]
    assert report == original_report and mosaic == original_mosaic
    assert verify_dxf_calibration(mosaic, report, source_sha256="a"*64)["status"] == "blocked"


def test_two_copies_can_use_different_valid_quad_diagonals_and_mesh_order():
    mosaic = source_sample()
    report = snapshot_report(mosaic, mixed_diagonals=True)
    report["cad_geometry_instance"]["meshes"].reverse()
    for mesh in report["cad_geometry_instance"]["meshes"]:
        mesh["triangles_mm"].reverse()
        for triangle in mesh["triangles_mm"]:
            triangle.reverse()
    assert check(mosaic, report)["status"] == "source_snapshot_matches"


@pytest.mark.parametrize("damage", ["missing", "duplicate", "mixed_cell", "wrong_diagonal", "shift", "scale", "z"])
def test_every_triangle_and_multiplicity_checked_even_when_both_api_reads_agree(damage):
    mosaic = source_sample()
    report = snapshot_report(mosaic)
    for key in ("cad_geometry", "cad_geometry_instance"):
        geometry = report[key]
        triangles = geometry["meshes"][0]["triangles_mm"]
        if damage == "missing":
            triangles.pop(0)
        elif damage == "duplicate":
            triangles.append(copy.deepcopy(triangles[0]))
        elif damage == "mixed_cell":
            triangles[0][1] = copy.deepcopy(triangles[2][1])
        elif damage == "wrong_diagonal":
            triangles[1] = [triangles[0][0], triangles[0][1], triangles[1][2]]
        else:
            for triangle in triangles:
                for point in triangle:
                    if damage == "shift":
                        point[0] += 1
                    elif damage == "scale":
                        point[0] *= 1000
                    else:
                        point[2] = -34
        recount(geometry)
    result = check(mosaic, report)
    assert result["status"] == "blocked"
    assert result["api_snapshot_agreement"]["status"] == "matches"
    assert result["geometry"]["status"] == "differs"
    assert result["issues"] and not result["placement_eligible"]
    assert "binding_candidate" not in result


def test_source_tolerance_does_not_hide_differences_between_api_snapshots():
    mosaic, report = source_sample(), snapshot_report()
    report["cad_geometry_instance"]["meshes"][0]["triangles_mm"][0][0][0] += 0.001
    result = check(mosaic, report)
    assert result["status"] == "blocked"
    assert result["api_snapshot_agreement"]["status"] == "differs"
    for geometry in (report["cad_geometry"], report["cad_geometry_instance"]):
        for mesh in geometry["meshes"]:
            for triangle in mesh["triangles_mm"]:
                for point in triangle:
                    point[0] = round(point[0]) + 0.001
    result = check(mosaic, report)
    assert result["status"] == "source_snapshot_matches"
    assert result["geometry"]["max_matched_vertex_error_mm"] == pytest.approx(0.001)


@pytest.mark.parametrize("copies", [None, True, 2.0, 0, 1, 3])
def test_multiplicity_must_be_explicit_correct_integer(copies):
    assert check(source_sample(), snapshot_report(), expected_mosaic_copies=copies)["status"] == "blocked"


@pytest.mark.parametrize("kwargs", [dict(policy_id=None), dict(policy_id="fallback"), dict(source_sha256="b"*64),
    dict(source_sha256=None), dict(shk_sha256="A"*64), dict(report_sha256="short")])
def test_no_implicit_policy_or_missing_source_identity(kwargs):
    assert check(source_sample(), snapshot_report(), **kwargs)["status"] == "blocked"


@pytest.mark.parametrize("damage", ["old_version", "not_linked", "unloaded", "unknown_hash", "modified", "modified_bool",
    "approve", "root_units", "root_coordinates", "wrong_schema", "identity", "same_identity", "reflection", "tilt",
    "unknown_report_issue", "missing_inventory", "bool_count", "count_mismatch", "limit", "mesh_partial", "mesh_missing",
    "mesh_path_duplicate", "object_path_duplicate", "unexpected_object", "unknown_geometry_issue", "truncated_issues",
    "unexplained_incomplete", "unexpected_solid", "comparison_differs", "comparison_limit", "comparison_approval",
    "wrong_method", "wrong_coordinates", "unresolved_count", "layer_flag", "nan", "bool_vertex", "short_triangle"])
def test_incomplete_or_ambiguous_reports_fail_closed(damage):
    mosaic, report = source_sample(), snapshot_report()
    g = report["cad_geometry"]
    if damage == "old_version":
        report["probe_version"] = "0.5.0"
    elif damage == "not_linked":
        report["cad"]["is_linked"] = False
    elif damage == "unloaded":
        report["cad"]["linked_file_status"] = "NotFound"
    elif damage == "unknown_hash":
        report["linked_source_file"]["status"] = "not_checked"
    elif damage in ("modified", "modified_bool"):
        report["document"]["is_modified_after"] = True if damage == "modified" else 0
    elif damage == "approve":
        report["placement_eligible"] = True
    elif damage == "root_units":
        report["units"] = "feet"
    elif damage == "root_coordinates":
        report["coordinate_system"] = "project-base-point"
    elif damage == "wrong_schema":
        report["schema_version"] = "revit-reference-probe/v1"
    elif damage == "identity":
        report["cad"]["unique_id"] = ""
    elif damage == "same_identity":
        report["cad"]["element_id"] = report["floor"]["element_id"]
    elif damage == "reflection":
        report["cad"]["instance_transform"]["basis_x"][0] = -1
    elif damage == "tilt":
        report["cad"]["instance_transform"]["basis_x"][2] = 0.01
    elif damage == "unknown_report_issue":
        report["issues"].append({"stage": "floor", "message": "Failed to read host"})
    elif damage == "missing_inventory":
        g["objects"] = []
    elif damage == "bool_count":
        g["meshes"][0]["declared_triangle_count"] = True
    elif damage == "count_mismatch":
        g["all_mesh_triangle_count"] += 1
    elif damage == "limit":
        g["limit_exceeded"] = "CAD triangle limit exceeded"
    elif damage == "mesh_partial":
        g["meshes"][0]["read_status"] = "partial"
    elif damage == "mesh_missing":
        g["meshes"].pop()
        recount(g)
    elif damage == "mesh_path_duplicate":
        g["meshes"][1]["path"] = g["meshes"][0]["path"]
    elif damage == "object_path_duplicate":
        g["object_records"][1]["path"] = g["object_records"][0]["path"]
    elif damage == "unexpected_object":
        g["object_records"][0]["kind"] = "Face"
    elif damage == "unknown_geometry_issue":
        g["issues"][0]["message"] = "Failed to read triangles"
    elif damage == "truncated_issues":
        g["issue_count"] += 1
    elif damage == "unexplained_incomplete":
        g["mesh_read_complete"] = True
    elif damage == "unexpected_solid":
        g["object_records"][-1]["kind"] = "PolyLine"
    elif damage == "comparison_differs":
        report["cad_geometry_comparison"]["status"] = "differs"
    elif damage == "comparison_limit":
        report["cad_geometry_comparison"]["reason"] = "Comparison limit exceeded"
    elif damage == "comparison_approval":
        report["cad_geometry_comparison"]["layer_identity_verified"] = True
    elif damage == "wrong_method":
        report["cad_geometry_instance"]["method"] = SYMBOL_METHOD
    elif damage == "wrong_coordinates":
        report["cad_geometry_instance"]["coordinate_system"] = "local"
    elif damage == "unresolved_count":
        g["unresolved_mesh_count"] = 0
    elif damage == "layer_flag":
        g["layer_resolution_complete"] = True
    elif damage in ("nan", "bool_vertex"):
        g["meshes"][0]["triangles_mm"][0][0][0] = float("nan") if damage == "nan" else True
    else:
        g["meshes"][0]["triangles_mm"][0].pop()
    result = check(mosaic, report)
    assert result["status"] == "blocked", damage
    assert result["issues"] and not result["placement_eligible"]


@pytest.mark.parametrize("damage", ["legend", "source_units", "non_planar", "duplicate_cell", "unknown_aci"])
def test_incomplete_source_is_not_replaced_by_cached_geometry(damage):
    mosaic, report = source_sample(), snapshot_report()
    if damage == "legend":
        mosaic.legend = []
    elif damage == "source_units":
        mosaic.meta["units"] = "m"
    elif damage == "non_planar":
        mosaic.meta["z_range_mm"] = [-8970, -34]
    elif damage == "duplicate_cell":
        mosaic.cells.append(copy.deepcopy(mosaic.cells[0]))
    else:
        mosaic.cells[0].aci = 255
    assert check(mosaic, report)["status"] == "blocked"


def test_triangle_resource_limit_is_not_partial_success(monkeypatch):
    import rebar.application.revit_source_binding as module
    monkeypatch.setattr(module, "MAX_MESH_TRIANGLES", 1)
    assert check(source_sample(), snapshot_report())["status"] == "blocked"


def test_trusted_triangle_ranges_are_read_once_without_changing_layer_identity():
    mosaic, report = source_sample(), snapshot_report(solids=False)
    for key in ("cad_geometry", "cad_geometry_instance"):
        g = report[key]
        mesh = g["meshes"][0]
        g["triangles_mm"] = mesh["triangles_mm"]
        g["triangle_count"] = len(g["triangles_mm"])
        mesh.update(layer="KLEENKA", triangles_mm=[], trusted_triangle_range=[0, g["triangle_count"]])
        g["object_records"][0]["layer"] = "KLEENKA"
        g["objects"] = [{"kind": "Mesh", "layer": "KLEENKA", "count": 1},
                        {"kind": "Mesh", "layer": "<unresolved>", "count": 1}]
        g["unresolved_mesh_count"] = 1
        g["issues"] = [row for row in g["issues"] if row["message"] != NO_TRUSTED_MESH]
        g["issue_count"] = len(g["issues"])
    report["issues"] = copy.deepcopy(report["cad_geometry"]["issues"])
    assert check(mosaic, report)["status"] == "source_snapshot_matches"
    report["cad_geometry"]["meshes"][0]["trusted_triangle_range"] = [0, 1]
    assert check(mosaic, report)["status"] == "blocked"


@pytest.mark.parametrize("failure", [None, "wrong_copies", "duplicate_key", "NaN", "Infinity", "root_list", "missing_policy"])
def test_cli_roundtrip_preserves_all_source_hashes_and_never_grants_apply(tmp_path, failure):
    from rebar.application.demo import IRREGULAR_PLATE_DEMO, write_demo_dxf
    from rebar.dxf_ingest import read_mosaic
    dxf = tmp_path / IRREGULAR_PLATE_DEMO.filename
    write_demo_dxf(IRREGULAR_PLATE_DEMO.id, dxf)
    shk = tmp_path / "synthetic.shk"
    labels = [b"s300d12"] + [("s300d12+s300d%d" % d).encode() for d in (12, 14, 16, 18, 20)]
    shk.write_bytes(b"".join(struct.pack("<ffHB", i+1, i+2, i, len(label)) + label for i, label in enumerate(labels)))
    report = snapshot_report(read_mosaic(str(dxf), str(shk)))
    report["linked_source_file"]["sha256"] = hashlib.sha256(dxf.read_bytes()).hexdigest()
    raw = json.dumps(report, ensure_ascii=False)
    if failure == "duplicate_key":
        raw = '{"x": 1, "x": 2}'
    elif failure in ("NaN", "Infinity"):
        raw = '{"x": ' + failure + '}'
    elif failure == "root_list":
        raw = "[]"
    path = tmp_path / "cad-report.json"
    path.write_text("\ufeff" + raw, encoding="utf-8")
    output = tmp_path / "source-binding.json"
    before = {p: p.read_bytes() for p in (dxf, shk, path)}
    cmd = [sys.executable, str(ROOT / "scripts/verify_revit_source_binding.py"),
           "--dxf", str(dxf), "--shk", str(shk), "--report", str(path), "--output", str(output),
           "--expected-mosaic-copies", "1" if failure == "wrong_copies" else "2"]
    if failure != "missing_policy":
        cmd.extend(["--policy", SOURCE_BINDING_POLICY])
    run = subprocess.run(cmd, capture_output=True, text=True)
    assert all(p.read_bytes() == value for p, value in before.items())
    if failure in (None, "wrong_copies"):
        assert run.returncode == (0 if failure is None else 1), run.stderr
        result = json.loads(output.read_text())
        assert result["status"] == ("source_snapshot_matches" if failure is None else "blocked")
        assert result["source_sha256"] == {k: hashlib.sha256(before[p]).hexdigest()
                                          for k, p in zip(("dxf", "shk", "report"), before)}
        assert result["inputs_unchanged"] and not result["placement_eligible"]
    else:
        assert run.returncode == 2 and not output.exists()


@pytest.mark.parametrize("target", ["existing.json", "model.rvt"])
def test_cli_refuses_overwrite_and_non_json_output_before_inputs(tmp_path, target):
    path = tmp_path / target
    path.write_bytes(b"keep")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_revit_source_binding.py"),
        "--dxf", "missing.dxf", "--shk", "missing.shk", "--report", "missing.json", "--output", str(path),
        "--policy", SOURCE_BINDING_POLICY, "--expected-mosaic-copies", "2"], capture_output=True, text=True)
    assert result.returncode == 2 and "NEW .json" in result.stderr
    assert path.read_bytes() == b"keep"


def test_actual_051_snapshot_matches_but_strict_checker_remains_blocked(two_background_top_x_sources):
    from rebar.dxf_ingest import read_mosaic
    path = ROOT / "revit_info/051/qmonitoring-cad-20260910-095834-615000.json"
    if not path.is_file():
        pytest.skip("Private real Revit report is not distributed")
    dxf, shk = two_background_top_x_sources
    before = {p: p.read_bytes() for p in (dxf, shk, path)}
    mosaic, report = read_mosaic(str(dxf), str(shk)), json.loads(before[path])
    result = check(mosaic, report, source_sha256=hashlib.sha256(before[dxf]).hexdigest(),
                   shk_sha256=hashlib.sha256(before[shk]).hexdigest(), report_sha256=hashlib.sha256(before[path]).hexdigest())
    assert result["status"] == "source_snapshot_matches", result["issues"]
    assert result["geometry"]["source_cell_count"] == 2132
    assert result["geometry"]["source_vertex_count"] == 2209
    assert result["geometry"]["actual_triangle_count"] == 8450
    assert result["geometry"]["max_matched_vertex_error_mm"] == pytest.approx(0.0012847201)
    assert result["mesh_snapshots"]["symbol"]["unresolved_mesh_count"] == 5
    assert result["demand_source"]["cells_by_level"] == {"0": 791, "1": 788, "2": 388, "3": 165}
    assert result["binding_candidate"]["cad"]["element_id"] == 407861
    assert result["binding_candidate"]["host"]["element_id"] == 407801
    assert verify_dxf_calibration(mosaic, report)["status"] == "blocked"
    assert all(p.read_bytes() == value for p, value in before.items())
