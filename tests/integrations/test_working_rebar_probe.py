"""Read-only native-host inventory API doubles; no Autodesk runtime is claimed."""
import ast
import importlib
import importlib.util
import json
import math
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from test_revit_probe import Element, Floor, Id, XYZ, api, modules  # noqa: F401


ROOT = Path(__file__).resolve().parents[2]
EXTENSION = ROOT / "integrations/pyrevit/QMonitoring.extension"
MODULE_PATH = EXTENSION / "lib/qm_working_rebar_probe.py"
BUTTON = EXTENSION / "QMonitoring.tab/Diagnostics.panel/WorkingRebarProbe.pushbutton/script.py"


def xyz(x, y, z):
    return XYZ(x / 304.8, y / 304.8, z / 304.8)


class Line:
    IsBound = True

    def __init__(self, start=(100, 200, 300), end=(4000, 200, 300)):
        self.start, self.end = start, end
        self.Length = math.dist(start, end) / 304.8

    def GetType(self):
        return SimpleNamespace(Name=type(self).__name__)

    def GetEndPoint(self, index):
        return xyz(*(self.start if index == 0 else self.end))

    def Evaluate(self, value, normalized):
        assert normalized is True
        return xyz(*(a + value * (b - a) for a, b in zip(self.start, self.end)))

    def Tessellate(self):
        return [self.GetEndPoint(0), self.GetEndPoint(1)]


class Arc:
    IsBound = True
    Center = xyz(100, 200, 300)
    Radius = 25 / 304.8
    Normal = XYZ(0, -1, 0)
    XDirection, YDirection = XYZ(1, 0, 0), XYZ(0, 0, 1)
    Length = 25 * math.pi / 2 / 304.8

    def GetType(self):
        return SimpleNamespace(Name="Arc")

    def GetEndPoint(self, index):
        return xyz(125, 200, 300) if index == 0 else xyz(100, 200, 325)

    def Evaluate(self, value, normalized):
        assert normalized is True
        return xyz(100 + 25 * math.cos(value * math.pi / 2), 200,
            300 + 25 * math.sin(value * math.pi / 2))

    def GetEndParameter(self, index):
        return 0 if index == 0 else math.pi / 2

    def Tessellate(self):
        return [self.GetEndPoint(0), self.Evaluate(0.5, True), self.GetEndPoint(1)]


class BaseBar(Element):
    LayoutRule = "NumberWithSpacing"

    def __init__(self, eid, host=11020633):
        super().__init__(eid, "Арматура Ø18 А500", mark="П1")
        self.host = host
        self.NumberOfBarPositions = 3
        self.Quantity = 2
        self.excluded = {1}
        self.calls = []
        self.curves = {0: [Line()], 2: [Arc()]}

    def GetHostId(self):
        return Id(self.host)

    def GetTypeId(self):
        return Id(165160)

    def DoesBarExistAtPosition(self, index):
        return index not in self.excluded

    def GetMaterialIds(self, paint):
        assert paint is False
        return [Id(90)]

    def GetBarPositionTransform(self, index):
        pytest.fail("Already transformed geometry must not be transformed a second time")

    def GetMovedBarTransform(self, index):
        pytest.fail("Already transformed geometry must not be transformed a second time")


class Rebar(BaseBar):
    def GetShapeId(self):
        return Id(91)

    def GetHookTypeId(self, index):
        return Id(-1)

    def GetTransformedCenterlineCurves(self, adjust, hooks, bends, multiplanar, index):
        assert (adjust, hooks, bends, multiplanar) == (False, False, False, "all-multiplanar")
        self.calls.append(index)
        return self.curves[index]


class RebarInSystem(BaseBar):
    SystemId = Id(80)

    def GetTransformedCenterlineCurves(self, adjust, hooks, bends, index):
        assert (adjust, hooks, bends) == (False, False, False)
        self.calls.append(index)
        return self.curves[index]


class AreaReinforcement(Element):
    def GetHostId(self):
        return Id(11020633)

    def GetRebarInSystemIds(self):
        return [Id(71)]


class PathReinforcement(AreaReinforcement):
    pass


class FabricArea(Element):
    HostId = Id(11020633)


