"""Read-only CAD adapter tests with small injected Autodesk API doubles."""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_revit_dxf_calibration import matrix, transform
from test_revit_probe import (  # noqa: F401 -- shared fixture registration
    Cad, Element, Id, XYZ, api, modules,
)

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "integrations/pyrevit/QMonitoring.extension/lib"
BUTTON = LIB.parent / "QMonitoring.tab/Diagnostics.panel/CadProbe.pushbutton/script.py"


class CadTransform:
    def __init__(self, origin=(0, 0, 0), angle=0):
        self.m = matrix(angle, origin)
        self.Origin = XYZ(*origin)
        self.BasisX, self.BasisY, self.BasisZ = (XYZ(*self.m['basis_' + a]) for a in 'xyz')
        self.Determinant, self.IsConformal = 1, True

    def OfPoint(self, p):
        return XYZ(*transform((p.X, p.Y, p.Z), self.m))

    def Multiply(self, other):
        return ComposedTransform(self, other)


class ComposedTransform:
    def __init__(self, parent, child):
        self.parent, self.child = parent, child

    def OfPoint(self, p):
        return self.parent.OfPoint(self.child.OfPoint(p))

    def Multiply(self, child):
        return ComposedTransform(self, child)


class Mesh:
    def __init__(self, layer=100, triangles=None):
        self.GraphicsStyleId = Id(layer)
        self.MaterialElementId = Id(-1)
        self.triangles = triangles or [[XYZ(0, 0, 0), XYZ(1, 0, 0), XYZ(0, 1, 0)]]
        self.NumTriangles = len(self.triangles)

    def get_Triangle(self, i):
        return SimpleNamespace(get_Vertex=lambda j: self.triangles[i][j])

    def GetType(self):
        return SimpleNamespace(Name=type(self).__name__)


class GeometryInstance:
    def __init__(self, contents, transform=None, layer=-1):
        self.contents, self.Transform, self.GraphicsStyleId = contents, transform or CadTransform(), Id(layer)

    def GetSymbolGeometry(self):
        return self.contents

    def GetInstanceGeometry(self):
        # Like Revit's root instance copy: bake transforms into leaves/nested placements.
        def clone(obj):
            if isinstance(obj, Mesh):
                result = Mesh(obj.GraphicsStyleId.Value, [[self.Transform.OfPoint(p) for p in t] for t in obj.triangles])
                result.MaterialElementId = obj.MaterialElementId
                return result
            if isinstance(obj, GeometryInstance):
                return GeometryInstance(obj.contents, self.Transform.Multiply(obj.Transform), obj.GraphicsStyleId.Value)
            return obj
        return [clone(obj) for obj in self.contents]

    def GetType(self):
        return SimpleNamespace(Name="GeometryInstance")


@pytest.fixture
def cad_api(request):
    _, adapter = request.getfixturevalue("modules")
    DB, doc, elements = request.getfixturevalue("api")
    cad_module = importlib.import_module("qm_revit_cad")
    DB.Mesh, DB.Solid, DB.ImportInstance = Mesh, type("Solid", (), {}), Cad
    DB.GeometryInstance = GeometryInstance
    DB.Transform, DB.Options = SimpleNamespace(Identity=CadTransform()), SimpleNamespace
    for number, name in ((100, "KLEENKA"), (101, "PLAST"), (102, "COLORSCALE")):
        elements[number] = SimpleNamespace(GraphicsStyleCategory=Element(number, name))
    elements[9].get_Geometry = lambda options: [GeometryInstance([Mesh(), Mesh(101), Mesh(102)])]
    doc.IsModified = False
    return cad_module, adapter.Probe(doc, DB), elements[9], DB, doc, elements


