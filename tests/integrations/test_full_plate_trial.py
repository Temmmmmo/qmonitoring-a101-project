"""Offline contract/rollback tests; API doubles do not prove Windows Revit support."""
from __future__ import annotations

import copy
import importlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.optimization.services.detailing import rebar_mass_kg

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "integrations/pyrevit/QMonitoring.extension/lib"


@pytest.fixture
def packet_module(monkeypatch):
    monkeypatch.syspath_prepend(str(LIB))
    return importlib.import_module("qm_plate_packet")


@pytest.fixture
def packet(packet_module):
    data = {"schema_version": packet_module.SCHEMA, "mode": "commit-readback-rollback", "units": "mm",
        "placement_eligible": False, "case_id": "Offline contract fixture", "source_report_sha256": "a" * 64,
        "source_blockers": ["engineering-approval"], "directions": [], "expected": {
            "zone_count": 4, "run_count": 4, "physical_bar_count": 10,
            "additional_mass_kg": rebar_mass_kg(18, 2000, 10)}}
    for i, direction in enumerate(packet_module.DIRECTIONS):
        axis = 0 if direction.endswith("X") else 1
        a, b = [1000, 1000], [1000, 1000]
        b[axis] += 2000
        run = {"id": direction + ":zone:0:0", "zone_id": "zone", "component_index": 0,
            "steel_class": "A500", "diameter_mm": 18, "start_xy_mm": a, "end_xy_mm": b,
            "bar_count": i + 1, "spacing_mm": 300}
        data["directions"].append({"direction": direction, "source": direction + ".dxf", "runs": [run]})
    return packet_module.validate_packet(data)


def host_data():
    return {"element_id": 999, "bbox_mm": {"min_mm": [0, 0, -300], "max_mm": [5000, 5000, 0]},
        "covers": {s: {"distance_mm": 25} for s in ("top", "bottom", "other")},
        "top_faces": [{"plane": {"origin_mm": [0, 0, 0], "normal": [0, 0, 1]}}],
        "bottom_faces": [{"plane": {"origin_mm": [0, 0, -300], "normal": [0, 0, -1]}}]}


def type_data():
    return {"element_id": 888, "nominal_diameter_mm": 18, "model_diameter_mm": 18}


@pytest.fixture
def placement(packet_module):
    return {"offset_x_mm": 100, "offset_y_mm": 200, "confirmed": True,
        "axis_depths_mm": dict(zip(packet_module.DIRECTIONS, (40, 80, 40, 80)))}


@pytest.fixture
def plan(packet_module, packet, placement):
    return packet_module.make_plan(packet, host_data(), {"A500|18": type_data()}, placement)


def readback(plan):
    sets = []
    for run in plan["runs"]:
        bars = []
        for axis in run["axes"]:
            bars.append({"curves": [{"kind": "Line", "start_mm": axis["start_mm"][:],
                "end_mm": axis["end_mm"][:], "length_mm": math.dist(axis["start_mm"], axis["end_mm"])}]})
        sets.append({"host_id": run["host_id"], "quantity": run["bar_count"],
            "number_of_bar_positions": run["bar_count"], "layout_rule": run["layout_rule"],
            "hook_type_ids": [-1, -1], "bar_type": type_data(), "bars": bars})
    return {"sets": sets}


def test_full_plate_axes_count_mass_layers_and_single(plan, packet_module):
    assert len(plan["runs"]) == 4 and len(plan["bars"]) == 10
    assert [r["axes"][0]["start_mm"][2] for r in plan["runs"]] == [-260, -220, -40, -80]
    assert [r["normal"] for r in plan["runs"]] == [[0, 1, 0], [1, 0, 0]] * 2
    assert plan["runs"][0]["layout_rule"] == "Single"
    assert plan["runs"][1]["axes"][1]["start_mm"] == [1400, 1200, -220]
    assert packet_module.compare_readback(plan, readback(plan))["status"] == "matches"


@pytest.mark.parametrize("change", ["partial", "duplicate", "mass", "quantity", "direction", "nan", "commit", "units"])
def test_packet_rejects_partial_or_changed_inputs(packet_module, packet, change):
    run = packet["directions"][0]["runs"][0]
    if change == "partial":
        packet["directions"].pop()
    elif change == "duplicate":
        packet["directions"][1]["runs"][0]["id"] = run["id"]
    elif change == "mass":
        packet["expected"]["additional_mass_kg"] += 1
    elif change == "quantity":
        run["bar_count"] = True
    elif change == "direction":
        run["end_xy_mm"][1] += 1
    elif change == "nan":
        run["start_xy_mm"][0] = float("nan")
    elif change == "commit":
        packet["placement_eligible"] = True
    else:
        packet["units"] = "m"
    with pytest.raises(ValueError):
        packet_module.validate_packet(packet)