class FabricSheet(FabricArea):
    pass


class RebarContainer(AreaReinforcement):
    pass


class RevitLinkInstance(Element):
    def GetLinkDocument(self):
        return None


@pytest.fixture
def rebar_api(request, monkeypatch):
    probe_modules = request.getfixturevalue("modules")
    native_api = request.getfixturevalue("api")
    module = importlib.import_module("qm_working_rebar_probe")
    DB, doc, elements = native_api
    floor = Floor(11020633, "Рабочая плита", "ПМ3")
    floor.Document = doc
    first, system = Rebar(70), RebarInSystem(71)
    other = Rebar(72, host=999)
    parent = AreaReinforcement(80)
    elements.update({11020633: floor, 70: first, 71: system, 72: other, 80: parent,
        90: Element(90, "А500С"), 91: Element(91, "Форма 21")})
    for element in elements.values():
        if not isinstance(element, Element):
            continue
        element.Document = doc
        element.WorksetId = Id(1)
        element.OwnerViewId = Id(-1)
        element.CreatedPhaseId = Id(10)
        element.DemolishedPhaseId = Id(-1)
        element.GroupId = Id(-1)
        if not hasattr(element, "GetMaterialIds"):
            element.GetMaterialIds = lambda paint: []
    doc.PathName = r"C:\Рабочая папка\модель.rvt"
    doc.IsReadOnly = doc.IsModifiable = doc.IsModified = doc.IsDetached = doc.IsModelInCloud = False
    doc.ProjectInformation = SimpleNamespace(UniqueId="project-unique-id")
    doc.GetWorksharingCentralModelPath = lambda: "central-native-path"
    DB.ModelPathUtils = SimpleNamespace(ConvertModelPathToUserVisiblePath=lambda path: "central-visible-path")
    DB.Line, DB.Arc, DB.RevitLinkInstance = Line, Arc, RevitLinkInstance
    DB.Structure = SimpleNamespace(Rebar=Rebar, RebarInSystem=RebarInSystem,
        AreaReinforcement=AreaReinforcement, PathReinforcement=PathReinforcement,
        FabricArea=FabricArea, FabricSheet=FabricSheet, RebarContainer=RebarContainer,
        MultiplanarOption=SimpleNamespace(IncludeAllMultiplanarCurves="all-multiplanar"))
    collections = {Rebar: [first, other], RebarInSystem: [system], AreaReinforcement: [parent],
        PathReinforcement: [], FabricArea: [], FabricSheet: [], RebarContainer: [], RevitLinkInstance: []}
    state = SimpleNamespace(module=module, DB=DB, doc=doc, floor=floor, elements=elements,
        first=first, system=system, other=other, parent=parent, collections=collections,
        collector_calls=[], disposed=0, worksets=[])

    class Collector:
        def __init__(self, document, *args):
            assert document is doc and not args, "No active-view collector is allowed"

        def OfClass(self, cls):
            self.cls = cls
            state.collector_calls.append(cls)
            return self

        def WhereElementIsNotElementType(self):
            return self

        def __iter__(self):
            return iter(collections[self.cls])

        def Dispose(self):
            state.disposed += 1

    class Worksets:
        def __init__(self, document):
            assert document is doc

        def OfKind(self, kind):
            assert kind == "user"
            return self

        def __iter__(self):
            return iter(state.worksets)

        def Dispose(self):
            pass

    def forbidden(*args, **kwargs):
        pytest.fail("Read-only probe must never mutate Revit or use the test-model collector")

    DB.FilteredElementCollector, DB.FilteredWorksetCollector = Collector, Worksets
    DB.WorksetKind = SimpleNamespace(UserWorkset="user")
    DB.Transaction = DB.TransactionGroup = DB.SubTransaction = forbidden
    doc.Save = doc.SaveAs = doc.SynchronizeWithCentral = forbidden
    monkeypatch.setattr(probe_modules[1], "collect_report", forbidden)
    return state


def collect(state):
    return state.module.collect_working_rebar_report(state.doc, state.DB, state.floor)


