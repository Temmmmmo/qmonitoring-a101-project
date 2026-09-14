"""Offline API doubles test safety/control flow, not real Revit geometry creation."""
from __future__ import annotations

import ast
import copy
import importlib
import json
import math
import runpy
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from test_revit_probe import (
    EXTENSION,
    Curve,
    Element,
    Id,
    XYZ,
    api as api,
    line,
    modules as modules,
)

BUTTON = EXTENSION / "QMonitoring.tab/Diagnostics.panel/CreationTrial.pushbutton/script.py"
JSON_BUTTON = EXTENSION / "QMonitoring.tab/Diagnostics.panel/JsonTrial.pushbutton/script.py"
SAMPLE = EXTENSION.parent / "samples/single-zone-trial.json"


@pytest.fixture
def trial(request):
    request.getfixturevalue("modules")
    return importlib.import_module("qm_revit_trial"), importlib.import_module("qm_trial_geometry")


@pytest.fixture
def json_input(trial):
    return importlib.import_module("qm_trial_input").load_trial_input(str(SAMPLE))


def prism_faces(lo=(0, 0, -300), hi=(23600, 14000, 0)):
    faces = []
    for axis in range(3):
        other = [i for i in range(3) if i != axis]
        for side in (0, 1):
            points = []
            for u, v in ((0, 0), (1, 0), (1, 1), (0, 1)):
                p = list(lo)
                p[axis] = (lo, hi)[side][axis]
                p[other[0]] = (lo, hi)[u][other[0]]
                p[other[1]] = (lo, hi)[v][other[1]]
                points.append(p)
            normal = [0, 0, 0]
            normal[axis] = 1 if side else -1
            faces.append({"plane": {"origin_mm": points[0], "normal": normal}, "edge_loops": [
                [line(a, b) for a, b in zip(points, points[1:] + points[:1])]]})
    return faces


def floor_geometry():
    faces = prism_faces()
    floor = {"element_id": 407801, "bbox_mm": {"min_mm": [0, 0, -300], "max_mm": [23600, 14000, 0]},
             "top_faces": [faces[5]], "bottom_faces": [faces[4]],
             "covers": {side: {"distance_mm": 25} for side in ("top", "bottom", "other")}}
    solid = {"faces": faces, "volume_mm3": 23600 * 14000 * 300}
    return floor, solid


def bar_type_data():
    return {"element_id": 165163, "nominal_diameter_mm": 25, "model_diameter_mm": 25}


def expected_readback(plan):
    return {"host_id": plan["host_id"], "quantity": 9, "number_of_bar_positions": 9,
            "bar_type": bar_type_data(), "layout_rule": "NumberWithSpacing", "hook_type_ids": [-1, -1],
            "bars": [{"curves": [line(a["start_mm"], a["end_mm"])]} for a in plan["axes"]]}


def test_plan_from_host_face_and_covers_without_dxf_z_or_extra_anchorage(trial):
    _, geometry = trial
    floor, solid = floor_geometry()
    assert geometry.validate_prism(floor, solid)["status"] == "passed"
    plan = geometry.make_trial_plan(floor, bar_type_data())
    assert plan["axes"][0]["start_mm"] == [1000, 1012.5, -37.5]
    assert plan["axes"][-1]["end_mm"] == [4900, 1787.5, -37.5]
    assert plan["anchorage_added_mm"] == 0
    assert not plan["placement_eligible"]
    assert geometry.compare_trial(plan, expected_readback(plan))["status"] == "matches"


@pytest.mark.parametrize("problem", ["dimensions", "cover", "extra_face", "inner_loop", "curved",
                                     "slope", "duplicate", "volume", "edge_plane", "face_extent"])
def test_non_prismatic_or_changed_host_is_rejected(trial, problem):
    _, geometry = trial
    floor, solid = floor_geometry()
    if problem == "dimensions":
        floor["bbox_mm"]["max_mm"][0] += 100
    elif problem == "cover":
        floor["covers"]["other"]["distance_mm"] = 30
    elif problem == "extra_face":
        solid["faces"].append(copy.deepcopy(solid["faces"][0]))
    elif problem == "inner_loop":
        solid["faces"][0]["edge_loops"] *= 2
    elif problem == "curved":
        solid["faces"][0]["plane"] = None
    elif problem == "slope":
        solid["faces"][0]["plane"]["normal"] = [-0.9, 0, 0.1]
    elif problem == "duplicate":
        solid["faces"][1] = solid["faces"][0]
    elif problem == "volume":
        solid["volume_mm3"] *= 0.99
    elif problem == "edge_plane":
        solid["faces"][0]["edge_loops"][0][0]["start_mm"][0] = 10
    elif problem == "face_extent":
        solid["faces"][5]["edge_loops"][0] = [
            line((0, 0, 0), (100, 0, 0)), line((100, 0, 0), (100, 100, 0)),
            line((100, 100, 0), (0, 100, 0)), line((0, 100, 0), (0, 0, 0))]
    with pytest.raises(ValueError):
        geometry.validate_prism(floor, solid)


@pytest.mark.parametrize("change", ["x", "y", "z", "shortened", "spacing", "excluded", "arc",
                                    "hook", "type", "diameter", "host", "layout"])
