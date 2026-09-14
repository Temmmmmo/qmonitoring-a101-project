"""0.5.1: unknown meshes remain evidence, never a substitute for CAD layer identity."""
from __future__ import annotations

import copy
import importlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.application.revit_dxf_calibration import verify_dxf_calibration

import test_revit_cad_probe
from test_revit_cad_probe import (  # noqa: F401 -- fixture registration
    CadTransform, GeometryInstance, Mesh, api, modules,
)
from test_revit_probe import Element, Id, XYZ
from test_revit_dxf_calibration import mosaic_sample, report_sample

ROOT = Path(__file__).resolve().parents[2]
cad_api = test_revit_cad_probe.cad_api  # register the shared injected API fixture


def diagnostics():
    return importlib.import_module("qm_cad_diagnostics")


def test_unknown_first_mesh_does_not_hide_later_meshes_or_gain_a_layer(cad_api):
    module, probe, cad, _, _, elements = cad_api
    unknown = Mesh(-1)
    unknown.MaterialElementId = Id(300)
    elements[300] = Element(300, "KLEENKA")
    elements[300].Color = SimpleNamespace(Red=255, Green=0, Blue=0)
    cad.get_Geometry = lambda options: [GeometryInstance([unknown, Mesh(), Mesh(101), Mesh(-1)])]
    r = module.read_cad_geometry(probe, cad)
    assert r["status"] == "partial" and r["mesh_read_complete"]
    assert not r["layer_resolution_complete"] and r["unresolved_mesh_count"] == 2
    assert r["all_mesh_triangle_count"] == 4 and r["triangle_count"] == 1
    assert len(list(diagnostics().all_triangles(r))) == 4
    assert r["meshes"][0]["material"] == {
        "element_id": 300, "status": "read", "name": "KLEENKA", "color_rgb": [255, 0, 0]}
    assert r["meshes"][0]["graphics_style_id"] == -1
    assert r["meshes"][0]["layer"] is None
    assert r["meshes"][1]["trusted_triangle_range"] == [0, 1]
    assert not r["meshes"][1]["triangles_mm"]  # trusted payload stored only once
    assert r["meshes"][2]["layer"] == "PLAST" and r["meshes"][2]["triangles_mm"]
    assert len(r["object_records"]) == 5


@pytest.mark.parametrize("kind", ["negative_valid", "missing", "throwing"])
def test_raw_style_resolution_is_explicit_and_failures_do_not_inherit(cad_api, kind):
    module, probe, cad, _, _, elements = cad_api
    mesh = Mesh(-200 if kind == "negative_valid" else 999)
    if kind == "negative_valid":
        elements[-200] = SimpleNamespace(GraphicsStyleCategory=Element(100, "KLEENKA"))
    elif kind == "throwing":
        elements[999] = SimpleNamespace()  # property read fails
    cad.get_Geometry = lambda options: [GeometryInstance([mesh], layer=100)]
    r = module.read_cad_geometry(probe, cad)
    row = r["meshes"][0]
    assert r["mesh_read_complete"] and r["all_mesh_triangle_count"] == 1
    assert row["graphics_style_id"] == (-200 if kind == "negative_valid" else 999)
    if kind == "negative_valid":
        assert r["status"] == "collected" and row["layer_source"] == "graphics_style"
    else:
        assert r["status"] == "partial" and row["layer"] is None
        assert row["graphics_style_status"] == ("not_found" if kind == "missing" else "read_error")


