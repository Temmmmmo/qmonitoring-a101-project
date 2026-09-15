"""TZ 8.1 source-family workflow; API doubles, not a real Revit run."""
import copy
import hashlib
import importlib
import json
import math
from pathlib import Path
from types import SimpleNamespace as NS
from zipfile import ZipFile

import pytest

from test_revit_source_preview import packet


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2]/"integrations/pyrevit/QMonitoring.extension/lib"))
    return importlib.import_module("qm_workflow_81")


def test_source_components_never_use_demand_or_trimmed_party(module):
    source = packet()
    before = copy.deepcopy(source)
    row, = module.source_components(source, "top-Y", (10, 20))
    assert source == before
    assert row["length_mm"] == 1800 and row["width_mm"] == 200
    assert row["center_xy_mm"] == [210, 520]
    assert row["rotation_rad"] == math.pi/2
    assert row["bar_count"] == 2 and row["demand_bbox_mm"] == [0, 0, 1000, 1000]
    assert "НЕ ФИЗИЧЕСКАЯ ПАРТИЯ" in row["annotation"]


@pytest.mark.parametrize("field,value", [("anchorage_diameters", 39), ("minimum_zone_fe_count", 1),
    ("minimum_zone_fe_count", True), ("background_step_mm", 0), ("background_diameter_mm", float("nan")),
    ("algorithm", "invented"), ("mass_preference", 2)])
def test_settings_fail_closed(module, field, value):
    with pytest.raises(ValueError):
        module.settings(**{field: value})


def test_request_does_not_claim_imported_zones_recalculated(module):
    request = module.make_request("a"*64, "top-X", module.settings(),
        {"kind": "DXF", "alignment_confirmed": True}, {"view_id": 42})
    assert request["status"] == "awaiting_calculation"
    assert request["calculation_performed"] is request["placement_eligible"] is False
    with pytest.raises(ValueError):
        module.make_request("a"*64, "top-X", module.settings(), {"kind": "DXF"}, {})


def test_prefix_automatic_only_unique_nonempty(module):
    catalog = [{"id": 1, "family": "А101_Зона"}, {"id": 2, "family": "А101_Марка"}]
    assert module.prefix_candidates(catalog, "а101_з")["automatic_id"] == 1
    assert module.prefix_candidates(catalog, "А101")["automatic_id"] is None
    assert module.prefix_candidates(catalog[:1], "")["automatic_id"] is None
    assert module.prefix_candidates(catalog, "wrong")["matches"] == []


class Id:
    def __init__(self, value):
        self.Value = value


class Parameter:
    def __init__(self, identifier, kind, value=None):
        self.Id = Id(identifier)
        self.IsReadOnly = False
        self.StorageType = {"length_mm": "double", "integer": "integer", "text": "string"}[kind]
        self.Definition = NS(Name="Настоящий параметр "+str(identifier), GetDataType=lambda: kind)
        self.value = value

    def Set(self, value):
        self.value = value
        return True

    def AsDouble(self):
        return self.value

    AsInteger = AsDouble
    AsString = AsDouble


class Point:
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z


class Line:
    def __init__(self, a, b):
        self.points = (a, b)

    def GetEndPoint(self, index):
        return self.points[index]


class View:
    pass