def test_readback_checks_actual_endpoints_and_semantics(trial, change):
    _, geometry = trial
    plan = geometry.make_trial_plan(floor_geometry()[0], bar_type_data())
    data = expected_readback(plan)
    if change in ("x", "y", "z"):
        for bar in data["bars"]:
            for key in ("start_mm", "end_mm"):
                bar["curves"][0][key]["xyz".index(change)] += 10
    elif change == "shortened":
        data["bars"][0]["curves"][0]["end_mm"][0] -= 100  # Even if API Length still says 3900.
    elif change == "spacing":
        data["bars"][1]["curves"][0]["start_mm"][1] += 3.125
    elif change == "excluded":
        data["bars"].pop()
    elif change == "arc":
        data["bars"][0]["curves"][0]["kind"] = "Arc"
    elif change == "hook":
        data["hook_type_ids"][0] = 10
    elif change == "type":
        data["bar_type"]["element_id"] = 1
    elif change == "diameter":
        data["bar_type"]["model_diameter_mm"] = 26
    elif change == "host":
        data["host_id"] = 1
    elif change == "layout":
        data["layout_rule"] = "MaximumSpacing"
    assert geometry.compare_trial(plan, data)["status"] == "differs"


def test_reversed_curve_and_position_order_is_accepted(trial):
    _, geometry = trial
    plan = geometry.make_trial_plan(floor_geometry()[0], bar_type_data())
    data = expected_readback(plan)
    data["bars"].reverse()
    for bar in data["bars"]:
        curve = bar["curves"][0]
        curve["start_mm"], curve["end_mm"] = curve["end_mm"], curve["start_mm"]
    assert geometry.compare_trial(plan, data)["status"] == "matches"


class CurveList(list):
    def Add(self, item):
        self.append(item)


