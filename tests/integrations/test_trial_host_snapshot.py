"""Early trial failures retain native host evidence without claiming placement."""
from __future__ import annotations

import copy
import importlib
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from test_full_plate_trial import (  # noqa: F401 -- shared offline fixtures
    host_data, packet, packet_module, placement, runtime_harness, type_data,
)
from test_physical_plan_trial import physical_module, physical_packet  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("kind", ["full", "physical"])
@pytest.mark.parametrize("failure", ["cover", "missing_depth", "read_issue", "foreign_type", "unsupported_face"])
def test_native_host_survives_failure_before_make_plan_finishes(
    runtime_harness, packet, physical_packet, placement, monkeypatch, kind, failure,  # noqa: F811
):
    runtime, doc, db, floor, bar_type, state = runtime_harness
    native = host_data()
    native["covers"]["other"]["distance_mm"] = 31
    read_issues = []
    if failure == "unsupported_face":
        native["top_faces"][0]["plane"] = None

    def read_floor(_):
        if failure == "read_issue":
            read_issues.append({"scope": "native-floor", "message": "Incomplete face read"})
        return copy.deepcopy(native)

    monkeypatch.setattr(runtime, "Probe", lambda *_: SimpleNamespace(
        issues=read_issues, floor=read_floor, bar_type=lambda _: type_data()))
    if failure == "cover":
        placement["axis_depths_mm"]["top-X"] = 25
    elif failure == "missing_depth":
        del placement["axis_depths_mm"]["top-Y"]
    types = {"A500|18": object() if failure == "foreign_type" else bar_type}
    if kind == "full":
        run, data = runtime.run_plate_trial, packet
    else:
        run = importlib.import_module("qm_revit_physical_trial").run_physical_trial
        data = physical_packet
    result = run(doc, db, floor, data, types, placement, lambda: [], lambda: [], copy_confirmed=True)
    assert result["status"] == "blocked_preflight"
    assert result["host"] == native
    assert result["host_id"] == floor.Id.Value == native["element_id"]
    assert result["read_issues"] == read_issues
    assert result["placement_eligible"] is False
    assert not state.calls and not state.made
    if kind == "physical":
        assert result["execution"]["host"] == result["host"]
        assert result["execution"]["host_id"] == result["host_id"]
        assert result["execution"]["read_issues"] == result["read_issues"]
        assert result["physical_plan"] == data


@pytest.mark.parametrize("kind", ["full", "physical"])
def test_failed_native_floor_read_keeps_id_and_read_errors_without_inventing_host(
    runtime_harness, packet, physical_packet, placement, monkeypatch, kind,  # noqa: F811
):
    runtime, doc, db, floor, bar_type, state = runtime_harness
    read_issues = []

    def read_floor(_):
        read_issues.append({"scope": "native-floor", "message": "Host read failed"})
        raise ValueError("Cannot complete native host read")

    monkeypatch.setattr(runtime, "Probe", lambda *_: SimpleNamespace(issues=read_issues, floor=read_floor))
    run = runtime.run_plate_trial if kind == "full" else importlib.import_module(
        "qm_revit_physical_trial").run_physical_trial
    result = run(doc, db, floor, packet if kind == "full" else physical_packet, {"A500|18": bar_type},
                 placement, lambda: [], lambda: [], copy_confirmed=True)
    assert result["status"] == "blocked_preflight"
    assert result["host_id"] == floor.Id.Value
    assert result["read_issues"] == read_issues
    assert "host" not in result
    assert not state.calls and not state.made


@pytest.mark.parametrize("native_state", ["missing", "present", "explicit_none", "other_host"])
def test_setup_fallback_never_replaces_native_host_or_issues(packet_module, native_state):  # noqa: F811
    runtime = importlib.import_module("qm_revit_plate_trial")
    setup = {"host": host_data(), "host_id": 999, "host_solid": {"volume_mm3": 100},
             "read_issues": [{"scope": "setup", "message": "Setup observation"}]}
    report = {"status": "blocked_preflight"}
    if native_state != "missing":
        report["host"] = host_data()
        report["host"]["covers"]["other"]["distance_mm"] = 33
        report["read_issues"] = []
    if native_state == "present":
        report["host_solid"] = {"volume_mm3": 200}
    elif native_state == "explicit_none":
        report.update(host=None, host_id=999, read_issues=None, host_solid=None)
    elif native_state == "other_host":
        report["host"]["element_id"] = 1234
    prior = copy.deepcopy(report)
    runtime.preserve_setup_host_snapshot(report, setup)
    if native_state == "missing":
        assert report["host"] == setup["host"] and report["host"] is not setup["host"]
        assert report["host_id"] == setup["host_id"]
        assert report["read_issues"] == setup["read_issues"]
    else:
        assert report["host"] == prior["host"]
        assert report["read_issues"] == prior["read_issues"]
        if native_state in ("present", "explicit_none"):
            assert report["host_solid"] == prior["host_solid"]
        else:
            assert report["host_id"] == 1234
            assert "host_solid" not in report


