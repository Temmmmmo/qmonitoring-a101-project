"""Original source graphics: offline Revit doubles, NOT a Windows acceptance run."""
import ast
import copy
import hashlib
import importlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

import test_revit_plan_preview as old


@pytest.fixture
def module(monkeypatch):
    return old.module.__wrapped__(monkeypatch)


def packet():
    rows = []
    for direction in ("bottom-X", "bottom-Y", "top-X", "top-Y"):
        layer, axis = direction.split("-")
        d = {"layer": layer, "axis": axis}
        bounds = [-400, 100, 1400, 300] if axis == "X" else [100, -400, 300, 1400]
        rows.append({"direction": d, "source_bbox_mm": [0, 0, 1000, 1000],
            "cells": [{"cell_id": "КЭ-1", "polygon_mm": [[0, 0], [1000, 0], [1000, 1000], [0, 1000]], "aci": 1, "level_index": 1}],
            "legend": [{"level_index": 1, "aci": 1, "rgb": [255, 0, 0], "label": "Ø10 + Ø12", "as_min_cm2_per_m": 1, "as_max_cm2_per_m": 2}],
            "zone_drafts": [{"schema_version": "reinforcement-zone-revit/v2", "units": "mm", "source_zone_id": "Зона-1",
                "direction": d, "demand_bbox_mm": [0, 0, 1000, 1000], "level_index": 1,
                "components": [{"component_index": 0, "diameter_mm": 10, "nominal_step_mm": 200,
                    "axis_coordinates_mm": [100, 300], "axis_window_mm": [0, 400], "bar_count": 2,
                    "installed_length_mm": 1800, "bar_axis_bbox_mm": bounds,
                    "placement": {"phase_mm": 100, "offsets_mm": [0]}}], "recipe": {"opaque_original": "preserved"}}]})
    return {"schema_version": "source-isofields-zones/v1", "units": "mm",
        "source_stage": "original-parametric-zones-before-physical-normalization", "case_id": "real-source-fixture",
        "placement_eligible": False, "engineering_approval": False, "directions": rows,
        "source_files": [], "provenance_status": "unverified"}


def test_original_source_dispatch_is_lossless_without_physical_certificate(module):
    value = packet()
    before = copy.deepcopy(value)
    result = module.build_preview_primitives(value, 400, -200)
    assert module._validate_primitives(result) == result
    assert result["source_packet"] == before == value
    assert result["bars"] == [] and result["summary"] == {
        "source_cell_count": 4, "source_zone_count": 4, "direction_count": 4}
    result["source_packet"]["directions"][0]["zone_drafts"][0]["recipe"]["opaque_original"] = "separate-copy"
    assert value == before


@pytest.mark.parametrize("field,value", [("placement_eligible", True), ("engineering_approval", True),
    ("units", "m"), ("source_stage", "physical-normalized"), ("provenance_status", "approved")])
def test_reject_approval_or_wrong_source_stage(module, field, value):
    source = packet()
    source[field] = value
    with pytest.raises(ValueError):
        module.build_preview_primitives(source, 0, 0)


@pytest.mark.parametrize("change", [
    lambda p: p["directions"].pop(),
    lambda p: p["directions"].__setitem__(1, copy.deepcopy(p["directions"][0])),
    lambda p: p["directions"][0]["cells"].append(copy.deepcopy(p["directions"][0]["cells"][0])),
    lambda p: p["directions"][0]["zone_drafts"].append(copy.deepcopy(p["directions"][0]["zone_drafts"][0])),
    lambda p: p["directions"][0]["cells"][0].__setitem__("level_index", 2),
    lambda p: p["directions"][0]["cells"][0].__setitem__("aci", 3),
    lambda p: p["directions"][0]["cells"][0].__setitem__("polygon_mm", [[0, 0], [1000, 1000], [1000, 0], [0, 1000]]),
    lambda p: p["directions"][0]["cells"][0]["polygon_mm"].__setitem__(0, [float("nan"), 0]),
    lambda p: p["directions"][0]["zone_drafts"][0]["components"][0].__setitem__("bar_count", 3),
    lambda p: p["directions"][0]["zone_drafts"][0]["components"][0].__setitem__("installed_length_mm", 1700),
    lambda p: p.__setitem__("unused_metadata", float("inf")),
])
def test_malformed_source_never_silently_repaired(module, change):
    source = packet()
    change(source)
    with pytest.raises(ValueError):
        module.build_preview_primitives(source, 0, 0)


