"""Offline checks with deliberately small API doubles; not a Revit integration run."""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import math
import runpy
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "integrations/pyrevit"
EXTENSION = SOURCE / "QMonitoring.extension"
BUTTON = EXTENSION / "QMonitoring.tab/Diagnostics.panel/ReferenceProbe.pushbutton/script.py"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(EXTENSION / "lib"))
    return (importlib.import_module("qm_probe_geometry"),
            importlib.import_module("qm_revit_probe"))


def line(a, b):
    return {"kind": "Line", "start_mm": list(a), "end_mm": list(b),
            "length_mm": math.dist(a, b), "tessellated_points_mm": [list(a), list(b)]}


def reference_bars():
    return [{"system_id": 123, "position_index": i, "nominal_diameter_mm": 25.0,
             "model_diameter_mm": 25.0,
             "curves": [line((0, 12.5 + i * 96.875, 262.5),
                             (3900, 12.5 + i * 96.875, 262.5))]}
            for i in range(9)]


def boundary():
    points = [(0, 0, 300), (3900, 0, 300), (3900, 800, 300), (0, 800, 300)]
    return [line(a, b) for a, b in zip(points, points[1:] + points[:1])]


def compare(geometry, bars, curves=None):
    measurement = geometry.measure_parallel_bars(bars)
    return geometry.compare_reference(measurement, curves or boundary(), bars,
                                      [{"element_id": 123, "max_spacing_mm": 96.875}], 100.0)


def test_manual_reference_is_measured_without_rounding_or_40d(modules):
    geometry, _ = modules
    bars = reference_bars()
    measurement = geometry.measure_parallel_bars(bars)
    assert measurement["actual_spacings_mm"] == [96.875] * 8
    assert measurement["extreme_axis_distance_mm"] == 775
    assert compare(geometry, bars)["status"] == "matches"


@pytest.mark.parametrize("requested, status", [(100.0, "matches"), (110.0, "differs"),
                                              (None, "not_checked")])
def test_requested_parent_spacing_is_separate_from_child_spacing(modules, requested, status):
    geometry, _ = modules
    bars = reference_bars()
    result = geometry.compare_reference(geometry.measure_parallel_bars(bars), boundary(), bars,
                                        [{"element_id": 123, "max_spacing_mm": 96.875}], requested)
    assert result["status"] == status
    assert not any(c["id"] == "system_123_max_spacing_mm" for c in result["checks"])


def test_received_revit_012_report_regression_when_available(modules):
    """Private original stays local; the synthetic parent/child regression always runs."""
    path = ROOT / "revit_info/settings/qmonitoring-reference-20260907-154624-974000.json"
    if not path.is_file():
        pytest.skip("Private real Revit readback is not distributed")
    geometry, _ = modules
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["status"] == "collected" and report["issues"] == []
    area = report["area"]
    assert area["reference_comparison"]["status"] == "differs"  # Original must stay untouched.
    parent = next(p for p in area["parameters"] if p["parameter_id"] == -1018121)
    assert parent["storage_type"] == "Double"
    assert "reinforcementSpacing" in parent["spec_type_id"]
    requested_mm = parent["value_internal"] * 304.8  # This specific API parameter is feet.
    result = geometry.compare_reference(geometry.measure_parallel_bars(area["physical_bars"]),
        area["boundary_curves"], area["physical_bars"], area["bar_systems"], requested_mm)
    assert result["status"] == "matches"
    assert area["bar_systems"][0]["max_spacing_mm"] == 96.875


@pytest.mark.parametrize("angle", [0.0, math.pi / 2, 0.37, 2.75])
def test_reference_comparison_is_translation_rotation_and_endpoint_order_invariant(modules, angle):
    geometry, _ = modules
    bars, edges = reference_bars(), boundary()

    def transform(p):
        x, y, z = p
        return [x * math.cos(angle) - y * math.sin(angle) + 245678,
                x * math.sin(angle) + y * math.cos(angle) - 3456, z - 6000]

    for curve in edges + [b["curves"][0] for b in bars]:
        curve["start_mm"], curve["end_mm"] = transform(curve["end_mm"]), transform(curve["start_mm"])
    assert compare(geometry, bars[::-1], edges[::-1])["status"] == "matches"


