"""Offline API doubles only: native working Floor/CAD, never reference-model fallback."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from test_revit_cad_probe import (  # noqa: F401 -- shared fixtures
    GeometryInstance, Mesh, api, cad_api, modules,
)
from test_revit_probe import Element, Face, Floor

ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "integrations/pyrevit/QMonitoring.extension"
BUTTON = EXTENSION / "QMonitoring.tab/Diagnostics.panel/WorkingHostProbe.pushbutton/script.py"


@pytest.fixture
def working_api(request, monkeypatch):
    cad_module, probe, cad, DB, doc, elements = request.getfixturevalue("cad_api")
    module = importlib.import_module("qm_working_host_probe")
    floor = Floor(11020633, "Рабочая плита", "ПМ-3")
    floor.Document = cad.Document = doc
    elements[floor.Id.Value] = floor
    doc.Title, doc.PathName = "Рабочая модель", r"C:\Рабочая папка\модель.rvt"
    doc.IsReadOnly = doc.IsModifiable = False
    DB.ViewDetailLevel = SimpleNamespace(Fine="Fine")
    solid = DB.Solid()
    solid.Volume, solid.Faces = 123, [Face(True), Face(False)]
    floor.get_Geometry = lambda options: [solid]
    cad.Category = Element(200, "Рабочий DXF")
    cad.Category.SubCategories = []
    reads = []

    def get_element(value):
        assert value.Value not in {407801, 407878, 165160, 165161, 165163, 123}
        reads.append(value.Value)
        return elements.get(value.Value)

    doc.GetElement = get_element

    def forbidden(*args, **kwargs):
        pytest.fail("Reference-model collector or Revit mutation must not be called")

    monkeypatch.setattr(cad_module, "collect_cad_report", forbidden)
    monkeypatch.setattr(importlib.import_module("qm_revit_probe"), "collect_report", forbidden)
    DB.Transaction = DB.TransactionGroup = DB.SubTransaction = forbidden
    DB.Structure = SimpleNamespace(Rebar=SimpleNamespace(CreateFromCurves=forbidden))
    return module, probe, floor, cad, DB, doc, elements, solid, reads


def collect(case, **kwargs):
    module, _, floor, cad, DB, doc, *_ = case
    return module.collect_working_host_report(doc, DB, floor, cad, **kwargs)


def test_complete_working_probe_reads_selected_native_host_and_cad_without_test_ids(working_api):
    result = collect(working_api)
    assert result["status"] == "collected"
    assert result["read_issues"] == result["host_read_issues"] == result["issues"] == []
    assert result["host_id"] == result["host"]["element_id"] == 11020633
    assert result["cad_id"] == result["cad"]["element_id"] == 9
    assert result["schema_version"] == "revit-working-host-cad-probe/v1"
    assert result["units"] == "mm" and result["coordinate_system"] == "revit-internal-origin-and-axes"
    assert result["read_only"] is True
    assert result["placement_eligible"] is result["engineering_approval"] is False
    assert result["host"]["covers"]["top"]["distance_mm"] == pytest.approx(25)
    assert len(result["host_solid"]["faces"]) == 2
    assert result["host_solid"]["volume_mm3"] == pytest.approx(123 * 304.8 ** 3)
    assert result["cad_geometry"]["triangle_count"] == result["cad_geometry_instance"]["triangle_count"] == 1
    assert result["cad_geometry_comparison"]["status"] == "matches"
    assert result["cad"]["total_transform"]["origin_mm"] == pytest.approx([304.8, 609.6, 914.4])
    assert result["document"]["modification_flag_unchanged"] is True
    assert result["source_binding"]["status"] == result["source_dxf_units"]["status"] == "not_checked"
    assert "area" not in result and "bar_types" not in result


@pytest.mark.parametrize("part", ["family", "foreign_floor", "foreign_cad", "not_floor", "not_cad"])
def test_selections_must_be_native_and_in_same_project(working_api, part):
    module, _, floor, cad, DB, doc, *_ = working_api
    if part == "family":
        doc.IsFamilyDocument = True
    elif part == "foreign_floor":
        floor.Document = object()
    elif part == "foreign_cad":
        cad.Document = object()
    elif part == "not_floor":
        floor = cad
    else:
        cad = floor
    with pytest.raises(ValueError):
        module.collect_working_host_report(doc, DB, floor, cad)


@pytest.mark.parametrize("part", ["host", "solid", "cad", "layers", "geometry", "identity"])
def test_partial_read_retains_other_actual_evidence_and_host_issues_are_separate(working_api, monkeypatch, part):
    module, _, floor, cad, _, _, _, solid, _ = working_api

    def fail(*args):
        raise RuntimeError("Не удалось прочитать часть геометрии")

    if part == "host":
        monkeypatch.setattr(module.Probe, "floor", fail)
    elif part == "solid":
        solid.Faces = []
    elif part == "cad":
        cad.GetTotalTransform = fail
    elif part == "layers":
        cad.Category = None
    elif part == "geometry":
        cad.get_Geometry = lambda options: [Mesh(-1)]
    else:
        original = module.Probe.floor
        monkeypatch.setattr(module.Probe, "floor", lambda p, f: dict(original(p, f), element_id=999))
    result = collect(working_api)
    assert result["status"] == "partial" and result["host_id"] == floor.Id.Value
    assert result["read_issues"] or result["issues"]
    assert bool(result["host_read_issues"]) == (part in {"host", "solid", "identity"})
    if part != "host":
        assert result["host"] is not None
    if part != "solid":
        assert result["host_solid"] is not None
    if part not in {"cad", "geometry"}:
        assert result["cad_geometry"]["triangle_count"] == 1
    assert result["placement_eligible"] is False


@pytest.mark.parametrize("limit", ["MAX_HOST_FACES", "MAX_HOST_EDGES", "MAX_HOST_TESSELLATION_POINTS"])
def test_host_solid_limits_do_not_return_truncated_approval(working_api, monkeypatch, limit):
    module = working_api[0]
    monkeypatch.setattr(module, limit, 1)
    result = collect(working_api)
    assert result["status"] == "partial" and result["host_solid"] is None
    assert result["host_read_issues"] and result["host"] is not None
    assert result["cad_geometry"]["triangle_count"] == 1


@pytest.mark.parametrize("geometry", ["empty", "multiple", "nested"])
def test_no_native_solid_substitution(working_api, geometry):
    _, _, floor, _, _, _, _, solid, _ = working_api
    floor.get_Geometry = lambda options: {"empty": [], "multiple": [solid, solid],
                                        "nested": [GeometryInstance([solid])]}[geometry]
    result = collect(working_api)
    assert result["status"] == "partial" and result["host_solid"] is None
    assert result["host_read_issues"]


def test_unexpected_document_flag_change_is_not_called_clean(working_api, monkeypatch):
    module, _, _, _, _, doc, *_ = working_api
    original = module.read_host_solid

    def flag_changed(probe, floor):
        result = original(probe, floor)
        doc.IsModified = True  # Deliberate external change in a double, not a command operation.
        return result

    monkeypatch.setattr(module, "read_host_solid", flag_changed)
    result = collect(working_api)
    assert result["status"] == "partial"
    assert result["document"]["modification_flag_unchanged"] is False
    assert result["issues"][0]["stage"] == "document"


def test_optional_disk_hash_does_not_certify_cached_import_and_unicode_round_trips(working_api, tmp_path):
    path = tmp_path / "Верхнее армирование вдоль ОСИ Х.dxf"
    path.write_bytes(b"exact original bytes")
    result = collect(working_api, source_dxf_path=str(path))
    assert result["selected_source_file"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert "NOT proof" in result["selected_source_file"]["scope"]
    assert result["linked_source_file"]["status"] == "not_checked"
    assert result["source_binding"]["status"] == "not_checked"
    writer = importlib.import_module("qm_revit_probe").write_report_json
    target = tmp_path / "Рабочая плита.json"
    writer(str(target), result)
    assert json.loads(target.read_text(encoding="utf-8")) == result
    with pytest.raises(OSError):
        writer(str(target), result)


def install_ui(case, monkeypatch, tmp_path, *, selected=True, stop=None):
    _, _, floor, cad, DB, doc, *_ = case
    destination = tmp_path / "Отчёт.json"
    if stop == "existing":
        destination.write_bytes(b"keep")
    picks = []

    class Cancelled(Exception):
        pass

    def pick(kind, selection_filter, prompt):
        picks.append((kind, prompt))
        assert selection_filter.AllowElement(floor)
        assert not selection_filter.AllowElement(cad)
        assert not selection_filter.AllowReference(None, None)
        if stop == "floor":
            raise Cancelled()
        return SimpleNamespace(ElementId=floor.Id)

    uidoc = SimpleNamespace(Selection=SimpleNamespace(
        GetElementIds=lambda: [floor.Id] if selected else [], PickObject=pick))

    class Collector:
        def __init__(self, document):
            assert document is doc

        def OfClass(self, cls):
            assert cls is DB.ImportInstance
            return self

        def WhereElementIsNotElementType(self):
            return [] if stop == "no_cad" else [cad]

    DB.FilteredElementCollector = Collector
    forms = SimpleNamespace(
        alert=lambda *a, **kw: stop != "confirmation",
        SelectFromList=SimpleNamespace(show=lambda values, **kw: None if stop == "cad" else values[0]),
        save_file=lambda **kw: None if stop == "destination" else str(destination),
        pick_file=lambda **kw: None)
    monkeypatch.setitem(sys.modules, "pyrevit", SimpleNamespace(DB=DB, forms=forms, revit=SimpleNamespace(doc=doc, uidoc=uidoc)))
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.Exceptions", SimpleNamespace(OperationCanceledException=Cancelled))
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.UI.Selection", SimpleNamespace(ISelectionFilter=object, ObjectType=SimpleNamespace(Element="Element")))
    return destination, picks


@pytest.mark.parametrize("selected", [True, False])
def test_button_selected_floor_or_native_pick_and_optional_dxf_cancel_still_saves(working_api, monkeypatch, tmp_path, selected):
    target, picks = install_ui(working_api, monkeypatch, tmp_path, selected=selected)
    runpy.run_path(str(BUTTON), run_name="__main__")
    result = json.loads(target.read_text(encoding="utf-8"))
    assert result["status"] == "collected" and result["host_id"] == 11020633
    assert result["selected_source_file"]["status"] == "not_checked"
    assert bool(picks) is not selected


@pytest.mark.parametrize("stop", ["floor", "cad", "destination", "confirmation", "existing", "no_cad"])
def test_button_cancellation_creates_nothing_and_does_not_overwrite(working_api, monkeypatch, tmp_path, stop):
    target, _ = install_ui(working_api, monkeypatch, tmp_path, selected=False, stop=stop)
    runpy.run_path(str(BUTTON), run_name="__main__")
    if stop == "existing":
        assert target.read_bytes() == b"keep"
    else:
        assert not target.exists()


@pytest.fixture
def packager():
    spec = importlib.util.spec_from_file_location("package_working_probe_test", ROOT / "scripts/package_revit_working_host_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_package_contains_only_readonly_button_and_dependencies(packager, tmp_path):
    output = packager.build_package(tmp_path / "readonly.zip")
    original = output.read_bytes()
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == set(packager.FILES) | {"README.md"}
        assert not any(part in name for name in archive.namelist() for part in ("Trial", "MVP", "qm_revit_trial", "ReferenceProbe", "CadProbe.pushbutton"))
        for name in packager.FILES:
            assert archive.read(name) == (packager.SOURCE / name).read_bytes()
        assert "НЕ создаёт арматуру" in archive.read("README.md").decode("utf-8")
    with pytest.raises(FileExistsError):
        packager.build_package(output)
    assert output.read_bytes() == original


def test_package_missing_runtime_file_fails_before_output_creation(packager, monkeypatch, tmp_path):
    monkeypatch.setattr(packager, "SOURCE", tmp_path / "missing")
    output = tmp_path / "not-created.zip"
    with pytest.raises(FileNotFoundError):
        packager.build_package(output)
    assert not output.exists()


def test_package_optional_exact_source_is_separate_from_extension(packager, tmp_path):
    source = tmp_path / "Верхнее Х.dxf"
    source.write_bytes(b"source data")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    output = packager.build_package(tmp_path / "with-source.zip", dxf=source, dxf_sha256=digest)
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == set(packager.FILES) | {"README.md", "source/top-X.dxf"}
        assert archive.read("source/top-X.dxf") == source.read_bytes()
        assert digest in archive.read("README.md").decode("utf-8")


@pytest.mark.parametrize("bad", ["no_hash", "no_file", "stale_hash", "invalid_hash", "empty", "wrong_ext", "missing"])
def test_package_rejects_unbound_or_changed_source_before_creating_zip(packager, tmp_path, bad):
    source = tmp_path / ("source.rvt" if bad == "wrong_ext" else "source.dxf")
    source.write_bytes(b"" if bad == "empty" else b"source")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if bad == "missing":
        source = tmp_path / "missing.dxf"
    elif bad == "no_hash":
        digest = None
    elif bad == "no_file":
        source = None
    elif bad == "stale_hash":
        digest = "a" * 64
    elif bad == "invalid_hash":
        digest = "../bad"
    output = tmp_path / "not-created.zip"
    with pytest.raises(ValueError):
        packager.build_package(output, dxf=source, dxf_sha256=digest)
    assert not output.exists()