@pytest.mark.parametrize("change", ["missing", "moved", "clipped", "type", "hook", "layout", "api_length"])
def test_readback_detects_revit_adjustments(packet_module, plan, change):
    data = readback(plan)
    item = data["sets"][-1]
    if change == "missing":
        data["sets"].pop()
    elif change == "moved":
        item["bars"][0]["curves"][0]["start_mm"][2] += 1
    elif change == "clipped":
        item["bars"][0]["curves"][0]["end_mm"][1] -= 10
    elif change == "type":
        item["bar_type"]["element_id"] = 9
    elif change == "hook":
        item["hook_type_ids"][0] = 5
    elif change == "layout":
        item["layout_rule"] = "MaximumSpacing"
    else:
        item["bars"][0]["curves"][0]["length_mm"] -= 1
    assert packet_module.compare_readback(plan, data)["status"] == "differs"


def test_readback_nan_is_not_accepted(packet_module, plan):
    data = readback(plan)
    data["sets"][0]["bar_type"]["nominal_diameter_mm"] = float("nan")
    with pytest.raises(ValueError):
        packet_module.compare_readback(plan, data)


def test_readback_requires_full_3d_points(packet_module, plan):
    data = readback(plan)
    data["sets"][0]["bars"][0]["curves"][0]["start_mm"].pop()
    with pytest.raises(ValueError, match="3D"):
        packet_module.compare_readback(plan, data)


@pytest.mark.parametrize("change", ["unconfirmed", "cover", "slope", "step", "type"])
def test_explicit_host_profile_required(packet_module, packet, placement, change):
    host, material = host_data(), type_data()
    if change == "unconfirmed":
        placement["confirmed"] = False
    elif change == "cover":
        placement["axis_depths_mm"]["top-X"] = 25
    elif change == "slope":
        host["top_faces"][0]["plane"]["normal"] = [0.1, 0, 1]
    elif change == "step":
        host["top_faces"].append({"plane": {"origin_mm": [0, 0, 10], "normal": [0, 0, 1]}})
    else:
        material["model_diameter_mm"] = 20
    with pytest.raises(ValueError):
        packet_module.make_plan(packet, host, {"A500|18": material}, placement)


def test_loader_unicode_duplicates_and_no_overwrite(packet_module, packet, tmp_path):
    target = tmp_path / "плита.json"
    target.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    assert packet_module.load_packet(str(target)) == packet
    target.write_text('{"units":"mm","units":"mm"}', encoding="utf-8")
    with pytest.raises(ValueError):
        packet_module.load_packet(str(target))