@pytest.mark.parametrize("change", ["exact100", "shift_along", "diameter", "elevation", "missing"])
def test_changed_reference_is_not_a_match(modules, change):
    geometry, _ = modules
    bars = reference_bars()
    if change == "exact100":
        for i, bar in enumerate(bars):
            for key in ("start_mm", "end_mm"):
                bar["curves"][0][key][1] = i * 100
    elif change == "shift_along":
        for key in ("start_mm", "end_mm"):
            bars[0]["curves"][0][key][0] += 100
    elif change == "diameter":
        bars[0]["model_diameter_mm"] = 26
    elif change == "elevation":
        for key in ("start_mm", "end_mm"):
            bars[0]["curves"][0][key][2] += 5
    else:
        bars.pop()
    assert compare(geometry, bars)["status"] == "differs"


@pytest.mark.parametrize("change", ["arc", "diagonal", "duplicate", "sloping", "empty"])
def test_unsupported_geometry_is_not_silently_approximated(modules, change):
    geometry, _ = modules
    bars, edges = reference_bars(), boundary()
    if change == "arc":
        bars[0]["curves"][0]["kind"] = "Arc"
    elif change == "diagonal":
        edges[0] = line((0, 0, 300), (3900, 800, 300))
    elif change == "duplicate":
        edges[-1] = edges[0]
    elif change == "sloping":
        bars[0]["curves"][0]["end_mm"][2] += 10
    else:
        bars = []
    assert compare(geometry, bars, edges)["status"] == "not_checked"


def test_face_depth_is_from_faces_not_absolute_elevation_and_not_containment(modules):
    geometry, _ = modules
    curves = [line((0, 0, -5737.5), (3900, 0, -5737.5))]
    top = [{"plane": {"origin_mm": [0, 0, -5700], "normal": [0, 0, 1]}}]
    bottom = [{"plane": {"origin_mm": [0, 0, -6000], "normal": [0, 0, -1]}}]
    report = geometry.plane_depths(curves, top, 12.5)
    assert report["axis_depth_min_mm"] == 37.5
    assert report["body_clearance_min_mm"] == 25
    assert not report["host_containment_checked"]
    assert geometry.plane_depths(curves, bottom, 12.5)["axis_depth_min_mm"] == 262.5
    assert geometry.plane_depths(curves, top * 2, 12.5)["status"] == "not_checked"


class Id:
    def __init__(self, value):
        self.Value = value

    @property
    def IntegerValue(self):
        raise AssertionError("Do not downcast Revit 2024 64-bit IDs")


class XYZ:
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z


class Transform:
    Origin = XYZ(1, 2, 3)
    BasisX, BasisY, BasisZ = XYZ(0, 1, 0), XYZ(-1, 0, 0), XYZ(0, 0, 1)
    Determinant, IsConformal = 1, True

    def OfPoint(self, p):
        return XYZ(1 - p.Y, 2 + p.X, 3 + p.Z)


class Curve:
    def __init__(self, data):
        self.data = data
        self.Length = data["length_mm"] / 304.8

    def GetType(self):
        return SimpleNamespace(Name=self.data["kind"])

    def GetEndPoint(self, index):
        return XYZ(*(x / 304.8 for x in self.data["start_mm" if index == 0 else "end_mm"]))

    def Tessellate(self):
        return [self.GetEndPoint(0), self.GetEndPoint(1)]


class Element:
    @property
    def Name(self):
        return self.name

    def __init__(self, number, name="element", mark=None):
        self.Id, self.UniqueId, self.name, self.mark = Id(number), "uid-" + str(number), name, mark
        self.Parameters = []

    def GetType(self):
        return SimpleNamespace(FullName=type(self).__name__)

    def get_Parameter(self, key):
        if key == "mark":
            return SimpleNamespace(AsString=lambda: self.mark)
        return SimpleNamespace(HasValue=True, AsElementId=lambda: Id(5))

    def GetTypeId(self):
        return Id(4)

    def get_BoundingBox(self, view):
        return SimpleNamespace(Min=XYZ(0, 0, 0), Max=XYZ(10, 20, 1), Transform=Transform())