def test_reads_all_native_host_positions_once_not_active_view_or_other_host(rebar_api):
    state = rebar_api
    result = collect(state)
    assert result["status"] == "collected", result["read_issues"]
    assert result["schema_version"] == "revit-working-rebar-probe/v1"
    assert result["host_id"] == result["host"]["element_id"] == 11020633
    assert result["host_unique_id"] == state.floor.UniqueId
    assert result["host"]["covers"]["top"]["distance_mm"] == pytest.approx(25)
    assert result["document"]["modification_flag_unchanged"] is True
    assert result["document"]["project_information_unique_id"] == "project-unique-id"
    assert result["summary"]["physical_bar_count"] == 4
    assert result["summary"]["excluded_position_count"] == 2
    assert result["summary"]["host_reinforcement_element_count_read"] == 2
    assert state.first.calls == state.system.calls == [0, 2] and state.other.calls == []
    assert state.disposed == 8 and len(state.collector_calls) == 8
    assert result["parents"][0]["child_ids"] == [71]
    assert result["placement_eligible"] is result["engineering_approval"] is False
    assert result["read_only"] is True
    assert result["summary"]["engineering_roles_classified"] is False
    assert result["scope"]["whole_model_collision_inventory_complete"] is False
    assert json.loads(importlib.import_module("qm_revit_probe").serialize_report_utf8(result))["status"] == "collected"


def test_exact_native_arc_line_and_Z_are_kept_without_extra_transform(rebar_api):
    result = collect(rebar_api)
    bar = result["bars"][0]
    assert bar["bar_type"]["nominal_diameter_mm"] == pytest.approx(18)
    assert bar["materials"]["materials"][0]["name"] == "А500С"
    assert bar["mark"] == "П1" and bar["engineering_role"] == "unclassified"
    assert bar["CreatedPhaseId"] == 10 and bar["phase_origin_mm"] is None
    line = bar["positions"][0]["curves"][0]
    assert line["start_mm"] == pytest.approx([100, 200, 300])
    assert line["end_mm"] == pytest.approx([4000, 200, 300])
    assert line["length_mm"] == pytest.approx(3900)
    arc = bar["positions"][2]["curves"][0]
    assert arc["kind"] == "Arc" and arc["exact_geometry_supported"] is True
    assert arc["center_mm"] == pytest.approx([100, 200, 300])
    assert arc["normal"] == [0, -1, 0]
    assert arc["end_mm"] == pytest.approx([100, 200, 325])
    assert arc["radius_mm"] == pytest.approx(25)
    assert arc["end_parameter"] == pytest.approx(math.pi / 2)
    assert arc["length_mm"] == pytest.approx(25 * math.pi / 2)
    assert len(arc["tessellated_points_mm"]) == 3
    assert bar["positions"][1]["status"] == "excluded" and bar["positions"][1]["curves"] == []


def test_repeated_collectors_do_not_duplicate_physical_UID_positions(rebar_api):
    rebar_api.collections[Rebar].append(rebar_api.first)
    result = collect(rebar_api)
    assert result["status"] == "collected" and result["summary"]["physical_bar_count"] == 4
    assert rebar_api.first.calls == [0, 2]
    keys = [tuple(p["position_key"]) for b in result["bars"] for p in b["positions"]]
    assert len(keys) == len(set(keys))


@pytest.mark.parametrize("change", ("quantity", "empty-curves", "read-throws", "missing-type", "NaN", "unbounded"))
def test_unreadable_positions_or_types_remain_partial_with_other_evidence(rebar_api, change):
    bar = rebar_api.first
    if change == "quantity":
        bar.Quantity = 3
    elif change == "empty-curves":
        bar.curves[0] = []
    elif change == "read-throws":
        bar.DoesBarExistAtPosition = lambda i: (_ for _ in ()).throw(RuntimeError("ошибка чтения"))
    elif change == "missing-type":
        bar.GetTypeId = lambda: Id(99999)
    elif change == "NaN":
        bar.curves[0][0].Length = float("nan")
    else:
        bar.curves[0][0].IsBound = False
    result = collect(rebar_api)
    assert result["status"] == "partial" and result["read_issues"]
    assert result["summary"]["physical_bar_count"] is None
    assert result["summary"]["native_host_inventory_complete"] is False
    assert result["bars"][1]["readback_complete"] is True
    importlib.import_module("qm_revit_probe").serialize_report_utf8(result)