@pytest.fixture
def trial_api(request, trial):
    adapter, _ = trial
    DB, doc, elements = request.getfixturevalue("api")
    calls = []
    controls = SimpleNamespace(fail=None, obstacles=[], links=[], group_open=False, committed=False, create_count=0)
    floor_data, solid_data = floor_geometry()

    class TrialFace:
        def __init__(self, face):
            self.Origin = XYZ(*(v / 304.8 for v in face["plane"]["origin_mm"]))
            self.FaceNormal = XYZ(*face["plane"]["normal"])
            self.EdgeLoops = [[SimpleNamespace(AsCurve=lambda c=c: Curve(c)) for c in loop]
                              for loop in face["edge_loops"]]

    class Solid:
        Volume = solid_data["volume_mm3"] / 304.8 ** 3
        Faces = [TrialFace(f) for f in solid_data["faces"]]

    class Link(Element):
        pass

    class Sketch(Element):
        OwnerId = Id(407801)

    class View(Element):
        pass

    class BarType(Element):
        pass

    class NewRebar(Element):
        Quantity = NumberOfBarPositions = 9
        LayoutRule = "NumberWithSpacing"

        @staticmethod
        def CreateFromCurves(document, style, bar_type, hook_a, hook_b, floor,
                             normal, curves, orient_a, orient_b, use_shape, new_shape):
            calls.append("create")
            assert doc.IsModifiable
            assert hook_a is hook_b is None
            assert floor.Id.Value == 407801 and bar_type.Id.Value in (165163, 165160)
            assert [normal.X, normal.Y, normal.Z] == [0, 1, 0]
            number = 800001 + controls.create_count * 2
            controls.create_count += 1
            result = NewRebar(number, "Временная арматура")
            result.type_id = bar_type.Id.Value
            result.first = curves[0].data
            result.indices = []
            elements[number] = result
            elements[number + 1] = Element(number + 1, "temporary shape")
            if controls.fail == "create" or (controls.fail == "second_create" and controls.create_count == 2):
                raise RuntimeError("Creation failed after partial mutation")
            return result

        def GetTypeId(self):
            return Id(self.type_id)

        def GetHostId(self):
            return Id(407801)

        def GetHookTypeId(self, end):
            return Id(-1)

        def GetShapeDrivenAccessor(self):
            def configure(count, spacing, normal_side, first, last):
                calls.append("layout")
                self.Quantity = self.NumberOfBarPositions = count
                assert normal_side is first is last is True
                self.spacing = spacing * 304.8
                if controls.fail == "layout" or (controls.fail == "second_layout" and controls.create_count == 2):
                    raise RuntimeError("Layout failed")
            return SimpleNamespace(SetLayoutAsNumberWithSpacing=configure, Dispose=lambda: None)

        def DoesBarExistAtPosition(self, index):
            return not (controls.fail == "excluded" and index == self.NumberOfBarPositions - 1)

        def GetTransformedCenterlineCurves(self, adjust, hooks, bends, multiplanar, index):
            assert (adjust, hooks, bends, multiplanar) == (False, False, False, "all")
            calls.append("read_axis")
            self.indices.append(index)
            if controls.fail == "readback" and index == min(4, self.NumberOfBarPositions - 1):
                raise RuntimeError("Readback failed")
            a, b = (list(self.first[k]) for k in ("start_mm", "end_mm"))
            for p in (a, b):
                p[1] += index * self.spacing
                if controls.fail == "shift" or (controls.fail == "post_commit_shift" and controls.committed):
                    p[0] += 10
                if controls.fail == "post_commit_second_shift" and controls.committed and self.Id.Value == 800003:
                    p[1] -= 100
            if controls.fail == "post_commit_readback" and controls.committed:
                raise RuntimeError("Post-Commit read failed")
            return [Curve(line(a, b))]

        def GetBarPositionTransform(self, index):
            pytest.fail("Never transform final curves again")

    class Collector:
        def __init__(self, document):
            self.cls = None
            self.filtered = False
            self.type_filter = None

        def OfClass(self, cls):
            self.cls = cls
            return self

        def WhereElementIsNotElementType(self):
            self.type_filter = False
            return self

        def WhereElementIsElementType(self):
            self.type_filter = True
            return self

        def WherePasses(self, filter):
            self.filtered = True
            return self

        def ToElementIds(self):
            assert self.type_filter is not None, "A real Revit collector requires a filter"
            return [Id(i) for i in elements]

        def __iter__(self):
            if self.cls is Link:
                return iter(controls.links)
            if self.filtered:
                return iter([elements[407801], elements[9]] + controls.obstacles)
            return iter(elements.values())

    class Options:
        def SetClearAfterRollback(self, value):
            assert value is True
            return self

        def SetForcedModalHandling(self, value):
            assert value is True
            return self

        def SetFailuresPreprocessor(self, value):
            self.recorder = value
            return self

    class Transaction:
        def __init__(self, document, name):
            self.status = "Uninitialized"
            self.original = dict(elements)

        def Start(self):
            calls.append("start")
            self.status = "Started"
            doc.IsModifiable = True
            return self.status

        def GetStatus(self):
            return self.status

        def GetFailureHandlingOptions(self):
            return Options()

        def SetFailureHandlingOptions(self, options):
            self.options = options

        def RollBack(self):
            calls.append("rollback")
            if controls.fail == "rollback":
                raise RuntimeError("Rollback failed")
            if controls.fail == "pending":
                self.status = "Pending"
                return self.status
            if controls.fail != "leaked_element":
                elements.clear()
                elements.update(self.original)
            if controls.fail == "changed_reference":
                elements[407801].mark = "changed unexpectedly"
            self.status = "RolledBack"
            doc.IsModifiable = False
            return self.status

        def Dispose(self):
            calls.append("dispose")

        def Commit(self):
            assert controls.group_open, "Commit without an outer rollback group is forbidden"
            calls.append("commit")
            if controls.fail == "commit_exception":
                raise RuntimeError("Commit failed")
            if controls.fail == "commit_pending":
                self.status = "Pending"
                return self.status
            if controls.fail in ("warning", "error"):
                message = SimpleNamespace(GetSeverity=lambda: controls.fail,
                    GetDescriptionText=lambda: "Ошибки теста", GetFailingElementIds=lambda: [Id(800001)])
                result = self.options.recorder.PreprocessFailures(SimpleNamespace(GetFailureMessages=lambda: [message]))
                assert result == "rollback"
                return self.RollBack()
            if controls.fail == "commit_rollback":
                return self.RollBack()
            self.status = "Committed"
            controls.committed = True
            doc.IsModifiable = False
            if controls.fail == "post_commit_missing":
                elements.pop(800001)
            return self.status

    class TransactionGroup:
        def __init__(self, document, name):
            self.original = dict(elements)
            self.status = "Uninitialized"

        def Start(self):
            calls.append("group_start")
            assert self.IsFailureHandlingForcedModal is True
            controls.group_open = True
            self.status = "Started"
            return self.status

        def GetStatus(self):
            return self.status

        def RollBack(self):
            calls.append("group_rollback")
            assert not doc.IsModifiable
            if controls.fail == "group_rollback":
                raise RuntimeError("Group rollback failed")
            if controls.fail != "group_leak":
                elements.clear()
                elements.update(self.original)
            self.status = "RolledBack"
            controls.group_open = False
            return self.status

        def Dispose(self):
            calls.append("group_dispose")

        def Commit(self):
            pytest.fail("Outer group must NEVER commit")

        def Assimilate(self):
            pytest.fail("Outer group must NEVER assimilate")

    identity_transform = SimpleNamespace(OfPoint=lambda p: p)
    elements[407801].get_BoundingBox = lambda view: SimpleNamespace(
        Min=XYZ(0, 0, -300 / 304.8), Max=XYZ(23600 / 304.8, 14000 / 304.8, 0),
        Transform=identity_transform)
    elements[407801].get_Geometry = lambda options: [Solid()]
    elements[407801].GetGeometryObjectFromReference = lambda top: TrialFace(solid_data["faces"][5 if top else 4])
    # Move the synthetic manual reference well away from the planned test window.
    for number in range(10, 14):
        data = elements[number].Curve.data
        for key in ("start_mm", "end_mm"):
            data[key] = [data[key][0] + 18500, data[key][1] + 12000, data[key][2] - 300]
    old_curves = elements[123].GetTransformedCenterlineCurves

    def translated_curves(*args):
        curves = old_curves(*args)
        for curve in curves:
            for key in ("start_mm", "end_mm"):
                p = curve.data[key]
                curve.data[key] = [p[0] + 18500, p[1] + 12000, p[2] - 300]
        return curves

    elements[123].GetTransformedCenterlineCurves = translated_curves
    elements[407878].Parameters = [SimpleNamespace(
        Id=Id(number), StorageType="Integer", Definition=SimpleNamespace(Name="layer"),
        AsValueString=lambda: "", AsInteger=lambda value=value: value)
        for number, value in ((-1018100, 1), (-1018102, 0), (-1018103, 0), (-1018104, 0))]
    original_type = elements[165163]
    elements[165163] = BarType(165163, "25 A500")
    elements[165163].BarNominalDiameter = original_type.BarNominalDiameter
    elements[165163].BarModelDiameter = original_type.BarModelDiameter
    original_18 = elements[165160]
    elements[165160] = BarType(165160, "18 A500")
    elements[165160].BarNominalDiameter = original_18.BarNominalDiameter
    elements[165160].BarModelDiameter = original_18.BarModelDiameter
    DB.Structure.RebarBarType = BarType
    DB.Structure.Rebar = NewRebar
    DB.Structure.RebarStyle = SimpleNamespace(Standard="standard")
    DB.Structure.RebarHookOrientation = SimpleNamespace(Right="right", Left="left")
    DB.Structure.MultiplanarOption = SimpleNamespace(IncludeAllMultiplanarCurves="all")
    DB.UnitUtils.ConvertToInternalUnits = lambda value, unit: value / 304.8
    DB.Line = SimpleNamespace(CreateBound=lambda a, b: Curve(line(
        [a.X * 304.8, a.Y * 304.8, a.Z * 304.8], [b.X * 304.8, b.Y * 304.8, b.Z * 304.8])))
    DB.Curve = Curve
    DB.PlanarFace, DB.Solid, DB.GeometryInstance = TrialFace, Solid, type("GeometryInstance", (), {})
    DB.Options = SimpleNamespace
    DB.ViewDetailLevel = SimpleNamespace(Fine="fine")
    DB.RevitLinkInstance, DB.ImportInstance = Link, type(elements[9])
    DB.Sketch, DB.View = Sketch, View
    DB.BuiltInCategory = SimpleNamespace(OST_SectionBox=-2000301, OST_Cameras=-2000500)
    elements[407801].SketchId = Id(407800)
    elements[407800] = Sketch(407800, "Эскиз")
    elements[407800].Category = None
    elements[407864] = Element(407864, "{3D}")
    elements[407864].Category = SimpleNamespace(Id=Id(-2000301), Name="Section Boxes",
                                               CategoryType="internal")
    controls.obstacles = [elements[407800], elements[407864]]
    DB.FilteredElementCollector = Collector
    DB.Outline = lambda a, b: SimpleNamespace(Dispose=lambda: None)
    DB.BoundingBoxIntersectsFilter = lambda outline: outline
    DB.CategoryType = SimpleNamespace(Annotation="annotation")
    DB.Transaction = Transaction
    DB.TransactionGroup = TransactionGroup
    DB.IFailuresPreprocessor = object
    DB.FailureProcessingResult = SimpleNamespace(Continue="continue", ProceedWithRollBack="rollback")
    DB.TransactionStatus = SimpleNamespace(Started="Started", RolledBack="RolledBack", Committed="Committed")
    doc.IsReadOnly = doc.IsModifiable = doc.IsModified = False

    def regenerate():
        calls.append("regenerate")
        if controls.fail == "regenerate":
            raise RuntimeError("Regeneration failed")

    doc.Regenerate = regenerate
    doc.GetWarnings = lambda: []
    return adapter, DB, doc, elements, calls, controls