def test_element_name_with_real_getset_descriptor_reproduces_reported_error(modules):
    _, adapter = modules

    class NameOwner(type):
        Name = type.__dict__["__name__"]

    class ТипАрматуры(metaclass=NameOwner):
        pass

    assert type(NameOwner.Name).__name__ == "getset_descriptor"
    with pytest.raises(AttributeError, match="getset_descriptor.*GetValue"):
        NameOwner.Name.GetValue(ТипАрматуры)
    assert adapter.element_name(ТипАрматуры, SimpleNamespace(Element=NameOwner)) == "ТипАрматуры"


def test_element_name_reads_base_descriptor_when_derived_getter_is_hidden(modules):
    _, adapter = modules

    class SetterOnlyElement(Element):
        Name = property(fset=lambda self, value: pytest.fail("Must not call a setter"))

    element = SetterOnlyElement(42, "25 А500")
    with pytest.raises(AttributeError):
        _ = element.Name
    assert adapter.element_name(element, SimpleNamespace(Element=Element)) == "25 А500"


def test_element_name_supports_legacy_getvalue_only_as_fallback(modules):
    _, adapter = modules

    class HiddenName:
        @property
        def Name(self):
            raise AttributeError("Inherited getter hidden")

    DB = SimpleNamespace(Element=SimpleNamespace(Name=SimpleNamespace(GetValue=lambda e: "Тип 300мм")))
    assert adapter.element_name(HiddenName(), DB) == "Тип 300мм"


def test_element_name_does_not_swallow_api_failures_or_invent_missing_names(modules):
    _, adapter = modules

    class InvalidElement:
        @property
        def Name(self):
            raise RuntimeError("Revit object is invalid")

    DB = SimpleNamespace(Element=Element)
    with pytest.raises(RuntimeError, match="Revit object is invalid"):
        adapter.element_name(InvalidElement(), DB)
    with pytest.raises(ValueError, match="missing element"):
        adapter.element_name(None, DB)
    with pytest.raises(AttributeError):
        adapter.element_name(object(), SimpleNamespace(Element=SimpleNamespace(Name=object())))


@pytest.mark.parametrize("name", ["", "Плита 300мм", "Верхнее армирование вдоль ОСИ Х.dxf"])
def test_element_name_preserves_unicode_and_empty_names(modules, name):
    _, adapter = modules
    assert adapter.element_name(Element(42, name), SimpleNamespace(Element=Element)) == name


class Floor(Element):
    LevelId = Id(6)

    def GetGeometryObjectFromReference(self, ref):
        return Face(ref)


class Face:
    def __init__(self, top):
        self.Origin = XYZ(0, 0, (300 if top else 0) / 304.8)
        self.FaceNormal = XYZ(0, 0, 1 if top else -1)
        self.EdgeLoops = [[SimpleNamespace(AsCurve=lambda c=c: Curve(c)) for c in boundary()]]


class Area(Element):
    def get_Parameter(self, key):
        if key == "top_major_spacing":
            return SimpleNamespace(AsDouble=lambda: 100 / 304.8)
        return super().get_Parameter(key)

    def GetHostId(self):
        return Id(407801)

    def GetBoundaryCurveIds(self):
        return [Id(i) for i in range(10, 14)]

    def GetRebarInSystemIds(self):
        return [Id(123)]


class System(Element):
    NumberOfBarPositions = Quantity = 9
    LayoutRule = "MaximumSpacing"
    MaxSpacing = 96.875 / 304.8
    SystemId = Id(407878)

    def __init__(self):
        super().__init__(123)
        self.excluded = set()
        self.read_indices = []

    def GetHostId(self):
        return Id(407801)

    def DoesBarExistAtPosition(self, index):
        return index not in self.excluded

    def GetTypeId(self):
        return Id(165163)

    def GetTransformedCenterlineCurves(self, adjust, hooks, bends, index):
        assert (adjust, hooks, bends) == (False, False, False)
        self.read_indices.append(index)
        return [Curve(reference_bars()[index]["curves"][0])]

    def GetBarPositionTransform(self, index):
        raise AssertionError("The final centerline must not be transformed twice")


class Cad(Element):
    IsLinked = False
    OwnerViewId = Id(-1)

    def GetTransform(self):
        return Transform()

    def GetTotalTransform(self):
        return Transform()