def test_actual_adapter_excludes_plast_legend_and_applies_nested_transform_once(cad_api):
    module, probe, cad, _, _, _ = cad_api
    outer, inner = CadTransform((10, 20, 30), math.pi/2), CadTransform((1, 2, 3))
    cad.get_Geometry = lambda options: [GeometryInstance([
        GeometryInstance([Mesh(-1)], inner, 100), Mesh(101), Mesh(102)], outer)]
    r = module.read_cad_geometry(probe, cad)
    assert r["status"] == "collected" and r["triangle_count"] == 1
    for actual, local in zip(r["triangles_mm"][0], Mesh().triangles[0]):
        p = outer.OfPoint(inner.OfPoint(local))
        assert actual == pytest.approx([304.8*p.X, 304.8*p.Y, 304.8*p.Z])
    assert any(o["layer"] == "PLAST" for o in r["objects"])


@pytest.mark.parametrize("failure", ["empty", "unknown_layer", "wrong_style", "unsupported", "nonfinite",
                                    "triangle_limit", "object_limit", "depth_limit", "api_failure"])
def test_adapter_does_not_accept_partial_or_unsupported_geometry(cad_api, monkeypatch, failure):
    module, probe, cad, _, _, elements = cad_api
    objects = [Mesh()]
    if failure == "empty":
        objects = [Mesh(101)]
    elif failure == "unknown_layer":
        objects = [Mesh(-1)]
    elif failure == "wrong_style":
        del elements[100]
    elif failure == "unsupported":
        objects = [SimpleNamespace(GraphicsStyleId=Id(100), GetType=lambda: SimpleNamespace(Name="PolyLine"))]
    elif failure == "nonfinite":
        objects[0].triangles[0][0].X = float("nan")
    elif failure == "triangle_limit":
        monkeypatch.setattr(module, "MAX_TRIANGLES", 0)
    elif failure == "object_limit":
        monkeypatch.setattr(module, "MAX_OBJECTS", 0)
    elif failure == "depth_limit":
        for _ in range(module.MAX_DEPTH+2):
            objects = [GeometryInstance(objects)]
    else:
        objects[0].get_Triangle = lambda i: (_ for _ in ()).throw(RuntimeError("API read failed"))
    cad.get_Geometry = lambda options: objects
    r = module.read_cad_geometry(probe, cad)
    assert r["status"] == "partial" and r["issues"]


def test_cad_report_keeps_reference_and_never_claims_calibration(cad_api):
    module, _, cad, DB, doc, _ = cad_api
    r = module.collect_cad_report(doc, DB, cad)
    assert r["status"] == "collected" and r["read_only"]
    assert r["area"]["physical_bar_count"] == 9
    assert r["cad_geometry"]["triangle_count"] == 1
    assert r["linked_source_file"]["status"] == "not_checked"
    assert r["cad"]["coordinate_calibration"] == "not_checked" and not r["placement_eligible"]


@pytest.mark.parametrize("kind", ["planar", "curved", "empty"])
def test_planar_solid_faces_are_read_and_unsupported_faces_rejected(cad_api, kind):
    module, probe, cad, DB, _, _ = cad_api

    class Face:
        GraphicsStyleId = Id(-1)
        MaterialElementId = Id(-1)

        def Triangulate(self):
            return Mesh()

    class Faces(list):
        @property
        def Size(self):
            return len(self)

    DB.PlanarFace = Face
    solid = DB.Solid()
    solid.GraphicsStyleId = Id(100)
    solid.GetType = lambda: SimpleNamespace(Name="Solid")
    solid.Faces = Faces([] if kind == "empty" else [Face() if kind == "planar" else object()])
    cad.get_Geometry = lambda options: [solid]
    result = module.read_cad_geometry(probe, cad)
    assert result["status"] == ("collected" if kind == "planar" else "partial")
    assert result["triangle_count"] == (1 if kind == "planar" else 0)