def test_resource_caps_and_primitive_tamper(module, monkeypatch):
    helper = importlib.import_module("qm_revit_source_preview")
    monkeypatch.setattr(helper, "MAX_CELLS", 3)
    with pytest.raises(ValueError, match="resource"):
        module.build_preview_primitives(packet(), 0, 0)
    monkeypatch.setattr(helper, "MAX_CELLS", 12000)
    data = module.build_preview_primitives(packet(), 0, 0)
    data["summary"]["source_zone_count"] = 3
    with pytest.raises(ValueError, match="changed"):
        module._validate_primitives(data)


def test_unverified_source_records_are_not_fabricated_binding(module, tmp_path):
    source = packet()
    source["source_files"] = [{"filename": "исходник.dxf", "role": "dxf"}]
    path = tmp_path / "источник.json"
    content = json.dumps(source, ensure_ascii=False).encode()
    path.write_bytes(content)
    loaded, digest = module.load_preview_input(str(path))
    assert loaded == source and digest == hashlib.sha256(content).hexdigest()
    source["provenance_status"] = "sha256_recorded"
    with pytest.raises(ValueError, match="SHA"):
        module.build_preview_primitives(source, 0, 0)


class Loop(list, old.Disposable):
    def Append(self, item):
        self.append(item)


class Region(old.Disposable):
    def GetBoundaries(self):
        return copy.deepcopy(self.loops)


class RegionType:
    pass


class Pattern:
    def GetFillPattern(self):
        result = old.Disposable()
        result.IsSolidFill = True
        return result


class SourceStyle(old.Style):
    def SetSurfaceForegroundPatternId(self, identifier):
        self.SurfaceForegroundPatternId = identifier

    def SetSurfaceForegroundPatternColor(self, color):
        self.SurfaceForegroundPatternColor = color

    def SetSurfaceForegroundPatternVisible(self, visible):
        self.IsSurfaceForegroundPatternVisible = visible

    def SetSurfaceBackgroundPatternVisible(self, visible):
        self.IsSurfaceBackgroundPatternVisible = visible


@pytest.fixture
def harness(module, monkeypatch):
    h = old.harness.__wrapped__(module, monkeypatch)
    h.floor.UniqueId = "native-floor-unique-id"
    h.primitives = module.build_preview_primitives(packet(), 123, -456)
    region_type, pattern = RegionType(), Pattern()
    region_type.Id, pattern.Id = old.Id(21), old.Id(22)
    h.doc.elements[21], h.doc.elements[22] = region_type, pattern
    h.before = dict(h.doc.elements)
    h.DB.FilledRegion, h.DB.FilledRegionType, h.DB.FillPatternElement = Region, RegionType, Pattern
    h.DB.CurveLoop, h.DB.OverrideGraphicSettings = Loop, SourceStyle
    h.DB.Color = lambda r, g, b: SimpleNamespace(Red=r, Green=g, Blue=b)
    monkeypatch.setattr(old.Point, "DistanceTo", lambda a, b: math.dist((a.X, a.Y, a.Z), (b.X, b.Y, b.Z)), raising=False)

    def add_region(doc, type_id, view_id, loops):
        assert doc.IsModifiable and type_id == region_type.Id
        region = Region()
        region.loops, region.OwnerViewId = copy.deepcopy(loops), view_id
        doc.next_id += 1
        region.Id = old.Id(doc.next_id)
        doc.elements[region.Id.Value] = region
        return region

    def set_override(view, identifier, style):
        assert view.doc.IsModifiable
        view.overrides[identifier.Value] = copy.deepcopy(style)

    original_note_create = h.DB.TextNote.Create

    def note_create(*args):
        assert "\n" not in args[3]  # Revit native paragraph delimiter is CR.
        result = original_note_create(*args)
        result.Text = result.text
        return result

    Region.Create = staticmethod(add_region)
    h.DB.TextNote.Create = note_create
    monkeypatch.setattr(old.View, "SetElementOverrides", set_override)
    monkeypatch.setattr(old.View, "GetElementOverrides", lambda view, identifier: copy.deepcopy(view.overrides[identifier.Value]), raising=False)
    monkeypatch.setattr(module, "read_preview_host", lambda *a: pytest.fail("Source view must not check or infer native holes/cover"))
    monkeypatch.setattr(module, "check_preview_host", lambda *a: pytest.fail("Source view is not physical host acceptance"))
    return h