@pytest.fixture
def runtime_harness(monkeypatch, packet_module):
    runtime = importlib.import_module("qm_revit_plate_trial")
    state = SimpleNamespace(ids={999, 888}, calls=[], made=[], fail_at=None, commit="Committed",
                            read_count=0, mismatch_after=False, geometry_blocked=False, reads_after_pending=0)
    document = SimpleNamespace(IsFamilyDocument=False, IsReadOnly=False, IsModifiable=False,
                               IsWorkshared=False, Application=SimpleNamespace(VersionNumber="2024"))

    class Id:
        def __init__(self, value):
            self.Value = value

    class Floor:
        Document = document

    class BarType:
        Document = document

    floor, bar_type = Floor(), BarType()
    floor.Id = Id(999)
    document.GetElement = lambda value: floor
    document.Regenerate = lambda: None

    class Options:
        def SetClearAfterRollback(self, _):
            return self

        def SetForcedModalHandling(self, _):
            return self

        def SetFailuresPreprocessor(self, _):
            return self

    class Scope:
        def __init__(self, doc, name):
            self.status, self.group = "Uninitialized", "FULL PLATE" in name

        def Start(self):
            self.status = "Started"
            state.calls.append("start_group" if self.group else "start_transaction")
            return self.status

        def GetStatus(self):
            return self.status

        def Commit(self):
            self.status = state.commit
            state.calls.append("commit")
            return self.status

        def RollBack(self):
            state.calls.append("rollback_group" if self.group else "rollback_transaction")
            state.ids = {999, 888}
            self.status = "RolledBack"
            return self.status

        def Dispose(self):
            state.calls.append("dispose_group" if self.group else "dispose_transaction")

        def GetFailureHandlingOptions(self):
            return Options()

        def SetFailureHandlingOptions(self, _):
            pass

    db = SimpleNamespace(Floor=Floor, ElementId=Id, Structure=SimpleNamespace(RebarBarType=BarType),
        Transaction=Scope, TransactionGroup=Scope, BuiltInParameter=SimpleNamespace(ALL_MODEL_INSTANCE_COMMENTS="comments"),
        TransactionStatus=SimpleNamespace(Started="Started", Committed="Committed", RolledBack="RolledBack"))

    class Probe:
        def __init__(self, doc, DB):
            self.doc, self.DB, self.issues = doc, DB, []

        def floor(self, _):
            return copy.deepcopy(host_data())

        def bar_type(self, _):
            return type_data()

    def ids(*_):
        if state.commit == "Pending" and "commit" in state.calls:
            state.reads_after_pending += 1
        return state.ids.copy()

    def create(probe, floor, bar_type, plan, factory):
        if len(state.made) == state.fail_at:
            raise ValueError("Injected mid-batch failure")
        state.made.append(plan)
        value = 1000 + len(state.made)
        state.ids.add(value)
        return SimpleNamespace(Id=Id(value), get_Parameter=lambda _: SimpleNamespace(IsReadOnly=False, Set=lambda _: True))

    def read(*_):
        state.read_count += 1
        result = readback({"runs": state.made})
        if state.mismatch_after and state.read_count == 2:
            result["sets"][0]["bars"][0]["curves"][0]["end_mm"][0] += 1
        return result

    monkeypatch.setattr(runtime, "Probe", Probe)
    monkeypatch.setattr(runtime, "document_ids", ids)
    monkeypatch.setattr(runtime, "create_trial_rebar", create)
    monkeypatch.setattr(runtime, "read_core_sets", read)
    monkeypatch.setattr(runtime, "failure_recorder", lambda *_: object())
    monkeypatch.setattr(runtime, "check_host_geometry", lambda *_: {"status": "blocked" if state.geometry_blocked
        else "contained_conservative_envelopes"})
    return runtime, document, db, floor, bar_type, state


@pytest.mark.parametrize("failure", [None, "mid_batch", "post_commit", "pending", "geometry", "copy"])
def test_whole_batch_transaction_and_no_partial_success(runtime_harness, packet, placement, failure):
    runtime, doc, db, floor, bar_type, state = runtime_harness
    state.fail_at = 2 if failure == "mid_batch" else None
    state.mismatch_after = failure == "post_commit"
    state.commit = "Pending" if failure == "pending" else "Committed"
    state.geometry_blocked = failure == "geometry"
    result = runtime.run_plate_trial(doc, db, floor, packet, {"A500|18": bar_type}, placement,
        lambda: [], lambda: [], copy_confirmed=failure != "copy")
    assert result["placement_eligible"] is False
    assert result["source_blockers"] == packet["source_blockers"]
    if failure is None:
        assert result["status"] == "passed_rolled_back"
        assert len(state.made) == 4
        assert result["post_commit_comparison"]["physical_bar_count"] == 10
        assert state.calls.index("commit") < state.calls.index("rollback_group")
        assert result["restoration"]["verified"]
    elif failure in ("mid_batch", "post_commit"):
        assert result["status"] == "failed_rolled_back"
        assert result["restoration"]["verified"]
    elif failure == "pending":
        assert result["status"] == "rollback_unconfirmed"
        assert "rollback_group" not in state.calls
        assert state.reads_after_pending == 0
    else:
        assert result["status"] == "blocked_preflight"
        assert not state.calls
    if failure != "pending":
        assert state.ids == {999, 888}


def test_new_runtime_never_saves_or_assimilates():
    content = (LIB / "qm_revit_plate_trial.py").read_text()
    for forbidden in (".Save(", ".SaveAs(", ".Assimilate(", ".Delete(", "407801", "407878"):
        assert forbidden not in content