def test_unsupported_spline_is_evidence_not_an_exact_line(rebar_api):
    class Spline:
        IsBound = True
        Length = 10 / 304.8

        def GetType(self):
            return SimpleNamespace(Name="NurbSpline")

        def GetEndPoint(self, index):
            return xyz(index * 10, 0, 0)

        def Evaluate(self, value, normalized):
            return xyz(5, 2, 0)

        def Tessellate(self):
            return [xyz(0, 0, 0), xyz(5, 2, 0), xyz(10, 0, 0)]

    rebar_api.first.curves[0] = [Spline()]
    result = collect(rebar_api)
    row = result["bars"][0]["positions"][0]
    assert result["status"] == "partial" and row["status"] == "read_failed"
    assert row["curves"][0]["exact_geometry_supported"] is False
    assert len(row["curves"][0]["tessellated_points_mm"]) == 3


@pytest.mark.parametrize("limit", ("MAX_SCANNED_ELEMENTS", "MAX_HOST_ELEMENTS", "MAX_TOTAL_POSITIONS",
    "MAX_CURVES", "MAX_TESSELLATION_POINTS", "MAX_POINTS_PER_CURVE"))
def test_limits_never_turn_truncated_inventory_into_complete_zero(rebar_api, monkeypatch, limit):
    monkeypatch.setattr(rebar_api.module, limit, 1)
    result = collect(rebar_api)
    assert result["status"] == "partial" and result["read_issues"]
    assert result["summary"]["physical_bar_count"] is None
    assert result["summary"]["native_host_inventory_complete"] is False


@pytest.mark.parametrize("change", ("missing-parent", "missing-child", "wrong-parent", "empty", "duplicate"))
def test_area_path_parent_children_completeness_and_no_double_count(rebar_api, change):
    if change == "missing-parent":
        rebar_api.collections[AreaReinforcement] = []
    elif change == "missing-child":
        rebar_api.collections[RebarInSystem] = []
    elif change == "wrong-parent":
        rebar_api.system.SystemId = Id(999)
    elif change == "empty":
        rebar_api.parent.GetRebarInSystemIds = lambda: []
    else:
        rebar_api.parent.GetRebarInSystemIds = lambda: [Id(71), Id(71)]
    result = collect(rebar_api)
    assert result["status"] == "partial" and result["summary"]["physical_bar_count"] is None
    assert result["read_issues"]


def test_path_parent_is_also_checked(rebar_api):
    rebar_api.collections[AreaReinforcement] = []
    path = PathReinforcement(80)
    for key in ("WorksetId", "OwnerViewId", "CreatedPhaseId", "DemolishedPhaseId", "GroupId"):
        setattr(path, key, getattr(rebar_api.parent, key))
    rebar_api.collections[PathReinforcement] = [path]
    assert collect(rebar_api)["status"] == "collected"


@pytest.mark.parametrize("closed", (False, True))
def test_workshared_open_worksets_supported_closed_worksets_explicitly_incomplete(rebar_api, closed):
    rebar_api.doc.IsWorkshared = True
    rebar_api.worksets.append(SimpleNamespace(Id=Id(1), UniqueId="workset-1", Name="Арматура", IsOpen=not closed))
    result = collect(rebar_api)
    assert result["status"] == ("partial" if closed else "collected")
    assert result["worksharing"]["user_worksets"][0]["is_open"] is not closed
    assert result["document"]["is_workshared"] is True
    assert rebar_api.worksets[0].IsOpen is not closed
    assert result["document"]["central_model_path"] == "central-visible-path"


@pytest.mark.parametrize("cls", (FabricArea, FabricSheet, RebarContainer))
def test_unsupported_native_host_reinforcement_blocks_completeness(rebar_api, cls):
    item = cls(101)
    rebar_api.collections[cls] = [item]
    result = collect(rebar_api)
    assert result["status"] == "partial"
    assert result["scope"]["unsupported_host_elements"][0]["element_id"] == 101
    assert result["summary"]["physical_bar_count"] is None