def test_four_views_complete_source_and_real_mm_scale(module, harness):
    h = harness
    report = old.run(module, h, confirmed=True)
    assert report["status"] == "graphic_preview_created", report["issues"]
    assert report["readback"]["checked_view_count"] == 4
    assert report["readback"]["checked_source_cell_count"] == 4
    assert report["readback"]["checked_source_zone_count"] == 4
    assert report["readback"]["checked_rectangle_edge_count"] == 32
    assert len(report["created_source_views"]) == 4 and report["geometry_check"]["status"] == "not_checked"
    assert report["structural_elements_created"] == 0 and report["bar_element_mapping"] == []
    assert all(h.doc.elements[key] is value for key, value in h.before.items())
    region = h.doc.elements[report["source_cell_mapping"][0]["element_id"]]
    start, end = region.loops[0][0].ends
    assert start.X*304.8 == pytest.approx(123) and start.Y*304.8 == pytest.approx(-456)
    assert (end.X-start.X)*304.8 == pytest.approx(1000)  # NOT /view.Scale=100.
    assert report["source_zone_records"][0]["original_zone_draft"] == packet()["directions"][0]["zone_drafts"][0]
    assert all(h.doc.elements[row["view_id"]].Scale == 100 for row in report["created_source_views"])
    assert not report["placement_eligible"] and not report["engineering_approval"]


@pytest.mark.parametrize("tamper", ["FE", "zone", "note", "view", "color"])
def test_post_commit_failure_rolls_back_every_source_view(module, harness, monkeypatch, tamper):
    h = harness
    original = old.Transaction.Commit

    def commit(transaction):
        result = original(transaction)
        if tamper == "FE":
            next(e for e in h.doc.elements.values() if isinstance(e, Region)).loops[0][0].ends[0].X += 1
        elif tamper == "zone":
            next(e for e in h.doc.elements.values() if isinstance(e, old.DetailCurve)).GeometryCurve.ends[0].Y += 1
        elif tamper == "note":
            next(e for e in h.doc.elements.values() if hasattr(e, "Text")).Text = "lost parameters"
        elif tamper == "view":
            identifier = next(k for k, e in h.doc.elements.items() if isinstance(e, old.View))
            del h.doc.elements[identifier]
        else:
            view = next(e for e in h.doc.elements.values() if isinstance(e, old.View))
            next(s for s in view.overrides.values() if hasattr(s, "SurfaceForegroundPatternColor")).SurfaceForegroundPatternColor.Red = 0
        return result

    monkeypatch.setattr(old.Transaction, "Commit", commit)
    report = old.run(module, h, confirmed=True)
    assert report["status"] == "failed_rolled_back", report["issues"]
    assert h.doc.elements == h.before and report["created_view_id"] is None
    assert report["created_annotation_ids"] == [] and report["source_cell_mapping"] == []
    assert "created_source_views" not in report


def test_cancel_after_first_view_removes_whole_group(module, harness):
    report = old.run(module, harness, confirmed=True, progress=lambda stage, value, total: value < 2)
    assert report["status"] == "cancelled_rolled_back"
    assert harness.doc.elements == harness.before


@pytest.mark.parametrize("missing", ["zone-key", "zone-parameters", "legend"])
def test_absent_note_and_absent_mapping_still_fail_source_readback(module, harness, monkeypatch, missing):
    original = module.draw_source_views

    def incomplete(*args):
        original(*args)
        report = args[-2]
        note = next(row for row in report["source_note_mapping"] if row["kind"] == missing)
        report["source_note_mapping"].remove(note)
        report["created_annotation_ids"].remove(note["element_id"])
        del harness.doc.elements[note["element_id"]]

    monkeypatch.setattr(module, "draw_source_views", incomplete)
    report = old.run(module, harness, confirmed=True)
    assert report["status"] == "failed_rolled_back", report["issues"]
    assert "omitted" in report["issues"][0]["message"]
    assert harness.doc.elements == harness.before