@pytest.fixture
def analysis(packet):
    directions = []
    for direction in packet["directions"]:
        key = direction["direction"]
        layer, axis = key.split("-")
        vector = {"layer": layer, "axis": axis}
        run = direction["runs"][0]
        coordinates = [1000 + i * 300 for i in range(run["bar_count"])]
        box = ([1000, 1000, 3000, coordinates[-1]] if axis == "X"
               else [1000, 1000, coordinates[-1], 3000])
        mass = rebar_mass_kg(18, 2000, run["bar_count"])
        metrics = {"zone_count": 1, "physical_bar_count": run["bar_count"], "additional_mass_kg": mass}
        component = {"component_index": 0, "diameter_mm": 18, "bar_axis_bbox_mm": box,
            "installed_length_mm": 2000, "axis_coordinates_mm": coordinates,
            "uniform_runs": [{"first_axis_mm": 1000, "actual_step_mm": 300, "bar_count": run["bar_count"]}],
            "bar_count": run["bar_count"], "mass_kg": mass}
        zone = {"schema_version": "reinforcement-zone-revit/v2", "units": "mm", "direction": vector,
            "source_zone_id": "zone", "components": [component], "checks": {
                "geometry_and_schedule": "pass", "blocking_check_ids": ["engineering-approval"]}}
        directions.append({"direction": vector, "settings": {"steel_class": "A500"},
            "source": {"filenames": {"dxf": key + ".dxf"}}, "candidates": [{"candidate_index": 8,
                "coverage": {"coverage_passed": True, "uncovered_cell_count": 0},
                "metrics": metrics, "zone_drafts": [zone]}]})
    return {"schema_version": "composite-plate-analysis/v1", "units": "mm", "placement_eligible": False,
        "source_demand_preserved": True, "case_id": packet["case_id"], "blocking_check_ids": packet["source_blockers"],
        "directions": directions, "front": [{"direction_candidate_indexes": [8] * 4,
            "zone_count": 4, "physical_bar_count": 10, "additional_mass_kg": packet["expected"]["additional_mass_kg"]}]}


def test_export_all_four_directions_losslessly(packet_module, packet, analysis):
    from rebar.application.plate_revit_trial import build_full_plate_trial
    exported = build_full_plate_trial(analysis, 0, "a" * 64)
    assert packet_module.validate_packet(exported) == packet


@pytest.mark.parametrize("change", ["demand", "coverage", "axes", "length", "count", "zones", "mass", "candidate", "demo"])
def test_export_rejects_inconsistent_source(analysis, change):
    from rebar.application.plate_revit_trial import build_full_plate_trial
    candidate = analysis["directions"][0]["candidates"][0]
    component = candidate["zone_drafts"][0]["components"][0]
    if change == "demand":
        analysis["source_demand_preserved"] = False
    elif change == "coverage":
        candidate["coverage"]["uncovered_cell_count"] = 1
    elif change == "axes":
        component["axis_coordinates_mm"][0] += 1
    elif change == "length":
        component["installed_length_mm"] += 1
    elif change == "count":
        component["bar_count"] += 1
    elif change == "zones":
        candidate["zone_drafts"] *= 2
    elif change == "mass":
        analysis["front"][0]["additional_mass_kg"] += 1
    elif change == "candidate":
        analysis["front"][0]["direction_candidate_indexes"][0] = 1
    else:
        analysis["demo"] = True
    with pytest.raises(ValueError):
        build_full_plate_trial(analysis, 0, "a" * 64)


def test_package_whitelist_and_no_overwrite(packet_module, packet, tmp_path, monkeypatch):
    from zipfile import ZipFile
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    packager = importlib.import_module("package_revit_plate_trial")
    source = tmp_path / "input.json"
    source.write_text(json.dumps(packet), encoding="utf-8")
    target = tmp_path / "full-plate.zip"
    packager.build_package(target, source)
    with ZipFile(target) as archive:
        expected_names = set(packager.PROBE_FILES) | set(packager.NEW_FILES) | {"REFERENCE_README.md", "full-plate-trial.json"}
        assert set(archive.namelist()) == expected_names
        assert not any("MVP.panel" in name or "_mvp" in name for name in archive.namelist())
        assert json.loads(archive.read("full-plate-trial.json")) == packet
        for name in packager.NEW_FILES:
            assert archive.read(name) == (packager.SOURCE / name).read_bytes()
    with pytest.raises(FileExistsError):
        packager.build_package(target, source)