def test_json_trial_commits_rereads_and_rolls_back_outer_group(trial_api, json_input):
    adapter, DB, doc, elements, calls, _ = trial_api
    before = set(elements)
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, trial_input=json_input)
    assert report["status"] == "passed_rolled_back", report
    assert report["commit"]["status"] == "committed"
    assert report["post_commit_comparison"]["status"] == "matches"
    assert report["inner_transaction_end"]["status"] == "committed"
    assert report["group_rollback"]["status"] == "rolled_back"
    assert report["restoration"]["verified"] and set(elements) == before
    assert report["commit_failures"] == [] and not report["placement_eligible"]
    assert "commit-time-validation" not in report["not_checked"]
    assert calls == ["group_start", "start", "create", "layout", "regenerate"] + ["read_axis"] * 9 + [
        "commit"] + ["read_axis"] * 9 + ["dispose", "group_rollback", "group_dispose"]
    json.dumps(report, ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize("failure", ["create", "layout", "regenerate", "readback", "shift", "excluded",
    "commit_exception", "commit_rollback", "warning", "error", "post_commit_shift",
    "post_commit_readback", "post_commit_missing"])
def test_json_trial_failures_never_pass_and_restore_group(trial_api, json_input, failure):
    adapter, DB, doc, elements, calls, controls = trial_api
    before = set(elements)
    controls.fail = failure
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, trial_input=json_input)
    assert report["status"] == "failed_rolled_back", report
    assert report["group_rollback"]["status"] == "rolled_back"
    assert report["restoration"]["verified"] and set(elements) == before
    if failure in ("shift", "excluded", "readback", "regenerate", "create", "layout"):
        assert "commit" not in calls
    if failure in ("warning", "error"):
        assert report["commit_failures"][0]["description"] == "Ошибки теста"
        assert "post_commit_readback" not in report
    if failure == "post_commit_shift":
        assert report["comparison"]["status"] == "matches"
        assert report["post_commit_comparison"]["status"] == "differs"


@pytest.mark.parametrize("failure, expected", [("commit_pending", "rollback_unconfirmed"),
    ("group_rollback", "rollback_unconfirmed"), ("group_leak", "restoration_failed")])
def test_json_trial_uncertain_cleanup_never_claims_restoration(trial_api, json_input, failure, expected):
    adapter, DB, doc, _, calls, controls = trial_api
    controls.fail = failure
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, trial_input=json_input)
    assert report["status"] == expected, report
    assert not report.get("restoration", {}).get("verified", False)
    if failure == "commit_pending":
        assert "post_commit_readback" not in report and "group_rollback" not in calls


@pytest.mark.parametrize("key, value", [("bar_count", True), ("bar_count", 9.0), ("bar_count", 33),
    ("bar_count", 1), ("spacing_mm", 0), ("spacing_mm", float("inf")), ("length_mm", float("nan")),
    ("length_mm", "3900"), ("host_id", 1), ("diameter_mm", 18), ("direction", "bottom-X"),
    ("z_policy", "dxf"), ("anchorage_added_mm", 1000), ("first_axis_offset_x_mm", 23500),
    ("first_axis_offset_y_mm", 0), ("unexpected", 1)])