@pytest.fixture
def native(module, monkeypatch):
    n = importlib.import_module("qm_workflow_81_native")
    doc = NS(IsFamilyDocument=False, IsReadOnly=False, IsModifiable=False, IsWorkshared=False,
        Application=NS(VersionNumber="2024"), instances={}, after_commit=None)
    view = View()
    view.Id, view.UniqueId, view.Document = Id(99), "view-99", doc
    view.IsTemplate, view.ViewDirection, view.Origin = False, Point(0, 0, 1), Point(0, 0, 0)
    symbols = {}
    for role, identifier, placement in (("zone", 10, "CurveBasedDetail"), ("annotation", 20, "ViewBased")):
        symbol = NS(Id=Id(identifier), UniqueId="symbol-"+str(identifier), Name=role, IsActive=True,
            Family=NS(Name="Q_"+role, FamilyPlacementType=placement), role=role)
        symbol.Activate = lambda s=symbol: setattr(s, "IsActive", True)
        symbols[identifier] = symbol
    doc.GetElement = lambda identifier: symbols.get(identifier.Value) or doc.instances.get(identifier.Value)
    doc.Regenerate = lambda: None

    class Collector:
        def __init__(self, unused):
            pass

        def OfClass(self, unused):
            return self

        def OfCategory(self, category):
            return [symbol for symbol in symbols.values() if symbol.role == category]

    DB = NS(ElementId=Id, XYZ=Point, Line=NS(CreateBound=Line), ViewPlan=View, FamilySymbol=object,
        BuiltInCategory=NS(OST_DetailComponents="zone", OST_GenericAnnotation="annotation"),
        FilteredElementCollector=Collector, StorageType=NS(Double="double", Integer="integer", String="string"),
        SpecTypeId=NS(Length="length_mm", Boolean=NS(YesNo="yesno")),
        TransactionStatus=NS(Started="started", Committed="committed"))

    def create(location, symbol, target):
        identifier = 100+len(doc.instances)
        instance = NS(Id=Id(identifier), Symbol=symbol, OwnerViewId=target.Id)
        instance.Location = NS(Curve=location) if isinstance(location, Line) else NS(Point=location)
        instance.Parameters = ([Parameter(2, "length_mm"), Parameter(3, "length_mm"), Parameter(4, "length_mm"),
            Parameter(5, "integer"), Parameter(6, "text")] if symbol.role == "zone" else [Parameter(7, "text")])
        doc.instances[identifier] = instance
        return instance
    doc.Create = NS(NewFamilyInstance=create)

    class Scope:
        def __init__(self, group=False):
            self.group, self.status = group, "started"
            self.initial = copy.deepcopy(doc.instances)

        def Commit(self):
            self.status = "committed"
            if doc.after_commit:
                doc.after_commit(doc)
            return self.status

        def GetStatus(self):
            return self.status

        def Assimilate(self):
            self.status = "committed"
            return self.status

        def Dispose(self):
            pass

    def rollback(group, tx, unused):
        doc.instances = group.initial if group is not None else {}
        return True, {"status": "rolled_back"}
    monkeypatch.setattr(n, "_start", lambda *args: (Scope(True), Scope()))
    monkeypatch.setattr(n, "_rollback", rollback)
    selections = {}
    for role, identifier in (("zone", 10), ("annotation", 20)):
        inspected = n.inspect_parameters(doc, DB, view, identifier, role, confirmed=True)
        binding = ({"length_mm": "curve-length", "width_mm": 2, "diameter_mm": 3,
            "step_mm": 4, "bar_count": 5, "source_id": 6} if role == "zone" else {"annotation": 7})
        selections[role] = {"symbol_id": identifier, "inspection": inspected, "binding": binding}
    return NS(n=n, doc=doc, DB=DB, view=view, selections=selections)


def run(h, **kwargs):
    return h.n.place_source_families(h.doc, h.DB, h.view, packet(), "top-Y", h.selections, confirmed=True, **kwargs)


def test_rollback_parameter_inspection_and_complete_family_readback(native):
    assert native.doc.instances == {}
    result = run(native)
    assert result["status"] == "source_view_families_created", result["issues"]
    assert result["readback"]["instance_count"] == 2 and len(native.doc.instances) == 2
    assert result["structural_elements_created"] == 0 and not result["placement_eligible"]


@pytest.mark.parametrize("tamper", [
    lambda doc: doc.instances.clear(),
    lambda doc: setattr(doc.instances[100].Parameters[0], "value", 999),
    lambda doc: setattr(doc.instances[101].Parameters[0], "value", "wrong ID"),
    lambda doc: setattr(doc.instances[100], "OwnerViewId", Id(444)),
    lambda doc: setattr(doc.instances[100].Location.Curve.points[0], "X", 33),
])
def test_post_commit_mismatch_rolls_back_all_instances(native, tamper):
    native.doc.after_commit = tamper
    result = run(native)
    assert result["status"] == "failed_rolled_back"
    assert result["created_instance_ids"] == [] and native.doc.instances == {}


def test_cancel_and_bad_binding_do_not_leave_partial_family_party(native):
    result = run(native, progress=lambda *_: False)
    assert result["status"] == "failed_rolled_back" and native.doc.instances == {}
    native.selections["zone"]["binding"]["source_id"] = 3
    result = run(native)
    assert result["status"] == "blocked_preflight" and native.doc.instances == {}


def test_no_consent_no_native_transaction(native):
    result = native.n.place_source_families(native.doc, native.DB, native.view, packet(), "top-X", native.selections)
    assert result["status"] == "blocked_preflight" and native.doc.instances == {}


def test_no_type_parameter_or_boolean_count_binding(module, native):
    instance = NS(Parameters=[Parameter(1, "length_mm"), Parameter(2, "integer")])
    instance.Parameters[0].IsReadOnly = True
    instance.Parameters[1].Definition.GetDataType = lambda: "yesno"
    assert module.parameter_catalog(instance, native.DB) == []