@pytest.mark.parametrize("kind", ["full", "physical"])
@pytest.mark.parametrize("native_state", ["missing", "present", "other_host"])
def test_pushbutton_keeps_setup_fallback_and_prefers_native_runtime_snapshot(
    packet, physical_packet, tmp_path, monkeypatch, kind, native_state,  # noqa: F811
):
    import runpy
    import sys

    source, output = tmp_path / "пакет.json", tmp_path / "результат.json"
    data = packet if kind == "full" else physical_packet
    source.write_text(json.dumps(data), encoding="utf-8")
    floor_class = type("Floor", (), {})
    floor = floor_class()
    floor.Id = SimpleNamespace(Value=999)
    doc = SimpleNamespace(IsFamilyDocument=False, Title="Рабочая копия", GetElement=lambda _: floor)
    uidoc = SimpleNamespace(Selection=SimpleNamespace(GetElementIds=lambda: [floor.Id]), ActiveView=None)
    material = object()

    class Collector:
        def OfClass(self, _):
            return [material]

    db = SimpleNamespace(Floor=floor_class, Structure=SimpleNamespace(RebarBarType=object), View3D=type("View3D", (), {}),
                         FilteredElementCollector=lambda _: Collector())
    inputs = iter(("0; 0", "40; 80; 40; 80"))
    forms = SimpleNamespace(pick_file=lambda **_: str(source), save_file=lambda **_: str(output),
        alert=lambda *_a, **_kw: True, ask_for_string=lambda **_: next(inputs),
        SelectFromList=SimpleNamespace(show=lambda labels, **_: labels[0]))
    pyrevit = ModuleType("pyrevit")
    pyrevit.DB, pyrevit.forms = db, forms
    pyrevit.revit = SimpleNamespace(doc=doc, uidoc=uidoc)
    generic = ModuleType("System.Collections.Generic")
    generic.List = list
    monkeypatch.setitem(sys.modules, "pyrevit", pyrevit)
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", generic)
    probe_module = importlib.import_module("qm_revit_probe")
    trial_module = importlib.import_module("qm_revit_trial")
    monkeypatch.setattr(probe_module, "Probe", lambda *_: SimpleNamespace(issues=[], floor=lambda _: host_data(),
                        bar_type=lambda _: dict(type_data(), name="18 A500")))
    monkeypatch.setattr(trial_module, "solid_report", lambda *_: {"volume_mm3": 100})
    runtime_name = "qm_revit_plate_trial" if kind == "full" else "qm_revit_physical_trial"
    function_name = "run_plate_trial" if kind == "full" else "run_physical_trial"
    returned = {"status": "blocked_preflight", "placement_eligible": False,
                "issues": [{"stage": "preflight", "message": "Injected early placement rejection"}]}
    if native_state != "missing":
        returned["host"] = host_data()
        returned["host"]["covers"]["other"]["distance_mm"] = 33
        returned["host_id"] = 999 if native_state == "present" else 1234
        returned["host"]["element_id"] = returned["host_id"]
        returned["read_issues"] = [{"scope": "native", "message": "Runtime host observation"}]
        returned["host_solid"] = {"volume_mm3": 200}
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return copy.deepcopy(returned)

    monkeypatch.setattr(importlib.import_module(runtime_name), function_name, run)
    button = "FullPlateTrial" if kind == "full" else "PhysicalPlanTrial"
    entry = ROOT / ("integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/"
                    + button + ".pushbutton/script.py")
    runpy.run_path(str(entry))["main"]()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert result["status"] == "blocked_preflight"
    assert result["placement_eligible"] is False
    if native_state == "missing":
        assert result["host"] == host_data()
        assert result["host_id"] == 999
        assert result["read_issues"] == []
        assert result["host_solid"] == {"volume_mm3": 100}
    else:
        for key in ("host", "host_id", "read_issues", "host_solid"):
            assert result[key] == returned[key]


@pytest.mark.parametrize("kind", ["full", "physical"])
@pytest.mark.parametrize("failed_read", ["floor", "solid"])
def test_pushbutton_retains_read_issues_even_when_setup_geometry_read_raises(
    packet, physical_packet, tmp_path, monkeypatch, kind, failed_read,  # noqa: F811
):
    import runpy
    import sys

    source, output = tmp_path / "пакет.json", tmp_path / "результат.json"
    source.write_text(json.dumps(packet if kind == "full" else physical_packet), encoding="utf-8")
    floor_class = type("Floor", (), {})
    floor = floor_class()
    floor.Id = SimpleNamespace(Value=999)
    doc = SimpleNamespace(IsFamilyDocument=False, Title="Рабочая копия", GetElement=lambda _: floor)
    uidoc = SimpleNamespace(Selection=SimpleNamespace(GetElementIds=lambda: [floor.Id]))
    forms = SimpleNamespace(pick_file=lambda **_: str(source), save_file=lambda **_: str(output),
                            alert=lambda *_a, **_kw: True)
    pyrevit = ModuleType("pyrevit")
    pyrevit.DB, pyrevit.forms = SimpleNamespace(Floor=floor_class), forms
    pyrevit.revit = SimpleNamespace(doc=doc, uidoc=uidoc)
    generic = ModuleType("System.Collections.Generic")
    generic.List = list
    monkeypatch.setitem(sys.modules, "pyrevit", pyrevit)
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", generic)
    read_issues = []

    def fail():
        read_issues.append({"scope": failed_read, "message": "Injected native setup read failure"})
        raise ValueError("Injected native setup read failure")

    def read_floor(_):
        return fail() if failed_read == "floor" else host_data()

    monkeypatch.setattr(importlib.import_module("qm_revit_probe"), "Probe",
                        lambda *_: SimpleNamespace(issues=read_issues, floor=read_floor))
    monkeypatch.setattr(importlib.import_module("qm_revit_trial"), "solid_report", lambda *_: fail())
    button = "FullPlateTrial" if kind == "full" else "PhysicalPlanTrial"
    entry = ROOT / ("integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/"
                    + button + ".pushbutton/script.py")
    runpy.run_path(str(entry))["main"]()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["status"] == "blocked_setup"
    assert result["host_id"] == 999
    assert result["read_issues"] == read_issues
    assert result["issues"][0]["message"] == "Injected native setup read failure"
    if failed_read == "floor":
        assert "host" not in result
    else:
        assert result["host"] == host_data()