@pytest.fixture
def api():
    elements = {407801: Floor(407801, mark="TEST_SLAB_01"),
                407878: Area(407878, mark="AR_TEST_TOP_X_001"),
                123: System(), 4: Element(4, "Тип"), 5: Element(5, "25 мм"),
                6: Element(6, "Уровень 1"), 9: Cad(9)}
    elements[5].CoverDistance = 25 / 304.8
    for number, diameter in ((165160, 18), (165161, 20), (165163, 25)):
        elements[number] = Element(number, str(diameter) + " A500")
        elements[number].BarNominalDiameter = diameter / 304.8
        elements[number].BarModelDiameter = diameter / 304.8
    for i, curve in enumerate(boundary(), 10):
        elements[i] = SimpleNamespace(Curve=Curve(curve))
    DB = SimpleNamespace(ElementId=Id, Floor=Floor, Element=Element, XYZ=XYZ, PlanarFace=Face,
        Structure=SimpleNamespace(AreaReinforcement=Area),
        UnitUtils=SimpleNamespace(ConvertFromInternalUnits=lambda v, unit: v * 304.8),
        UnitTypeId=SimpleNamespace(Millimeters="mm"), SpecTypeId=SimpleNamespace(Length="length"),
        BuiltInParameter=SimpleNamespace(ALL_MODEL_MARK="mark", CLEAR_COVER_TOP="top",
                                        CLEAR_COVER_BOTTOM="bottom", CLEAR_COVER_OTHER="other",
                                        REBAR_SYSTEM_SPACING_TOP_DIR_1="top_major_spacing"),
        HostObjectUtils=SimpleNamespace(GetTopFaces=lambda f: [True], GetBottomFaces=lambda f: [False]))
    doc = SimpleNamespace(Title="Копия эталона", IsWorkshared=False, IsFamilyDocument=False,
        Application=SimpleNamespace(VersionName="Revit", VersionNumber="2024",
                                    VersionBuild="20250918_1515(x64)", SubVersionNumber="2024.3.4"),
        GetElement=lambda i: elements.get(i.Value))
    return DB, doc, elements


def test_collector_reads_actual_positions_and_exports_json_without_revit_mutations(modules, api):
    _, adapter = modules
    DB, doc, elements = api
    report = adapter.collect_report(doc, DB, elements[9])
    assert report["status"] == "collected"
    assert report["area"]["physical_bar_count"] == 9
    assert report["area"]["reference_comparison"]["status"] == "matches"
    assert report["floor"]["covers"]["top"]["distance_mm"] == pytest.approx(25)
    assert len(report["floor"]["top_faces"][0]["edge_loops"][0]) == 4
    assert report["area"]["physical_bars"][0]["top_face_depths"]["axis_depth_min_mm"] == 37.5
    assert elements[123].read_indices == list(range(9))
    assert not report["placement_eligible"]
    assert report["cad"]["coordinate_calibration"] == "not_checked"
    assert report["cad"]["total_transform"]["basis_x"] == [0, 1, 0]
    assert report["cad"]["total_transform"]["origin_mm"] == pytest.approx([304.8, 609.6, 914.4])
    json.dumps(report, ensure_ascii=False, allow_nan=False)


def test_excluded_position_is_not_a_physical_bar(modules, api):
    _, adapter = modules
    DB, doc, elements = api
    elements[123].excluded = {4}
    elements[123].Quantity = 8
    report = adapter.collect_report(doc, DB, elements[9])
    assert report["area"]["physical_bar_count"] == 8
    assert 4 not in elements[123].read_indices
    assert report["area"]["reference_comparison"]["status"] == "differs"


@pytest.mark.parametrize("failure", ["method_missing", "empty_curves", "quantity", "limit", "no_systems"])
def test_incomplete_readback_never_claims_success_or_uses_nominal_spacing(modules, api, failure):
    _, adapter = modules
    DB, doc, elements = api
    if failure == "method_missing":
        elements[123].GetTransformedCenterlineCurves = None
    elif failure == "empty_curves":
        elements[123].GetTransformedCenterlineCurves = lambda *args: []
    elif failure == "quantity":
        elements[123].Quantity = 10
    elif failure == "limit":
        elements[123].NumberOfBarPositions = 10001
    else:
        elements[407878].GetRebarInSystemIds = lambda: []
    report = adapter.collect_report(doc, DB, elements[9])
    assert report["status"] == "partial"
    assert report["area"]["physical_bar_count"] is None
    assert report["area"]["reference_comparison"]["status"] == "not_checked"
    assert not report["placement_eligible"]