def test_json_bad_or_outside_plan_never_starts_transaction(trial_api, json_input, key, value):
    adapter, DB, doc, _, calls, _ = trial_api
    json_input["zone"][key] = value
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, trial_input=json_input)
    assert report["status"] == "blocked_preflight", report
    assert report["issues"] and not calls


def test_json_changed_valid_parameters_are_used_not_fixed_reference(trial_api, json_input):
    adapter, DB, doc, _, _, _ = trial_api
    json_input["zone"].update(bar_count=5, length_mm=2500, spacing_mm=100,
                              first_axis_offset_x_mm=2000, first_axis_offset_y_mm=2500)
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, trial_input=json_input)
    assert report["status"] == "passed_rolled_back", report
    assert report["plan"]["axes"][-1]["end_mm"] == [4500, 2900, -37.5]
    assert len(report["post_commit_readback"]["bars"]) == 5


@pytest.mark.parametrize("invalid", [b'{"zone":{},"zone":{}}', b'{"zone":{"x":NaN}}',
    b'{"zone":{"x":Infinity}}', b'{"zone":{"x":1e999}}', b'[]', b'null', b'{}', b'\xff',
    b' ' * 16385])
def test_json_loader_rejects_duplicate_nonfinite_wrong_shape_oversize_and_bad_encoding(trial, tmp_path, invalid):
    path = tmp_path / "вход.json"
    path.write_bytes(invalid)
    with pytest.raises(ValueError):
        importlib.import_module("qm_trial_input").load_trial_input(str(path))


def test_failure_recorder_is_fail_closed_even_when_reading_failures_raises(trial_api):
    adapter, DB, _, _, _, _ = trial_api
    failures = []
    recorder = adapter.failure_recorder(DB, failures)
    assert recorder.PreprocessFailures(SimpleNamespace(GetFailureMessages=lambda: [])) == "continue"

    def broken():
        raise RuntimeError("Cannot read failures")

    assert recorder.PreprocessFailures(SimpleNamespace(GetFailureMessages=broken)) == "rollback"
    assert failures == [{"read_error": "Cannot read failures"}]


def test_complete_trial_creates_reads_rolls_back_and_restores_all_ids(trial_api):
    adapter, DB, doc, elements, calls, _ = trial_api
    before = set(elements)
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True)
    assert report["status"] == "passed_rolled_back", report
    assert report["comparison"]["status"] == "matches"
    assert report["rollback"]["status"] == "rolled_back"
    assert report["restoration"] == {"added_ids": [], "removed_ids": [],
                                     "reference_unchanged": True, "verified": True}
    assert set(elements) == before
    ignored = {row["element_id"]: row for row in report["obstacle_check"]["ignored"]}
    assert ignored[407800]["reason"] == "selected_host_sketch"
    assert ignored[407800]["sketch_owner_id"] == 407801
    assert ignored[407864]["reason"] == "section_box_category"
    assert ignored[407864]["category"]["id"] == -2000301
    assert "bbox_mm" in ignored[407864]
    assert calls == ["start", "create", "layout", "regenerate"] + ["read_axis"] * 9 + ["rollback", "dispose"]
    assert not doc.IsModifiable and not report["placement_eligible"]
    json.dumps(report, ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize("change", ["foreign_sketch", "wrong_owner", "missing_sketch_id",
                                    "missing_category", "model_named_3d", "internal_category"])
def test_unknown_and_physical_obstacles_remain_blocking_despite_names_and_ids(trial_api, change):
    adapter, DB, doc, elements, calls, _ = trial_api
    if change == "foreign_sketch":
        elements[407801].SketchId = Id(900000)
    elif change == "wrong_owner":
        elements[407800].OwnerId = Id(900000)
    elif change == "missing_sketch_id":
        del elements[407801].SketchId
    elif change == "missing_category":
        elements[407864].Category = None
    else:
        elements[407864].Category = SimpleNamespace(Id=Id(-2000011), Name="Walls",
            CategoryType="model" if change == "model_named_3d" else "internal")
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True)
    assert report["status"] == "blocked_preflight", report
    assert report["obstacles"] and not calls
    assert all(row["decision"] == "blocks_trial" for row in report["obstacles"])
    assert all("category" in row and "bbox_mm" in row for row in report["obstacles"])


@pytest.mark.parametrize("category_id, reason", [(-2000301, "section_box_category"),
                                                 (-2000500, "view_camera_category")])
def test_view_helper_classification_uses_category_not_name_or_specific_element_id(
        trial_api, category_id, reason):
    adapter, DB, doc, elements, _, controls = trial_api
    box = Element(912345, "Совсем другое имя")
    box.Category = SimpleNamespace(Id=Id(category_id), Name="Different category label",
                                    CategoryType="Model")
    box.OwnerViewId = Id(30)
    controls.obstacles = [box]
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True)
    assert report["status"] == "passed_rolled_back", report
    row = next(r for r in report["obstacle_check"]["ignored"] if r["element_id"] == 912345)
    assert row["reason"] == reason and row["owner_view_id"] == 30


@pytest.mark.parametrize("category_id", [None, -2008079, -2000011, -999999])
def test_physical_or_unknown_camera_named_objects_still_block(trial_api, category_id):
    adapter, DB, doc, elements, calls, controls = trial_api
    camera = Element(407864, "{3D}")
    camera.Category = (SimpleNamespace(Id=Id(category_id), Name="Камеры", CategoryType="Model")
                       if category_id is not None else None)
    controls.obstacles = [camera]
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True)
    assert report["status"] == "blocked_preflight", report
    assert not calls
    assert report["obstacles"][0]["reason"] == "physical_or_unclassified_candidate"


