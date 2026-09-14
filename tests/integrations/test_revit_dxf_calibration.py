"""Offline source/mesh checks. Synthetic reports are NOT actual Revit runs."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
import subprocess
import struct
import sys
from pathlib import Path

import pytest

from rebar.application.revit_dxf_calibration import verify_dxf_calibration
from rebar.models import Axis, Cell, Direction, Layer, Mosaic

ROOT = Path(__file__).resolve().parents[2]


def matrix(angle=0, origin=(0, 0, 0)):
    c, s = math.cos(angle), math.sin(angle)
    return {"origin_mm": list(origin), "basis_x": [c, s, 0], "basis_y": [-s, c, 0],
            "basis_z": [0, 0, 1], "determinant": 1, "is_conformal": True, "basis_units": "dimensionless"}


def transform(p, m):
    return [m["origin_mm"][i] + sum(p[j] * m["basis_"+a][i] for j, a in enumerate("xyz")) for i in range(3)]


def mosaic_sample():
    polys = [[(0, 0), (1000, 0), (1000, 500), (0, 500)],
             [(1000, 0), (2100, 0), (2100, 500), (1000, 500)],
             [(0, 500), (1000, 500), (0, 1400)]]
    return Mosaic(Direction(Layer.TOP, Axis.X), [Cell(p, p[0], 1) for p in polys], [], (0, 0, 2100, 1400),
                  "synthetic-top-x.dxf", {"source_format": "dxf", "units": "mm", "z_range_mm": [-8970, -8970],
                  "unit_scale_to_mm": 1000, "unit_detection": "heuristic_edge_length"})


def report_sample(mosaic=None, *, angle=0, origin=(0, 0, 0), other_diagonal=False):
    mosaic = mosaic or mosaic_sample()
    m = matrix(angle, origin)
    triangles = []
    z = mosaic.meta["z_range_mm"][0]
    for cell in mosaic.cells:
        p = [transform((*xy, z), m) for xy in cell.poly]
        if len(p) == 3:
            triangles.append(p)
        elif other_diagonal:
            triangles.extend([[p[0], p[1], p[3]], [p[1], p[2], p[3]]])
        else:
            triangles.extend([[p[0], p[1], p[2]], [p[0], p[2], p[3]]])
    return {"schema_version": "revit-cad-calibration-probe/v1", "status": "collected", "issues": [],
            "read_only": True, "placement_eligible": False,
            "document": {"is_modified_before": False, "is_modified_after": False},
            "cad": {"unique_id": "cad-uid", "element_id": 123, "instance_transform": m,
                    "total_transform": matrix(0.9, (999, 999, 999)), "is_linked": True},
            "floor": {"unique_id": "floor-uid", "element_id": 407801, "bbox_mm": {
                "min_mm": [0, 0, -300], "max_mm": [2100, 1400, 0]}},
            "linked_source_file": {"status": "read", "sha256": "a"*64},
            "cad_geometry": {"status": "collected", "issues": [], "layer": "KLEENKA", "units": "mm",
                "method": "symbol-geometry-with-instance-transform-chain-once",
                "coordinate_system": "revit-internal-origin-and-axes", "triangle_count": len(triangles),
                "triangles_mm": copy.deepcopy(triangles)}}


@pytest.mark.parametrize("angle, origin", [(0, (0, 0, 0)), (math.pi/2, (5000, -2000, 9000)),
                                         (0.37, (22222, -33333, 100))])
@pytest.mark.parametrize("other_diagonal", [False, True])
def test_every_vertex_cell_and_control_point_matches_without_fitting(angle, origin, other_diagonal):
    report = report_sample(angle=angle, origin=origin, other_diagonal=other_diagonal)
    before = copy.deepcopy(report)
    result = verify_dxf_calibration(mosaic_sample(), report, source_sha256="a"*64)
    assert result["status"] == "geometry_matches" and not result["issues"]
    assert result["linked_disk_file"]["status"] == "matches"
    assert result["geometry"]["mismatched_cell_count"] == 0
    assert result["geometry"]["source_cell_count"] == 3
    assert result["geometry"]["max_matched_vertex_error_mm"] == 0
    assert len(result["geometry"]["control_points"]) == 3
    assert all(row["scale_ratio"] == pytest.approx(1) for row in result["geometry"]["control_lengths"])
    assert result["binding"]["candidate_transform"] == report["cad"]["instance_transform"]
    assert result["binding"]["extra_translation_mm"] == [0, 0, 0]
    assert not result["placement_eligible"] and not result["binding"]["placement_eligible"]
    assert report == before


@pytest.mark.parametrize("failure", ["missing", "duplicate", "wrong_diagonal", "mixed_cell", "shift",
                                    "scale1000", "source_z", "nan", "bool", "wrong_hash", "partial",
                                    "truncated", "count_bool", "modified", "unknown_layer", "wrong_units",
                                    "old_report", "reflected", "tilt", "shear", "matrix_scale", "missing_field"])
def test_bad_readback_never_becomes_a_calibrated_mapping(failure):
    report = report_sample()
    geometry = report["cad_geometry"]
    triangles = geometry["triangles_mm"]
    if failure == "missing":
        triangles.pop(0)
        geometry["triangle_count"] -= 1
    elif failure == "duplicate":
        triangles.append(copy.deepcopy(triangles[0]))
        geometry["triangle_count"] += 1
    elif failure == "wrong_diagonal":
        triangles[1] = [triangles[0][0], triangles[0][1], triangles[1][2]]
    elif failure == "mixed_cell":
        triangles[0][1] = triangles[2][1]
    elif failure in ("shift", "scale1000", "source_z"):
        for t in triangles:
            for p in t:
                if failure == "shift":
                    p[0] += 100
                elif failure == "scale1000":
                    p[0] *= 1000
                    p[1] *= 1000
                else:
                    p[2] = -34
    elif failure in ("nan", "bool"):
        triangles[0][0][0] = float("nan") if failure == "nan" else True
    elif failure == "wrong_hash":
        report["linked_source_file"]["sha256"] = "b"*64
    elif failure == "partial":
        geometry["status"] = "partial"
    elif failure == "truncated":
        geometry["triangle_count"] += 1
    elif failure == "count_bool":
        geometry["triangle_count"] = True
    elif failure == "modified":
        report["document"]["is_modified_after"] = True
    elif failure == "unknown_layer":
        geometry["layer"] = "PLAST"
    elif failure == "wrong_units":
        geometry["units"] = "feet"
    elif failure == "old_report":
        report["schema_version"] = "revit-reference-probe/v1"
    elif failure in ("reflected", "tilt", "shear", "matrix_scale"):
        m = report["cad"]["instance_transform"]
        if failure == "reflected":
            m["basis_x"][0] = -1
            m["determinant"] = -1
        elif failure == "tilt":
            m["basis_x"][2] = 1
        elif failure == "shear":
            m["basis_y"][0] = 0.1
        else:
            m["basis_x"][0] = 1000
    else:
        del geometry["triangles_mm"]
    result = verify_dxf_calibration(mosaic_sample(), report, source_sha256="a"*64)
    assert result["status"] == "blocked" and result["issues"]
    assert result["placement_eligible"] is False


def test_revit_float_precision_is_tolerated_but_lost_triangle_is_not():
    report = report_sample()
    for t in report["cad_geometry"]["triangles_mm"]:
        for p in t:
            p[0] += 0.001
            p[2] -= 0.0001556
    assert verify_dxf_calibration(mosaic_sample(), report)["status"] == "geometry_matches"
    report["cad_geometry"]["triangles_mm"].pop(1)
    report["cad_geometry"]["triangle_count"] -= 1
    result = verify_dxf_calibration(mosaic_sample(), report)
    assert result["status"] == "blocked"
    assert result["geometry"]["mismatched_cell_count"] == 1


@pytest.mark.parametrize("failure", ["no_cells", "non_planar", "duplicate", "concave", "units"])
def test_source_mesh_ambiguities_fail_closed(failure):
    mosaic = mosaic_sample()
    report = report_sample()
    if failure == "no_cells":
        mosaic.cells.clear()
    elif failure == "non_planar":
        mosaic.meta["z_range_mm"] = [-8970, 0]
    elif failure == "duplicate":
        mosaic.cells.append(copy.deepcopy(mosaic.cells[0]))
    elif failure == "concave":
        mosaic.cells[0].poly[2] = (100, 100)
    else:
        mosaic.meta["units"] = "m"
    assert verify_dxf_calibration(mosaic, report)["status"] == "blocked"


def test_imported_or_unavailable_file_does_not_gain_source_identity_approval():
    report = report_sample()
    report["cad"]["is_linked"] = False
    result = verify_dxf_calibration(mosaic_sample(), report, source_sha256="a"*64)
    assert result["status"] == "geometry_matches" and result["linked_disk_file"]["status"] == "not_checked"
    assert not result["placement_eligible"]
    assert result["host_xy_bbox"]["status"] == "matches"
    report["floor"]["bbox_mm"]["max_mm"][0] -= 100
    assert verify_dxf_calibration(mosaic_sample(), report)["host_xy_bbox"]["status"] == "differs"


def test_real_source_full_mesh_simulation_when_available():
    """Private source exercises ingest/matcher, NOT Autodesk or actual readback."""
    from rebar.dxf_ingest import read_mosaic

    folder = ROOT / "Дополнительные материалы/Изополя(мозаики) армирования/2 фона"
    dxf, shk = folder / "Верхнее армирование вдоль ОСИ Х.dxf", folder / "К09_фп_2 фона_Вх.shk"
    if not dxf.is_file() or not shk.is_file():
        pytest.skip("Private source DXF/SHK are not distributed")
    mosaic = read_mosaic(str(dxf), str(shk))
    assert len(mosaic.cells) == 2132
    assert mosaic.meta["unit_scale_to_mm"] == 1000
    result = verify_dxf_calibration(mosaic, report_sample(mosaic, other_diagonal=True))
    assert result["status"] == "geometry_matches", result["issues"]
    assert result["geometry"]["source_cell_count"] == 2132
    assert not result["placement_eligible"]


def test_040_core_report_independently_revalidated_when_available(monkeypatch):
    from rebar.application.core_revit_trial import make_core_revit_trial_sample

    path = ROOT / "revit_info/040/qmonitoring-core-trial-20260909-142843-698000.json"
    if not path.is_file():
        pytest.skip("Private real Revit Core Trial report is not distributed")
    monkeypatch.syspath_prepend(str(ROOT / "integrations/pyrevit/QMonitoring.extension/lib"))
    core = importlib.import_module("qm_core_trial")
    content = path.read_bytes()
    d = json.loads(content)
    assert d["input"] == make_core_revit_trial_sample()
    plan = core.make_core_plan(d["reference_before"]["floor"], d["readback"]["sets"][0]["bar_type"], d["input"])
    assert plan == d["plan"]
    assert d["status"] == "passed_rolled_back" and not d["issues"]
    assert d["commit"]["returned"] == d["commit"]["final"] == "Committed"
    assert d["group_rollback"]["returned"] == d["group_rollback"]["final"] == "RolledBack"
    assert d["readback"] == d["post_commit_readback"]
    assert core.compare_core_trial(plan, d["post_commit_readback"])["status"] == "matches"
    assert d["restoration"] == {"added_ids": [], "removed_ids": [], "reference_unchanged": True, "verified": True}
    assert path.read_bytes() == content


def test_cli_rejects_overwrite_before_reading_any_inputs(tmp_path):
    p = tmp_path / "keep.json"
    p.write_bytes(b"keep")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_revit_dxf_calibration.py"),
        "--dxf", "missing.dxf", "--shk", "missing.shk", "--report", "missing.json", "--output", str(p)],
        capture_output=True, text=True)
    assert result.returncode == 2 and "NEW .json" in result.stderr
    assert p.read_bytes() == b"keep"


@pytest.mark.parametrize("failure", [None, "lost_triangle", "duplicate_key", "NaN", "Infinity"])
def test_cli_public_ingest_roundtrip_and_negative_controls(tmp_path, failure):
    from rebar.application.demo import IRREGULAR_PLATE_DEMO, write_demo_dxf
    from rebar.dxf_ingest import read_mosaic

    dxf = tmp_path / IRREGULAR_PLATE_DEMO.filename
    write_demo_dxf(IRREGULAR_PLATE_DEMO.id, dxf)
    shk = tmp_path / "synthetic.shk"
    labels = [b"s300d12"] + [("s300d12+s300d%d" % d).encode() for d in (12, 14, 16, 18, 20)]
    shk.write_bytes(b"".join(struct.pack("<ffHB", i+1, i+2, i, len(label)) + label for i, label in enumerate(labels)))
    m = read_mosaic(str(dxf), str(shk))
    r = report_sample(m)
    r["linked_source_file"]["sha256"] = hashlib.sha256(dxf.read_bytes()).hexdigest()
    if failure == "lost_triangle":
        r["cad_geometry"]["triangles_mm"].pop()
        r["cad_geometry"]["triangle_count"] -= 1
    raw = json.dumps(r, ensure_ascii=False)
    if failure == "duplicate_key":
        raw = '{"x": 1, "x": 2}'
    elif failure in ("NaN", "Infinity"):
        raw = '{"x": ' + failure + '}'
    report = tmp_path / "cad-report.json"
    report.write_text(raw, encoding="utf-8")
    output = tmp_path / "calibration.json"
    before = {p: p.read_bytes() for p in (dxf, shk, report)}
    command = [sys.executable, str(ROOT / "scripts/verify_revit_dxf_calibration.py"),
        "--dxf", str(dxf), "--shk", str(shk), "--report", str(report), "--output", str(output)]
    run = subprocess.run(command, capture_output=True, text=True)
    assert all(p.read_bytes() == data for p, data in before.items())
    if failure in ("duplicate_key", "NaN", "Infinity"):
        assert run.returncode != 0 and not output.exists()
    else:
        assert run.returncode == (1 if failure else 0), run.stderr
        result = json.loads(output.read_text())
        assert result["status"] == ("blocked" if failure else "geometry_matches")
        assert result["source_report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
        assert result["source"]["sha256"] == hashlib.sha256(dxf.read_bytes()).hexdigest()
        assert result["geometry"]["source_cell_count"] == 96 and not result["placement_eligible"]