@pytest.mark.parametrize("broken", ["mesh", "instance", "nonfinite"])
def test_read_error_preserves_partial_evidence_and_continues_siblings(cad_api, broken):
    module, probe, cad, _, _, _ = cad_api
    bad = Mesh(-1, Mesh().triangles * 2)
    if broken == "instance":
        bad = GeometryInstance([])
        bad.GetSymbolGeometry = lambda: (_ for _ in ()).throw(RuntimeError("broken instance"))
    else:
        original = bad.get_Triangle

        def read(i):
            if i == 1:
                if broken == "nonfinite":
                    return SimpleNamespace(get_Vertex=lambda j: XYZ(float("nan"), 0, 0))
                raise RuntimeError("broken second triangle")
            return original(i)

        bad.get_Triangle = read
    cad.get_Geometry = lambda options: [bad, Mesh()]
    r = module.read_cad_geometry(probe, cad)
    assert r["status"] == "partial" and not r["mesh_read_complete"]
    assert r["triangle_count"] == 1 and r["visited_object_count"] == 2
    assert r["all_mesh_triangle_count"] == (1 if broken == "instance" else 2)
    if broken != "instance":
        assert r["meshes"][0]["triangle_count"] == 1 and r["meshes"][0]["read_status"] == "partial"
    json.dumps(r, allow_nan=False)  # no failed point poisons the diagnostic report


def test_face_failure_does_not_skip_other_faces(cad_api):
    module, probe, cad, DB, _, _ = cad_api

    class Face:
        GraphicsStyleId = Id(-1)
        MaterialElementId = Id(-1)

        def Triangulate(self):
            return Mesh()

    bad = Face()
    bad.Triangulate = lambda: (_ for _ in ()).throw(RuntimeError("cannot triangulate"))

    class Faces(list):
        Size = 2

    DB.PlanarFace = Face
    solid = DB.Solid()
    solid.GraphicsStyleId, solid.Faces = Id(100), Faces([bad, Face()])
    solid.GetType = lambda: SimpleNamespace(Name="Solid")
    cad.get_Geometry = lambda options: [solid]
    r = module.read_cad_geometry(probe, cad)
    assert r["status"] == "partial" and not r["mesh_read_complete"]
    assert r["triangle_count"] == 1 and r["visited_object_count"] == 3
    assert r["issues"][0]["path"] == "/0/face:0"


@pytest.mark.parametrize("origin,angle", [((0, 0, 0), 0), ((10, -20, 30), 0), ((10, 20, 30), math.pi/2)])
def test_two_methods_agree_on_nested_model_coordinates_without_double_transform(cad_api, origin, angle):
    module, probe, cad, _, _, _ = cad_api
    outer, inner = CadTransform(origin, angle), CadTransform((2, 3, 4), -math.pi/4)
    cad.GetTotalTransform = lambda: CadTransform((999, 999, 999), 1)
    cad.get_Geometry = lambda options: [GeometryInstance([GeometryInstance([Mesh()], inner)], outer)]
    first, second = (module.read_cad_geometry(probe, cad, mode) for mode in ("symbol", "instance"))
    assert first["status"] == second["status"] == "collected"
    for reading in (first, second):
        for actual, point in zip(reading["triangles_mm"][0], Mesh().triangles[0]):
            expected = outer.OfPoint(inner.OfPoint(point))
            assert actual == pytest.approx([304.8 * v for v in (expected.X, expected.Y, expected.Z)])
    assert "GetInstanceGeometry" in second["object_records"][0]["child_read_method"]
    assert "GetSymbolGeometry" in second["object_records"][1]["child_read_method"]
    comparison = diagnostics().compare_geometry_reads(first, second)
    assert comparison["status"] == "matches" and comparison["max_vertex_error_mm"] < 1e-8
    assert not comparison["placement_eligible"] and not comparison["layer_identity_verified"]