def test_received_021_camera_report_reclassified_without_model_or_snapshot_edits(trial_api):
    adapter, DB, doc, elements, calls, controls = trial_api
    root = EXTENSION.parents[2] / "revit_info/08-09-2026-021"
    trial_path = root / "qmonitoring-creation-trial-20260908-093128-718000.json"
    reference_path = root / "qmonitoring-reference-20260908-093107-059000.json"
    if not trial_path.is_file() or not reference_path.is_file():
        pytest.skip("Private Revit 0.2.1 reports are not distributed")
    original_bytes = trial_path.read_bytes()
    data = json.loads(original_bytes.decode("utf-8"))
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    assert reference["status"] == "collected" and reference["issues"] == []
    assert reference["area"]["reference_comparison"]["status"] == "matches"
    assert data["probe_version"] == "0.2.1"
    assert data["status"] == "blocked_preflight" and data["rollback"]["status"] == "not_started"
    assert data["reference_issues"] == [] and data["host_check"]["status"] == "passed"
    assert "readback" not in data and "temporary_element_id" not in data
    assert len(data["obstacles"]) == 1
    observed = data["obstacles"][0]
    assert observed["category"] == {"name": "Камеры", "id": -2000500, "type": "Model"}
    camera = Element(observed["element_id"], observed["name"])
    camera.Category = SimpleNamespace(Id=Id(observed["category"]["id"]),
        Name=observed["category"]["name"], CategoryType=observed["category"]["type"])
    camera.OwnerViewId = Id(observed["owner_view_id"])
    controls.obstacles = [elements[407800], camera]
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True)
    # Simulated workflow only: the original real run never reached creation.
    assert report["status"] == "passed_rolled_back", report
    row = next(r for r in report["obstacle_check"]["ignored"] if r["element_id"] == camera.Id.Value)
    assert row["reason"] == "view_camera_category"
    assert row["owner_view_id"] == -1  # OwnerViewId alone would not identify this view helper.
    assert trial_path.read_bytes() == original_bytes


@pytest.mark.parametrize("kind", ["annotation", "view", "unreadable_category", "unreadable_owner"])
def test_obstacle_metadata_and_nonphysical_classification(trial_api, kind):
    adapter, DB, doc, elements, calls, controls = trial_api
    if kind == "view":
        item = DB.View(70, "View")
        item.Category = None
    elif kind == "unreadable_owner":
        class BadSketch(DB.Sketch):
            @property
            def OwnerId(self):
                raise RuntimeError("Ownership unavailable")
        item = BadSketch(407800, "Sketch")
        item.Category = None
    elif kind == "unreadable_category":
        class BadElement(Element):
            @property
            def Category(self):
                raise RuntimeError("Category unavailable")
        item = BadElement(407864, "{3D}")
    else:
        item = Element(71, "Annotation")
        item.Category = SimpleNamespace(Id=Id(-123), Name="Text", CategoryType="annotation")
    controls.obstacles = [item]
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True)
    if kind.startswith("unreadable"):
        assert report["status"] == "blocked_preflight" and not calls
        assert report["obstacles"][0]["decision"] == "blocks_trial"
    else:
        assert report["status"] == "passed_rolled_back", report


def test_received_020_reports_confirm_readback_but_not_creation():
    root = EXTENSION.parents[2]
    reference_path = root / "revit_info/qmonitoring-reference-20260907-180145-395000.json"
    trial_path = root / "revit_info/qmonitoring-creation-trial-20260907-180315-358000.json"
    if not reference_path.is_file() or not trial_path.is_file():
        pytest.skip("Private Revit reports are not distributed")
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    data = json.loads(trial_path.read_text(encoding="utf-8"))
    assert reference["status"] == "collected" and reference["issues"] == []
    assert reference["area"]["reference_comparison"]["status"] == "matches"
    assert data["status"] == "blocked_preflight" and data["rollback"]["status"] == "not_started"
    assert data["host_check"]["status"] == "passed"
    assert "readback" not in data and "temporary_element_id" not in data
    assert {o["element_id"] for o in data["obstacles"]} == {407800, 407864}
    # No category in 0.2.0: the generic {3D} element cannot be positively classified offline.
    generic = next(o for o in data["obstacles"] if o["element_id"] == 407864)
    assert generic["class"] == "Autodesk.Revit.DB.Element" and "category" not in generic