def test_native_preflight_rejects_central_template_and_nonplan(native, monkeypatch):
    native.view.IsTemplate = True
    with pytest.raises(ValueError, match="horizontal"):
        native.n.preflight(native.doc, native.DB, native.view)
    native.view.IsTemplate = False
    monkeypatch.setattr(native.n, "classify_document", lambda *_: {"status": "blocked", "issues": ["central"]})
    with pytest.raises(ValueError, match="central"):
        native.n.preflight(native.doc, native.DB, native.view)


def test_python2_grammar_and_no_structural_mutations(module):
    from lib2to3.refactor import RefactoringTool
    root = Path(module.__file__).parent
    for name in ("qm_workflow_81.py", "qm_workflow_81_native.py", "qm_workflow_81_transport.py"):
        source = (root/name).read_text()
        RefactoringTool([]).refactor_string(source, name)
        assert "DB.Structure.Rebar" not in source
        assert "CheckoutElements(" not in source
        assert "SynchronizeWithCentral(" not in source


def test_workflow_code_only_archive_is_deterministic_complete_and_separate(module, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2]/"scripts"))
    packager = importlib.import_module("package_revit_workflow")
    first, second = tmp_path/"first.zip", tmp_path/"second.zip"
    packager.build_package(first)
    packager.build_package(second)
    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as archive:
        names = set(archive.namelist())
        assert "QMonitoringWorkflow.extension/QMonitoringWorkflow.tab/Workflow.panel/SourceWorkflow.pushbutton/script.py" in names
        assert not any(name.endswith((".rvt", ".rfa", ".dxf")) for name in names)
        assert not any("Trial.pushbutton" in name for name in names)
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["native_live_test"] == "not_verified"
        assert manifest["placement_eligible"] is False
        assert {record["path"] for record in manifest["files"]} == names-{"manifest.json"}
        for record in manifest["files"]:
            data = archive.read(record["path"])
            assert len(data) == record["bytes"]
            assert hashlib.sha256(data).hexdigest() == record["sha256"]
    with pytest.raises(FileExistsError):
        packager.build_package(first)


@pytest.mark.parametrize("url", ["", "http://example.com", "https://user:secret@example.com", "https://example.com?token=secret", "file:///etc/passwd", "https://example.com#fragment", "https://example.com\nheader"])
def test_transport_rejects_implicit_or_unsafe_server(module, url):
    transport = importlib.import_module("qm_workflow_81_transport")
    with pytest.raises(ValueError):
        transport.server_endpoint(url)


def test_transport_requires_consent_before_dotnet_or_network(module):
    transport = importlib.import_module("qm_workflow_81_transport")
    assert transport.server_endpoint("http://127.0.0.1:8000") == "http://127.0.0.1:8000/api/revit/workflow/analyze"
    with pytest.raises(ValueError, match="consent"):
        transport.post_calculation("https://example.com", b"{}", {})


def test_actual_single_dxf_result_passes_native_adapter_and_rejects_tamper(module, tmp_path):
    from rebar.application.demo import write_demo_dxf
    from rebar.application.revit_workflow import analyze_workflow
    transport = importlib.import_module("qm_workflow_81_transport")
    path = tmp_path/"Нижняя по оси X.dxf"
    write_demo_dxf("irregular-plate-x", path)
    sources = {"dxf": transport.read_source(str(path), ".dxf")}
    request_bytes = transport.calculation_request("bottom-X", module.settings(background_diameter_mm=12, algorithm="bsp"),
        {"background_origin_mm": 0, "first_300_offset_mm": 100, "contact_side": "left", "steel_class": "A500"},
        "plate-zero-d12-v1", sources)
    result = analyze_workflow(path, request_bytes)
    loaded = transport.decode_analysis(json.dumps(result).encode(), request_bytes)
    rows = module.source_components(loaded, "bottom-X")
    assert len(rows) == result["metrics"]["source_zone_count"]
    assert all("СТО-покрытие" in row["annotation"] for row in rows)
    assert sum(row["bar_count"] for row in rows) == result["metrics"]["physical_bar_count"]
    changed = copy.deepcopy(result)
    changed["metrics"]["physical_bar_count"] -= 1
    with pytest.raises(ValueError, match="count"):
        module.source_components(changed, "bottom-X")
    changed = copy.deepcopy(result)
    changed["source"]["cells"].pop()
    with pytest.raises(ValueError, match="counts"):
        module.source_components(changed, "bottom-X")
    changed = copy.deepcopy(result)
    changed["request_sha256"] = "0"*64
    with pytest.raises(ValueError, match="exact"):
        transport.decode_analysis(json.dumps(changed).encode(), request_bytes)