@pytest.mark.parametrize("alternative", ["error", "different", "same_unknown"])
def test_alternate_read_never_silently_replaces_primary_or_certifies_layers(cad_api, alternative):
    module, _, cad, DB, doc, _ = cad_api
    instance = GeometryInstance([Mesh(-1 if alternative == "same_unknown" else 100)])
    if alternative == "error":
        instance.GetInstanceGeometry = lambda: (_ for _ in ()).throw(RuntimeError("API copy unavailable"))
    elif alternative == "different":
        instance.GetInstanceGeometry = lambda: [Mesh(100, [[XYZ(10, 0, 0), XYZ(11, 0, 0), XYZ(10, 1, 0)]])]
    cad.get_Geometry = lambda options: [instance]
    r = module.collect_cad_report(doc, DB, cad)
    assert r["status"] == "partial" and not r["placement_eligible"]
    assert r["cad_geometry"]["triangle_count"] == (0 if alternative == "same_unknown" else 1)
    assert r["cad_geometry"]["all_mesh_triangle_count"] == 1
    assert r["cad_geometry_comparison"]["status"] == {
        "error": "not_checked", "different": "differs", "same_unknown": "matches"}[alternative]
    assert not r["cad_geometry_comparison"]["layer_identity_verified"]
    assert not r["document"]["is_modified_before"] and not r["document"]["is_modified_after"]
    assert verify_dxf_calibration(mosaic_sample(), r)["status"] == "blocked"


@pytest.mark.parametrize("layer", [100, -1])
def test_new_report_calibration_uses_only_confirmed_primary_layer(cad_api, layer):
    module, _, cad, DB, doc, _ = cad_api
    mosaic = mosaic_sample()
    points = report_sample(mosaic)["cad_geometry"]["triangles_mm"]
    triangles = [[XYZ(*(v / 304.8 for v in p)) for p in triangle] for triangle in points]
    cad.GetTransform = lambda: CadTransform()
    cad.get_Geometry = lambda options: [GeometryInstance([Mesh(layer, triangles), Mesh(101, triangles)])]
    report = module.collect_cad_report(doc, DB, cad)
    assert report["cad_geometry_comparison"]["status"] == "matches"
    result = verify_dxf_calibration(mosaic, report)
    assert result["status"] == ("geometry_matches" if layer == 100 else "blocked")
    assert not result["placement_eligible"]
    assert report["cad_geometry"]["all_mesh_triangle_count"] == 10
    assert report["cad_geometry"]["triangle_count"] == (5 if layer == 100 else 0)


def test_layer_catalog_is_bounded_metadata_and_cannot_relabel_geometry(cad_api):
    module, probe, cad, DB, _, _ = cad_api
    DB.GraphicsStyleType = SimpleNamespace(Projection="projection")
    layer = Element(100, "KLEENKA")
    layer.GetGraphicsStyle = lambda kind: SimpleNamespace(Id=Id(500))
    layer.LineColor = SimpleNamespace(Red=255, Green=0, Blue=0)
    cad.Category = Element(50, "Верхнее армирование X.dxf")
    cad.Category.SubCategories = [layer]
    inventory = diagnostics().layer_catalog(probe, cad)
    assert inventory["status"] == "collected"
    assert inventory["layers"] == [{"category_id": 100, "name": "KLEENKA",
                                     "projection_style_id": 500, "color_rgb": [255, 0, 0]}]
    cad.get_Geometry = lambda options: [Mesh(-1)]
    assert module.read_cad_geometry(probe, cad)["triangle_count"] == 0
    cad.Category.SubCategories = [layer] * 1001
    inventory = diagnostics().layer_catalog(probe, cad)
    assert inventory["status"] == "partial" and len(inventory["layers"]) == 1000


@pytest.mark.parametrize("limit", ["MAX_TRIANGLES", "MAX_OBJECTS", "MAX_DEPTH"])
def test_limits_are_reported_not_called_a_complete_read(cad_api, monkeypatch, limit):
    module, probe, cad, _, _, _ = cad_api
    monkeypatch.setattr(module, limit, 1)
    if limit == "MAX_DEPTH":
        cad.get_Geometry = lambda options: [Mesh(), GeometryInstance([GeometryInstance([Mesh()])])]
    else:
        cad.get_Geometry = lambda options: [Mesh(), Mesh(-1)]
    r = module.read_cad_geometry(probe, cad)
    assert r["triangle_count"] == 1  # useful prefix retained
    assert r["status"] == "partial" and r["limit_exceeded"] and not r["mesh_read_complete"]