def test_received_022_real_creation_readback_and_rollback(trial):
    """Recheck saved REAL curves, not synthesized API-double creation results."""
    _, geometry = trial
    root = EXTENSION.parents[2] / "revit_info/022"
    trial_path = root / "qmonitoring-creation-trial-20260908-163832-016000.json"
    reference_path = root / "qmonitoring-reference-20260908-163801-142000.json"
    if not trial_path.is_file() or not reference_path.is_file():
        pytest.skip("Private successful Revit reports are not distributed")
    original_bytes = trial_path.read_bytes()
    data = json.loads(original_bytes.decode("utf-8"))
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    assert reference["status"] == "collected" and reference["issues"] == []
    assert reference["area"]["reference_comparison"]["status"] == "matches"
    assert data["probe_version"] == "0.2.2" and data["status"] == "passed_rolled_back"
    assert data["issues"] == data["reference_issues"] == data["obstacles"] == []
    assert data["rollback"] == {"status": "rolled_back", "returned": "RolledBack", "final": "RolledBack"}
    assert data["restoration"] == {"added_ids": [], "removed_ids": [], "reference_unchanged": True,
                                   "verified": True}
    assert data["document"]["is_modified_before"] is False
    assert data["document"]["is_modified_after"] is False
    assert not data["placement_eligible"] and "commit-time-validation" in data["not_checked"]
    readback = data["readback"]
    assert readback["class"] == "Autodesk.Revit.DB.Structure.Rebar"
    assert readback["element_id"] == data["temporary_element_id"]
    floor = data["reference_before"]["floor"]
    assert geometry.validate_prism(floor, data["host_solid"])["status"] == "passed"
    plan = geometry.make_trial_plan(floor, readback["bar_type"])
    comparison = geometry.compare_trial(plan, readback)
    assert comparison["status"] == "matches" and len(comparison["checks"]) == 55
    assert all(check["matches"] for check in comparison["checks"])
    assert sorted(b["position_index"] for b in readback["bars"]) == list(range(9))
    lines = sorted((b["curves"][0] for b in readback["bars"]), key=lambda c: c["start_mm"][1])
    assert [math.dist(c["start_mm"], c["end_mm"]) for c in lines] == pytest.approx([3900] * 9)
    ys = [c["start_mm"][1] for c in lines]
    assert [b - a for a, b in zip(ys, ys[1:])] == pytest.approx([96.875] * 8)
    top_z = floor["top_faces"][0]["plane"]["origin_mm"][2]
    radius = readback["bar_type"]["model_diameter_mm"] / 2
    assert [top_z - c["start_mm"][2] - radius for c in lines] == pytest.approx([25] * 9)
    assert max(math.dist(c[key], axis[key]) for c, axis in zip(lines, plan["axes"])
               for key in ("start_mm", "end_mm")) < 1e-6
    assert trial_path.read_bytes() == original_bytes


@pytest.mark.parametrize("failure", ["create", "layout", "regenerate", "readback", "shift", "excluded"])
def test_failure_and_geometry_mismatch_always_roll_back(trial_api, failure):
    adapter, DB, doc, elements, calls, controls = trial_api
    controls.fail = failure
    before = set(elements)
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True)
    assert report["status"] == "failed_rolled_back", report
    assert report["rollback"]["status"] == "rolled_back"
    assert calls[-2:] == ["rollback", "dispose"]
    assert set(elements) == before and not doc.IsModifiable


@pytest.mark.parametrize("failure, expected", [("rollback", "rollback_unconfirmed"),
    ("pending", "rollback_unconfirmed"), ("leaked_element", "restoration_failed"),
    ("changed_reference", "restoration_failed")])
def test_failed_or_delayed_rollback_never_claims_restoration(trial_api, failure, expected):
    adapter, DB, doc, _, calls, controls = trial_api
    controls.fail = failure
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True)
    assert report["status"] == expected, report
    assert "rollback" in calls and calls[-1] == "dispose"


@pytest.mark.parametrize("problem", ["confirmation", "version", "workshared", "read_only", "modifiable",
                                     "floor_mark", "layer", "obstacle", "link", "missing_api"])
def test_preflight_blocks_without_starting_any_transaction(trial_api, problem, monkeypatch):
    adapter, DB, doc, elements, calls, controls = trial_api
    if problem == "version":
        doc.Application.VersionNumber = "2025"
    elif problem == "workshared":
        doc.IsWorkshared = True
    elif problem == "read_only":
        doc.IsReadOnly = True
    elif problem == "modifiable":
        doc.IsModifiable = True
    elif problem == "floor_mark":
        elements[407801].mark = "other"
    elif problem == "layer":
        elements[407878].Parameters = []
    elif problem == "obstacle":
        item = Element(77, "Existing bar")
        item.Category = SimpleNamespace(CategoryType="model")
        controls.obstacles = [item]
    elif problem == "link":
        controls.links = [Element(78)]
    elif problem == "missing_api":
        monkeypatch.setattr(DB.Structure.Rebar, "GetTransformedCenterlineCurves", None)
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=problem != "confirmation")
    assert report["status"] == "blocked_preflight", report
    assert report["rollback"]["status"] == "not_started"
    assert report["issues"] and not calls


def test_delivery_has_only_guarded_inner_commit_no_save_delete_network_or_python3_only_syntax(tmp_path):
    # Audit the delivered archive, not unshipped experiments in the worktree.
    # Its explicit allowlist must not pull in the persistent demo implementation.
    spec = importlib.util.spec_from_file_location(
        "package_trial_safety", EXTENSION.parents[2] / "scripts/package_revit_probe.py",
    )
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    with ZipFile(packager.build_package(tmp_path / "probe.zip")) as archive:
        sources = {name: archive.read(name).decode("utf-8") for name in archive.namelist()
                   if name.endswith(".py")}
    assert not any("MVP.panel" in name or "mvp" in name.lower() for name in sources)
    bundled_modules = {name.rsplit("/", 1)[-1][:-3] for name in sources}
    commits = []
    forbidden = {"Assimilate", "Save", "SaveAs", "Delete", "MoveElement", "RotateElement",
                 "SetValueString", "Set"}
    for path, source in sources.items():
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", getattr(node.func, "id", ""))
                assert name not in forbidden, (path, name)
                if name == "Commit":
                    assert path.rsplit("/", 1)[-1] == "qm_revit_trial.py"
                    assert isinstance(node.func.value, ast.Name) and node.func.value.id == "transaction"
                    commits.append(node)
            assert not isinstance(node, (ast.JoinedStr, ast.AnnAssign, ast.AsyncFunctionDef))
            if isinstance(node, ast.FunctionDef):
                assert node.returns is None and not node.args.kwonlyargs
                assert all(a.annotation is None for a in node.args.args)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module]
                assert not any(n.split(".")[0] in {"requests", "socket", "urllib", "http", "subprocess"}
                               for n in names)
                assert all(n.split(".")[0] in bundled_modules for n in names
                           if n.startswith("qm_")), (path, names)
    assert len(commits) == 1  # Runtime tests prove it is guarded by a rollback group.