def test_hash_rejects_a_file_changed_during_read(cad_api, tmp_path, monkeypatch):
    module = cad_api[0]
    p = tmp_path / "test.dxf"
    p.write_bytes(b"keep")
    snapshots = iter([(4, 100), (4, 101)])
    monkeypatch.setattr(module, "file_snapshot", lambda path: next(snapshots))
    with pytest.raises(ValueError, match="changed"):
        module.hash_local_dxf(str(p))
    assert p.read_bytes() == b"keep"


def test_local_linked_dxf_fingerprint_with_unicode_path_and_no_overwrite(cad_api, tmp_path):
    module, probe, cad, DB, _, elements = cad_api
    p = tmp_path / "верхнее Х.dxf"
    p.write_bytes(b"synthetic DXF bytes")
    cad.IsLinked = True
    elements[4].GetExternalFileReference = lambda: SimpleNamespace(
        GetAbsolutePath=lambda: str(p), GetLinkedFileStatus=lambda: "Loaded")
    DB.ModelPathUtils = SimpleNamespace(ConvertModelPathToUserVisiblePath=lambda path: path)
    r = module.linked_source_file(probe, cad)
    assert r["status"] == "read" and r["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert r["name"] == p.name and p.read_bytes() == b"synthetic DXF bytes"
    elements[4].GetExternalFileReference = lambda: SimpleNamespace(
        GetAbsolutePath=lambda: str(p), GetLinkedFileStatus=lambda: "Unloaded")
    assert module.linked_source_file(probe, cad)["status"] == "not_checked"


@pytest.mark.parametrize("kind", ["unc", "relative", "rvt", "empty", "oversize", "missing"])
def test_hash_does_not_read_network_or_unrelated_files(cad_api, tmp_path, monkeypatch, kind):
    module = cad_api[0]
    p = tmp_path / ("keep.rvt" if kind == "rvt" else "file.dxf")
    p.write_bytes(b"" if kind == "empty" else b"keep")
    path = str(p)
    if kind == "unc":
        path = "//server/share/file.dxf"
    elif kind == "relative":
        path = "file.dxf"
    elif kind == "oversize":
        monkeypatch.setattr(module, "MAX_FILE_BYTES", 1)
    elif kind == "missing":
        path = str(tmp_path / "missing.dxf")
    with pytest.raises((ValueError, OSError)):
        module.hash_local_dxf(path)


@pytest.mark.parametrize("stop", ["none", "confirmation", "selection", "destination", "existing", "rvt", "no_cad"])
def test_cad_ui_cancellation_and_safe_report_write(cad_api, monkeypatch, tmp_path, stop):
    module, _, cad, DB, doc, _ = cad_api
    p = tmp_path / ("keep.rvt" if stop == "rvt" else "report.json")
    if stop in ("existing", "rvt"):
        p.write_bytes(b"preserve")

    class Collector:
        def __init__(self, document):
            pass

        def OfClass(self, cls):
            return self

        def WhereElementIsNotElementType(self):
            return [] if stop == "no_cad" else [cad]

    DB.FilteredElementCollector = Collector
    forms = SimpleNamespace(alert=lambda *a, **kw: stop != "confirmation",
        SelectFromList=SimpleNamespace(show=lambda values, **kw: None if stop == "selection" else values[0]),
        save_file=lambda **kw: None if stop == "destination" else str(p))
    calls = []
    original = module.collect_cad_report
    monkeypatch.setattr(module, "collect_cad_report", lambda *a, **kw: (calls.append(True), original(*a, **kw))[1])
    monkeypatch.setitem(sys.modules, "pyrevit", SimpleNamespace(DB=DB, revit=SimpleNamespace(doc=doc), forms=forms))
    runpy.run_path(str(BUTTON))
    if stop == "none":
        assert len(calls) == 1 and json.loads(p.read_text())["cad_geometry"]["triangle_count"] == 1
    else:
        assert not calls
        if stop in ("existing", "rvt"):
            assert p.read_bytes() == b"preserve"
        else:
            assert not p.exists()