@pytest.mark.parametrize("failure", ["floor_mark", "area_mark", "wrong_host", "missing_floor"])
def test_other_model_or_replaced_reference_is_not_silently_accepted(modules, api, failure):
    _, adapter = modules
    DB, doc, elements = api
    if failure == "floor_mark":
        elements[407801].mark = "Something else"
    elif failure == "area_mark":
        elements[407878].mark = "Something else"
    elif failure == "wrong_host":
        elements[407878].GetHostId = lambda: Id(99)
    else:
        del elements[407801]
    report = adapter.collect_report(doc, DB, elements[9])
    assert report["status"] == "partial"
    assert report["area"] is None
    assert not elements[123].read_indices


def test_no_cad_still_produces_useful_partial_report(modules, api):
    _, adapter = modules
    DB, doc, _ = api
    report = adapter.collect_report(doc, DB)
    assert report["status"] == "partial"
    assert report["cad"] is None
    assert report["area"]["reference_comparison"]["status"] == "matches"


def test_json_serializer_bypasses_ascii_encoder_and_preserves_text_and_values(modules, monkeypatch):
    _, adapter = modules
    report = {"плита": {"parameters": [{"display_value": "12345678 см²", "type": "Ø25 × 3900 мм"}]},
              "issues": [{"message": 'Ошибка «слой»\n\t\\"\x00 🏗'}],
              "metrics": [96.875, 9, 2**40, True, False, None, ""]}

    def broken_ascii_encoder(value):
        raise AssertionError("Do not use the IronPython-incompatible ASCII encoder")

    monkeypatch.setattr(json.encoder, "encode_basestring_ascii", broken_ascii_encoder)
    encoded = adapter.serialize_report_utf8(report)
    assert isinstance(encoded, bytes)
    assert "см²" in encoded.decode("utf-8")
    assert not encoded.startswith(b"\xef\xbb\xbf")
    assert json.loads(encoded.decode("utf-8")) == report


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf"), object()])
def test_invalid_report_does_not_silently_stringify_or_leave_partial_file(modules, tmp_path, invalid):
    _, adapter = modules
    destination = tmp_path / "отчёт.json"
    with pytest.raises((TypeError, ValueError)):
        adapter.write_report_json(str(destination), {"value": invalid})
    assert not destination.exists()


def test_circular_report_is_rejected_before_opening_a_file(modules, tmp_path):
    _, adapter = modules
    report = {"value": []}
    report["value"].append(report)
    destination = tmp_path / "отчёт.json"
    with pytest.raises(ValueError, match="Circular"):
        adapter.write_report_json(str(destination), report)
    assert not destination.exists()


def test_unicode_json_write_preserves_existing_files(modules, tmp_path):
    _, adapter = modules
    destination = tmp_path / "Проверка см².json"
    report = {"name": "Плита Ø25", "issues": [{"message": "Слой не найден"}]}
    adapter.write_report_json(str(destination), report)
    assert json.loads(destination.read_text(encoding="utf-8")) == report
    with pytest.raises(OSError):
        adapter.write_report_json(str(destination), {"replace": True})
    assert json.loads(destination.read_text(encoding="utf-8")) == report


def test_runtime_smoke_script_also_runs_in_the_regular_test_suite():
    import subprocess
    import sys

    result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_revit_probe_runtime.py"),
                             "--lib-dir", str(EXTENSION / "lib")],
                            capture_output=True, text=True, check=True)
    assert "PASS: 9 serialization/file checks" in result.stdout
    assert "PASS: 6 JSON checks" in result.stdout
    assert "8 Python files compiled" in result.stdout