@pytest.mark.parametrize("destination_kind", ["new", "existing", "rvt", "cancel", "decline"])
def test_entrypoint_writes_report_only_after_rollback_and_can_cancel_before_mutation(
        trial_api, tmp_path, monkeypatch, destination_kind):
    adapter, DB, doc, _, calls, _ = trial_api
    destination = tmp_path / ("keep.rvt" if destination_kind == "rvt" else "отчёт.json")
    if destination_kind in ("existing", "rvt"):
        destination.write_bytes(b"preserved")
    forms = SimpleNamespace(alert=lambda *a, **kw: destination_kind != "decline",
        save_file=lambda **kw: None if destination_kind == "cancel" else str(destination))
    pyrevit = SimpleNamespace(DB=DB, forms=forms, revit=SimpleNamespace(doc=doc),
        script=SimpleNamespace(get_output=lambda: SimpleNamespace(print_md=print)))

    class GenericList:
        def __class_getitem__(cls, item):
            return CurveList

    monkeypatch.setitem(__import__("sys").modules, "pyrevit", pyrevit)
    monkeypatch.setitem(__import__("sys").modules, "System.Collections.Generic", SimpleNamespace(List=GenericList))
    probe = importlib.import_module("qm_revit_probe")
    writer = probe.write_report_json

    def checked_write(path, data):
        assert calls[-2:] == ["rollback", "dispose"] and not doc.IsModifiable
        return writer(path, data)

    monkeypatch.setattr(probe, "write_report_json", checked_write)
    runpy.run_path(str(BUTTON))
    if destination_kind == "new":
        assert json.loads(destination.read_text())["status"] == "passed_rolled_back"
    else:
        assert not calls
        if destination_kind in ("existing", "rvt"):
            assert destination.read_bytes() == b"preserved"
        else:
            assert not destination.exists()


@pytest.mark.parametrize("choice", ["new", "existing", "rvt", "cancel", "decline", "cancel_input", "invalid_input",
                                    "write_failure"])
@pytest.mark.parametrize("core_mode", [False, True])
def test_json_button_cancel_and_report_io_happen_outside_mutation(trial_api, tmp_path, monkeypatch, choice, core_mode):
    _, DB, doc, elements, calls, _ = trial_api
    before = set(elements)
    source = tmp_path / "вход.json"
    sample = EXTENSION.parent / "samples/core-axis-trial.json" if core_mode else SAMPLE
    source.write_bytes(b"{}" if choice == "invalid_input" else sample.read_bytes())
    original = source.read_bytes()
    destination = tmp_path / ("keep.rvt" if choice == "rvt" else "отчёт.json")
    if choice in ("existing", "rvt"):
        destination.write_bytes(b"preserved")
    alerts = []

    def alert(message, **kwargs):
        alerts.append(message)
        return choice != "decline"

    forms = SimpleNamespace(alert=alert,
        pick_file=lambda **kw: None if choice == "cancel_input" else str(source),
        save_file=lambda **kw: None if choice == "cancel" else str(destination))
    pyrevit = SimpleNamespace(DB=DB, forms=forms, revit=SimpleNamespace(doc=doc),
        script=SimpleNamespace(get_output=lambda: SimpleNamespace(print_md=print)))

    class GenericList:
        def __class_getitem__(cls, item):
            return CurveList

    monkeypatch.setitem(__import__("sys").modules, "pyrevit", pyrevit)
    monkeypatch.setitem(__import__("sys").modules, "System.Collections.Generic", SimpleNamespace(List=GenericList))
    probe = importlib.import_module("qm_revit_probe")
    writer = probe.write_report_json

    def checked_write(path, data):
        assert calls[-2:] == ["group_rollback", "group_dispose"] and not doc.IsModifiable
        assert set(elements) == before and data["restoration"]["verified"]
        if choice == "write_failure":
            raise IOError("Disk write failed")
        return writer(path, data)

    monkeypatch.setattr(probe, "write_report_json", checked_write)
    button = EXTENSION / "QMonitoring.tab/Diagnostics.panel/CoreTrial.pushbutton/script.py" if core_mode else JSON_BUTTON
    runpy.run_path(str(button))
    assert source.read_bytes() == original
    if choice == "new":
        assert json.loads(destination.read_text())["post_commit_comparison"]["status"] == "matches"
    elif choice == "write_failure":
        assert not destination.exists() and ("Ошибка теста" if core_mode else "Не удалось") in alerts[-1]
        assert set(elements) == before
    else:
        assert not calls
        if choice in ("existing", "rvt"):
            assert destination.read_bytes() == b"preserved"
        else:
            assert not destination.exists()


@pytest.mark.parametrize("key, value", [("schema_version", "reinforcement-zone-revit/v2"),
    ("units", "m"), ("placement_eligible", True), ("mode", "commit"), ("extra", None)])
def test_json_does_not_accept_production_contract_or_lift_gates(trial_api, json_input, key, value):
    adapter, DB, doc, _, calls, _ = trial_api
    json_input[key] = value
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, trial_input=json_input)
    assert report["status"] == "blocked_preflight" and not calls


def test_json_requires_copy_confirmation_before_group(trial_api, json_input):
    adapter, DB, doc, _, calls, _ = trial_api
    report = adapter.run_trial(doc, DB, CurveList, trial_input=json_input)
    assert report["status"] == "blocked_preflight" and not calls