def test_fabric_of_other_host_is_not_misreported_as_selected_host_failure(rebar_api):
    sheet = FabricSheet(101)
    sheet.HostId = Id(999)
    rebar_api.collections[FabricSheet] = [sheet]
    result = collect(rebar_api)
    assert result["status"] == "collected"
    assert result["scope"]["unsupported_host_elements"] == []


def test_link_inventory_is_explicitly_outside_native_host_geometry(rebar_api):
    rebar_api.collections[RevitLinkInstance] = [RevitLinkInstance(102, "Связь")]
    result = collect(rebar_api)
    assert result["status"] == "collected"
    assert result["scope"]["links"][0]["is_loaded"] is False
    assert result["scope"]["links_geometry_read"] is False
    assert result["scope"]["whole_model_collision_inventory_complete"] is False


@pytest.mark.parametrize("selection", ("family", "foreign", "not-floor"))
def test_native_project_selection_only(rebar_api, selection):
    if selection == "family":
        rebar_api.doc.IsFamilyDocument = True
    elif selection == "foreign":
        rebar_api.floor.Document = object()
    else:
        rebar_api.floor = rebar_api.first
    with pytest.raises(ValueError):
        collect(rebar_api)


def test_empty_native_host_inventory_is_counted_only_when_every_scope_read_succeeds(rebar_api):
    for cls in (Rebar, RebarInSystem, AreaReinforcement):
        rebar_api.collections[cls] = []
    result = collect(rebar_api)
    assert result["status"] == "collected" and result["summary"]["physical_bar_count"] == 0


def test_collector_host_id_failure_cannot_silently_drop_possible_host_rebar(rebar_api):
    rebar_api.other.GetHostId = lambda: (_ for _ in ()).throw(RuntimeError("неизвестный host"))
    result = collect(rebar_api)
    assert result["status"] == "partial" and result["summary"]["physical_bar_count"] is None
    assert result["summary"]["read_existing_position_count"] == 4


def test_runtime_has_no_mutating_calls_or_nested_comprehension_closures():
    prohibited = {"Transaction", "TransactionGroup", "SubTransaction", "Save", "SaveAs",
        "SynchronizeWithCentral", "CheckoutElements", "CheckoutWorksets", "OpenWorksets",
        "SetActiveWorksetId", "CreateFromCurves", "GetOrCreateRebarHostData", "GetBarPositionTransform",
        "GetMovedBarTransform", "GetShapeDrivenAccessor"}
    for path in (MODULE_PATH, BUTTON):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                assert name not in prohibited
            assert not isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))