@pytest.mark.parametrize("condition", ["contained", "opening", "boolean_failure", "bbox", "multiple_solids"])
def test_actual_solid_check_control_flow(packet_module, plan, condition):
    runtime = importlib.import_module("qm_revit_plate_trial")
    calls = []

    class Solid:
        Volume = 1

        def Dispose(self):
            calls.append("dispose_solid")

    class Loop:
        def Append(self, line):
            pass

        def Dispose(self):
            calls.append("dispose_loop")

    class Loops(list):
        def Add(self, item):
            self.append(item)

    def boolean(*_):
        calls.append("boolean")
        if condition == "boolean_failure":
            raise ValueError("Unable to compute boolean")
        result = Solid()
        result.Volume = 10 if condition == "opening" else 0
        return result

    class XYZ:
        BasisZ = object()

        def __init__(self, *values):
            self.values = values

    db = SimpleNamespace(Options=SimpleNamespace, ViewDetailLevel=SimpleNamespace(Fine=1),
        GeometryInstance=type("GeometryInstance", (), {}), Solid=Solid, XYZ=XYZ,
        CurveLoop=Loop, Line=SimpleNamespace(CreateBound=lambda a, b: (a, b)),
        UnitUtils=SimpleNamespace(ConvertToInternalUnits=lambda v, _: v), UnitTypeId=SimpleNamespace(Millimeters=1),
        GeometryCreationUtilities=SimpleNamespace(CreateExtrusionGeometry=lambda *_: Solid()),
        BooleanOperationsUtils=SimpleNamespace(ExecuteBooleanOperation=boolean),
        BooleanOperationsType=SimpleNamespace(Difference=1))
    probe = SimpleNamespace(DB=db, mm=lambda v: v)
    floor = SimpleNamespace(get_Geometry=lambda _: [Solid()] * (2 if condition == "multiple_solids" else 1))
    plan["host"] = host_data()
    if condition == "bbox":
        for bar in plan["bars"]:
            bar["body_bbox_mm"]["min_mm"][0] = -1
    if condition in ("boolean_failure", "multiple_solids"):
        with pytest.raises(ValueError):
            runtime.check_host_geometry(probe, floor, plan, Loops)
        return
    result = runtime.check_host_geometry(probe, floor, plan, Loops)
    assert result["checked_physical_bars"] == 10
    assert result["outside_count"] == (0 if condition == "contained" else 10)
    assert result["status"] == ("contained_conservative_envelopes" if condition == "contained" else "blocked")
    assert ("boolean" in calls) is (condition != "bbox")


def test_button_preserves_working_host_when_model_lacks_bar_types(packet_module, packet, tmp_path, monkeypatch):
    import runpy
    import sys
    from types import ModuleType
    source, output = tmp_path / "плита.json", tmp_path / "результат.json"
    source.write_text(json.dumps(packet), encoding="utf-8")
    probe_module = importlib.import_module("qm_revit_probe")
    runtime_module = importlib.import_module("qm_revit_trial")
    floor_class = type("Floor", (), {})
    floor = floor_class()
    floor.Id = SimpleNamespace(Value=999)
    doc = SimpleNamespace(IsFamilyDocument=False, Title="Рабочая копия", GetElement=lambda _: floor)
    uidoc = SimpleNamespace(Selection=SimpleNamespace(GetElementIds=lambda: [floor.Id]))

    class Collector:
        def OfClass(self, _):
            return []

    db = SimpleNamespace(Floor=floor_class, Structure=SimpleNamespace(RebarBarType=object),
                         FilteredElementCollector=lambda _: Collector())
    forms = SimpleNamespace(pick_file=lambda **_: str(source), save_file=lambda **_: str(output),
                            alert=lambda *args, **kwargs: None)
    pyrevit = ModuleType("pyrevit")
    pyrevit.DB, pyrevit.forms = db, forms
    pyrevit.revit = SimpleNamespace(doc=doc, uidoc=uidoc)
    generic = ModuleType("System.Collections.Generic")
    generic.List = list
    monkeypatch.setitem(sys.modules, "pyrevit", pyrevit)
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", generic)
    monkeypatch.setattr(probe_module, "Probe", lambda *_: SimpleNamespace(issues=[], floor=lambda _: host_data()))
    monkeypatch.setattr(runtime_module, "solid_report", lambda *_: {"volume_mm3": 7500000000})
    entry = ROOT / "integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/FullPlateTrial.pushbutton/script.py"
    runpy.run_path(str(entry))["main"]()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "blocked_setup"
    assert result["host"] == host_data()
    assert result["host_solid"]["volume_mm3"] == 7500000000
    assert result["missing_bar_types"] == ["A500|18"]
    assert result["available_bar_types"] == []
    assert result["placement_eligible"] is False