def test_parameter_double_units_are_not_all_treated_as_lengths(modules, api):
    _, adapter = modules
    DB, doc, elements = api

    def parameter(number, spec):
        return SimpleNamespace(Id=Id(number), StorageType="Double",
            Definition=SimpleNamespace(Name="Значение", GetDataType=lambda: SimpleNamespace(
                TypeId=spec, Equals=lambda other: other == spec)),
            AsValueString=lambda: "1", AsDouble=lambda: 1.0)

    elements[123].Parameters = [parameter(-1, "length"), parameter(-2, "area"), parameter(1, "length")]
    probe = adapter.Probe(doc, DB)
    parameters = probe.parameters(elements[123])
    assert len(parameters) == 2
    assert "length_mm" not in parameters[0]
    assert parameters[1]["length_mm"] == 304.8
    assert not probe.issues


def test_full_bounding_box_transform_and_64_bit_ids(modules, api):
    _, adapter = modules
    DB, doc, elements = api
    assert adapter.element_id(Id(2**40)) == 2**40
    box = adapter.Probe(doc, DB).bbox(elements[9])
    assert box["min_mm"] == pytest.approx([-19 * 304.8, 2 * 304.8, 3 * 304.8])
    assert box["max_mm"] == pytest.approx([304.8, 12 * 304.8, 4 * 304.8])


def test_probe_has_no_model_mutation_or_network_calls():
    forbidden = {"Transaction", "SubTransaction", "TransactionGroup", "Regenerate", "Save", "SaveAs",
                 "Delete", "Create", "Set", "SetValueString", "MoveElement", "RotateElement"}
    # The original diagnostic button and its entire import closure stay read-only.
    sources = [BUTTON, EXTENSION / "lib/qm_probe_geometry.py", EXTENSION / "lib/qm_revit_probe.py"]
    for source in sources:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                function = node.func
                name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", "")
                assert name not in forbidden, (source, name)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module]
                assert not any(n.split(".")[0] in {"requests", "socket", "urllib", "http", "subprocess"}
                               for n in names)
            assert not isinstance(node, (ast.JoinedStr, ast.AnnAssign, ast.AsyncFunctionDef))
            if isinstance(node, ast.FunctionDef):
                assert node.returns is None
                assert not node.args.kwonlyargs
                assert all(a.annotation is None for a in node.args.args)


@pytest.mark.parametrize("destination_kind", ["new", "existing", "rvt", "cancel"])
def test_pyrevit_entry_point_writes_only_a_new_json(modules, api, tmp_path, monkeypatch, destination_kind):
    DB, doc, elements = api
    destination = tmp_path / ("keep.rvt" if destination_kind == "rvt" else "report.json")
    if destination_kind in {"existing", "rvt"}:
        destination.write_bytes(b"must not change")

    class Collector:
        def __init__(self, doc):
            pass

        def OfClass(self, cls):
            return self

        def WhereElementIsNotElementType(self):
            return [elements[9]]

    DB.FilteredElementCollector = Collector
    DB.ImportInstance = Cad
    forms = SimpleNamespace(alert=lambda *args, **kwargs: True,
        SelectFromList=SimpleNamespace(show=lambda values, **kwargs: values[0]),
        save_file=lambda **kwargs: None if destination_kind == "cancel" else str(destination))
    fake_pyrevit = SimpleNamespace(DB=DB, revit=SimpleNamespace(doc=doc), forms=forms,
                                  script=SimpleNamespace(get_output=lambda: SimpleNamespace(print_md=print)))
    monkeypatch.setitem(__import__("sys").modules, "pyrevit", fake_pyrevit)
    runpy.run_path(str(BUTTON))
    if destination_kind == "new":
        report = json.loads(destination.read_text())
        assert report["area"]["reference_comparison"]["status"] == "matches"
    elif destination_kind == "cancel":
        assert not destination.exists()
    else:
        assert destination.read_bytes() == b"must not change"


def test_package_contains_only_the_delivery_allowlist_and_refuses_overwrite(tmp_path):
    spec = importlib.util.spec_from_file_location("package_probe", ROOT / "scripts/package_revit_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = module.build_package(tmp_path / "probe.zip")
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == set(module.FILES)
        assert archive.testzip() is None
        assert archive.read("README.md") == (SOURCE / "README.md").read_bytes()
        assert all(Path(n).suffix in {".py", ".yaml", ".md"} or n == "samples/single-zone-trial.json"
                   for n in archive.namelist())
    with pytest.raises(FileExistsError):
        module.build_package(output)