def test_python2_grammar_for_runtime_and_button():
    from lib2to3.pgen2 import driver
    from lib2to3 import pygram, pytree
    parser = driver.Driver(pygram.python_grammar, convert=pytree.convert)
    for path in (MODULE_PATH, BUTTON):
        parser.parse_string(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("part", ("cover", "parameter"))
def test_nonfinite_native_metadata_is_partial_but_still_json_serializable(rebar_api, monkeypatch, part):
    if part == "cover":
        rebar_api.elements[5].CoverDistance = float("nan")
    else:
        monkeypatch.setattr(rebar_api.module.Probe, "parameters", lambda p, e: [{"value_internal": float("inf")}])
    result = collect(rebar_api)
    assert result["status"] == "partial" and result["read_issues"]
    importlib.import_module("qm_revit_probe").serialize_report_utf8(result)


def install_ui(state, monkeypatch, tmp_path, *, selected=True, stop=None):
    class Cancelled(Exception):
        pass

    destination = tmp_path / "Отчёт арматуры.json"
    if stop == "existing":
        destination.write_bytes(b"keep existing file")
    picked, alerts = [], []

    def pick(kind, selection_filter, prompt):
        if stop == "floor":
            raise Cancelled()
        assert kind == "Element" and selection_filter.AllowElement(state.floor)
        assert not selection_filter.AllowReference(None, None)
        assert not selection_filter.AllowElement(state.first)
        picked.append(True)
        return SimpleNamespace(ElementId=state.floor.Id)

    def alert(message, **kwargs):
        alerts.append(message)
        return stop != "confirmation"

    uidoc = SimpleNamespace(Selection=SimpleNamespace(
        GetElementIds=lambda: [state.floor.Id] if selected else [], PickObject=pick))
    forms = SimpleNamespace(alert=alert,
        save_file=lambda **kwargs: None if stop == "destination" else str(destination))
    monkeypatch.setitem(sys.modules, "pyrevit", SimpleNamespace(DB=state.DB, forms=forms,
        revit=SimpleNamespace(doc=state.doc, uidoc=uidoc)))
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.Exceptions", SimpleNamespace(OperationCanceledException=Cancelled))
    monkeypatch.setitem(sys.modules, "Autodesk.Revit.UI.Selection", SimpleNamespace(ISelectionFilter=object,
        ObjectType=SimpleNamespace(Element="Element")))
    return destination, picked, alerts


@pytest.mark.parametrize("selected", (True, False))
def test_button_reads_selected_floor_or_native_pick_and_saves_new_unicode_report(rebar_api, monkeypatch, tmp_path, selected):
    destination, picked, alerts = install_ui(rebar_api, monkeypatch, tmp_path, selected=selected)
    runpy.run_path(str(BUTTON), run_name="__main__")
    result = json.loads(destination.read_text(encoding="utf-8"))
    assert result["status"] == "collected" and result["summary"]["physical_bar_count"] == 4
    assert bool(picked) is not selected and len(alerts) == 2


@pytest.mark.parametrize("stop", ("floor", "destination", "confirmation", "existing"))
def test_button_cancel_or_existing_file_never_runs_inventory_or_overwrites(rebar_api, monkeypatch, tmp_path, stop):
    destination, _, _ = install_ui(rebar_api, monkeypatch, tmp_path, selected=False, stop=stop)
    runpy.run_path(str(BUTTON), run_name="__main__")
    assert rebar_api.collector_calls == []
    if stop == "existing":
        assert destination.read_bytes() == b"keep existing file"
    else:
        assert not destination.exists()


def test_partial_probe_is_saved_not_relabelled_as_complete_by_button(rebar_api, monkeypatch, tmp_path):
    rebar_api.first.Quantity = 3
    destination, _, _ = install_ui(rebar_api, monkeypatch, tmp_path)
    runpy.run_path(str(BUTTON), run_name="__main__")
    result = json.loads(destination.read_text(encoding="utf-8"))
    assert result["status"] == "partial" and result["summary"]["physical_bar_count"] is None


@pytest.fixture
def packager():
    spec = importlib.util.spec_from_file_location("working_rebar_package_test", ROOT / "scripts/package_revit_working_rebar_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_standalone_package_contains_only_readonly_button_exact_sources_and_hash_manifest(packager, tmp_path):
    import hashlib
    output = packager.build_package(tmp_path / "working-rebar.zip")
    original = output.read_bytes()
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == set(packager.ARCHIVE_FILES) | {"README.md", "manifest.json"}
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["read_only"] is True and manifest["placement_eligible"] is False
        assert set(manifest["files"]) == set(packager.ARCHIVE_FILES) | {"README.md"}
        for source_name, archive_name in zip(packager.FILES, packager.ARCHIVE_FILES, strict=True):
            assert archive.read(archive_name) == (packager.SOURCE / source_name).read_bytes()
            assert archive_name.startswith("QMonitoringReadOnly.extension/")
        assert any("/QMonitoringReadOnly.tab/" in name for name in archive.namelist())
        for name, digest in manifest["files"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        assert not any(token in name for name in archive.namelist() for token in ("Trial", "Preview", ".rvt", ".dxf"))
        assert "531" in archive.read("README.md").decode("utf-8")
        assert "Старые пути QMonitoring не трогай" in archive.read("README.md").decode("utf-8")
    with pytest.raises(FileExistsError):
        packager.build_package(output)
    assert output.read_bytes() == original


def test_package_does_not_create_output_if_required_dependency_missing(packager, tmp_path, monkeypatch):
    monkeypatch.setattr(packager, "SOURCE", tmp_path / "missing")
    output = tmp_path / "not-created.zip"
    with pytest.raises(FileNotFoundError):
        packager.build_package(output)
    assert not output.exists()