def pair(module, triangles):
    base = {"mesh_read_complete": True, "units": "mm", "coordinate_system": "revit-internal-origin-and-axes",
            "meshes": [], "triangles_mm": triangles}
    return dict(copy.deepcopy(base), method=module.SYMBOL_METHOD), dict(copy.deepcopy(base), method=module.INSTANCE_METHOD)


@pytest.mark.parametrize("change", ["winding", "duplicates", "tolerance", "translation", "count", "nonfinite",
                                    "boolean", "shape", "incomplete", "same_method", "work_limit"])
def test_triangle_bijection_is_conservative_and_bounded(cad_api, monkeypatch, change):
    module = diagnostics()
    triangle = [[0., 0., 0.], [100., 0., 0.], [0., 100., 0.]]
    a, b = pair(module, [triangle, triangle, [[100., 0., 0.], [100., 100., 0.], [0., 100., 0.]]])
    expected = "not_checked"
    if change == "winding":
        b["triangles_mm"] = [list(reversed(t)) for t in reversed(b["triangles_mm"])]
        expected = "matches"
    elif change == "duplicates":
        b["triangles_mm"][2] = triangle  # counts equal but multiset differs
        expected = "differs"
    elif change in ("tolerance", "translation"):
        for t in b["triangles_mm"]:
            for p in t:
                p[2] += 0.001 if change == "tolerance" else 10
        expected = "matches" if change == "tolerance" else "differs"
    elif change == "count":
        b["triangles_mm"].pop()
        expected = "differs"
    elif change == "nonfinite":
        b["triangles_mm"][0][0][0] = float("nan")
    elif change == "boolean":
        b["triangles_mm"][0][0][0] = True
    elif change == "shape":
        b["triangles_mm"][0].pop()
    elif change == "incomplete":
        b["mesh_read_complete"] = False
    elif change == "same_method":
        b["method"] = a["method"]
    else:
        monkeypatch.setattr(module, "MAX_COMPARE_CHECKS", 1)
    r = module.compare_geometry_reads(a, b)
    assert r["status"] == expected and not r["placement_eligible"] and not r["layer_identity_verified"]
    if expected == "not_checked":
        assert r["reason"]


@pytest.mark.parametrize("name", ["qmonitoring-cad-20260909-160500-429000.json",
                                 "qmonitoring-cad-20260909-160538-917000.json"])
def test_private_050_failure_pattern_is_recorded_and_new_reader_continues(cad_api, name):
    source = ROOT / "revit_info/050" / name
    if not source.is_file():
        pytest.skip("Private Revit 0.5.0 report is not present")
    raw = source.read_bytes()
    old = json.loads(raw)
    assert old["probe_version"] == "0.5.0" and old["status"] == "partial"
    assert old["cad_geometry"]["visited_object_count"] == 89
    assert old["cad_geometry"]["triangle_count"] == 0
    assert "no resolvable CAD layer" in old["issues"][0]["message"]
    assert verify_dxf_calibration(mosaic_sample(), old)["status"] == "blocked"
    # Only the object sequence is reproduced: the old report contains NO mesh coordinates.
    module, probe, cad, _, _, _ = cad_api
    polyline = SimpleNamespace(GraphicsStyleId=Id(-1), GetType=lambda: SimpleNamespace(Name="PolyLine"))
    cad.get_Geometry = lambda options: [GeometryInstance([polyline] * 87 + [Mesh(-1), Mesh()])]
    r = module.read_cad_geometry(probe, cad)
    assert r["visited_object_count"] == 90 and r["all_mesh_triangle_count"] == 2
    assert r["mesh_read_complete"] and r["status"] == "partial" and r["triangle_count"] == 1
    assert source.read_bytes() == raw