def test_wrong_fill_and_matching_wrong_mapping_do_not_fool_readback(module, harness, monkeypatch):
    original = module.draw_source_views

    def bad_color(*args):
        original(*args)
        row = args[-2]["source_cell_mapping"][0]
        view = harness.doc.elements[row["view_id"]]
        row["rgb"] = [0, 0, 0]
        view.overrides[row["element_id"]].SurfaceForegroundPatternColor.Red = 0

    monkeypatch.setattr(module, "draw_source_views", bad_color)
    report = old.run(module, harness, confirmed=True)
    assert report["status"] == "failed_rolled_back", report["issues"]
    assert "fill override differs" in report["issues"][0]["message"]


def test_source_runtime_and_button_python2_grammar(module):
    from lib2to3.pgen2 import driver
    from lib2to3 import pygram, pytree
    parser = driver.Driver(pygram.python_grammar, convert=pytree.convert)
    helper = importlib.import_module("qm_revit_source_preview")
    button = old.LIB.parent / "QMonitoring.tab/Diagnostics.panel/PlanPreview.pushbutton/script.py"
    for path in (Path(helper.__file__), Path(module.__file__), button):
        parser.parse_string(path.read_text(encoding="utf-8")+"\n")


def test_missing_existing_style_and_no_consent_do_not_start_transaction(module, harness):
    h = harness
    report = old.run(module, h, confirmed=False)
    assert report["status"] == "blocked_preflight" and h.doc.scopes == []
    del h.doc.elements[21]
    report = old.run(module, h, confirmed=True)
    assert report["status"] == "blocked_preflight" and h.doc.scopes == []
    assert "EXISTING" in report["issues"][0]["message"]


@pytest.mark.parametrize("mode", ["live_local", "detached_with_worksets"])
def test_source_worksharing_retained_without_host_or_workset_checkout(module, harness, monkeypatch, mode):
    h = harness
    h.doc.IsWorkshared = True
    h.doc.GetWorksetTable = lambda: SimpleNamespace(GetActiveWorksetId=lambda: SimpleNamespace(IntegerValue=7))
    monkeypatch.setattr(module, "classify_document", lambda *args: {"status": "supported", "mode": mode})
    report = old.run(module, h, confirmed=True)
    assert report["status"] == "graphic_preview_created", report["issues"]
    assert report["active_workset_unchanged"] and report["selected_floor_snapshot_unchanged"]
    assert not report["checkout_requested"] and not report["save_requested"] and not report["synchronization_requested"]
    assert len(report["created_source_views"]) == 4


def test_draw_and_readback_have_no_ironpython_nested_comprehensions(module):
    helper = importlib.import_module("qm_revit_source_preview")
    tree = ast.parse(Path(helper.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in {"draw_source_views", "readback_source_views"}:
            assert not any(isinstance(item, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) for item in ast.walk(node))


def test_public_code_only_zip_deterministic_whitelist(module, monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(old.ROOT / "scripts"))
    package = importlib.import_module("package_revit_plan_preview")
    first = package.build_code_only_package(tmp_path / "one.zip")
    second = package.build_code_only_package(tmp_path / "two.zip")
    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as archive:
        assert archive.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["code_only"] is True and manifest["version"] == "0.2.1"
        assert {row["path"] for row in manifest["files"]} == set(archive.namelist())-{"manifest.json"}
        for row in manifest["files"]:
            assert hashlib.sha256(archive.read(row["path"])).hexdigest() == row["sha256"]
        assert not any(name.endswith((".rvt", ".dxf", "trial.json")) or "Trial.pushbutton" in name for name in archive.namelist())
        assert "QMonitoringPreview.extension/lib/qm_revit_source_preview.py" in archive.namelist()
        assert not any(name.startswith("QMonitoring.extension/") for name in archive.namelist())
    with pytest.raises(FileExistsError):
        package.build_code_only_package(first)
