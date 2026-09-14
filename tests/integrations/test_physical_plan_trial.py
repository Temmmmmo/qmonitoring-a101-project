"""Offline physical provenance/rollback checks, not Windows Revit verification."""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_full_plate_trial import (  # noqa: F401 -- shared offline API fixtures
    host_data,
    packet_module,
    runtime_harness,
)

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "integrations/pyrevit/QMonitoring.extension/lib"


def small_physical_packet():
    """Six source zones, five execution groups, eleven bars; one unresolved pair."""
    directions = ("bottom-X", "bottom-Y", "top-X", "top-Y")
    data = {"schema_version": "physical-bar-plan-trial/v1", "mode": "commit-readback-rollback",
        "units": "mm", "placement_eligible": False, "case_id": "Физический план: offline fixture",
        "source_report_sha256": "a" * 64, "raw_report_sha256": "b" * 64,
        "source_blockers": ["unresolved-joints", "host-z-engineering-not-approved"],
        "source_zones": [], "directions": [], "expected": {"source_zone_count": 6,
            "execution_group_count": 5, "run_count": 5, "physical_bar_count": 11,
            "additional_mass_kg": 0.000006165 * 18 ** 2 * 2000 * 11, "position_count": 1},
        "manual_joint_tasks": [{"id": "joint-1", "direction": "top-X", "status": "unresolved",
            "kind": "body_intersection", "first": {"run_id": "top-X:run", "bar_index": 0},
            "second": {"run_id": "top-X:joint", "bar_index": 0}}]}
    for i, direction in enumerate(directions):
        axis = 0 if direction.endswith("X") else 1
        start, end = [1000, 1000], [1000, 1000]
        end[axis] = 3000
        component = {"component_index": 0, "source_bar_count": i + 1, "diameter_mm": 18,
            "required_interval_mm": [1800, 2200], "axis_coordinates_mm": [1000 + j * 300 for j in range(i + 1)],
            "steel_class": "A500", "background_diameter_mm": 18, "background_origin_mm": 0}
        data["source_zones"].append({"direction": direction, "zone_id": "source", "components": [component]})
        run = {"id": direction + ":run", "execution_group_id": "group", "steel_class": "A500",
            "diameter_mm": 18, "start_xy_mm": start, "end_xy_mm": end, "bar_count": i + 1, "spacing_mm": 300,
            "bar_sources": [{"bar_id": "bar-" + str(j), "source_refs": [
                {"zone_id": "source", "component_index": 0, "bar_index": j}]} for j in range(i + 1)]}
        data["directions"].append({"direction": direction, "source": direction + ".dxf", "runs": [run]})
    extra = copy.deepcopy(data["source_zones"][0])
    extra["zone_id"] = "second-owner"
    data["source_zones"].append(extra)
    data["directions"][0]["runs"][0]["bar_sources"][0]["source_refs"].append(
        {"zone_id": "second-owner", "component_index": 0, "bar_index": 0})
    source = copy.deepcopy(data["source_zones"][2])
    source["zone_id"] = "joint-source"
    source["components"][0].update(source_bar_count=1, required_interval_mm=[2200, 2600], axis_coordinates_mm=[1000])
    data["source_zones"].append(source)
    data["directions"][2]["runs"].append({"id": "top-X:joint", "execution_group_id": "joint-group",
        "steel_class": "A500", "diameter_mm": 18, "start_xy_mm": [1400, 1000], "end_xy_mm": [3400, 1000],
        "bar_count": 1, "spacing_mm": 300, "bar_sources": [{"bar_id": "joint-bar", "source_refs": [
            {"zone_id": "joint-source", "component_index": 0, "bar_index": 0}]}]})
    return data


@pytest.fixture
def physical_module(monkeypatch):
    monkeypatch.syspath_prepend(str(LIB))
    return importlib.import_module("qm_physical_packet")


@pytest.fixture
def physical_packet(physical_module):
    return physical_module.validate_packet(small_physical_packet())


def test_distinct_source_groups_and_lossless_projection(physical_module, physical_packet, packet_module):  # noqa: F811
    before = copy.deepcopy(physical_packet)
    projected = physical_module.private_execution_packet(physical_packet)
    assert packet_module.validate_packet(projected) == projected
    assert projected["expected"]["zone_count"] == 5
    assert physical_packet["expected"]["source_zone_count"] == 6
    for old_dir, new_dir in zip(physical_packet["directions"], projected["directions"], strict=True):
        for old, new in zip(old_dir["runs"], new_dir["runs"], strict=True):
            for key in ("id", "steel_class", "diameter_mm", "start_xy_mm", "end_xy_mm", "bar_count", "spacing_mm"):
                assert old[key] == new[key]
    assert physical_packet == before
    assert len(physical_packet["directions"][0]["runs"][0]["bar_sources"][0]["source_refs"]) == 2
    projected["directions"][0]["runs"][0]["start_xy_mm"][0] = -999
    assert physical_packet == before


@pytest.mark.parametrize("change", [
    "approved", "missing_direction", "stale_mass", "fake_position_count", "zone_group_confusion", "raw_hash",
    "missing_owner", "duplicate_owner", "fake_axis", "too_short_40d", "fake_source_count", "source_order",
    "weaker_diameter", "changed_class", "missing_task", "extra_task", "solved_task", "unknown_task_bar",
    "missing_provenance", "duplicate_bar_id", "bad_group", "bool_quantity", "nan", "extra_field",
])
def test_strict_packet_rejects_loss_corruption_or_resolution_claim(physical_module, physical_packet, change):
    run = physical_packet["directions"][0]["runs"][0]
    source = physical_packet["source_zones"][0]["components"][0]
    if change == "approved":
        physical_packet["placement_eligible"] = True
    elif change == "missing_direction":
        physical_packet["directions"].pop()
    elif change == "stale_mass":
        physical_packet["expected"]["additional_mass_kg"] += 1
    elif change == "fake_position_count":
        physical_packet["expected"]["position_count"] += 1
    elif change == "zone_group_confusion":
        physical_packet["expected"]["source_zone_count"] = 5
    elif change == "raw_hash":
        physical_packet["raw_report_sha256"] = "z" * 64
    elif change == "missing_owner":
        run["bar_sources"][0]["source_refs"].pop()
    elif change == "duplicate_owner":
        run["bar_sources"][0]["source_refs"].append(copy.deepcopy(run["bar_sources"][0]["source_refs"][0]))
    elif change == "fake_axis":
        source["axis_coordinates_mm"][0] += 1
    elif change == "too_short_40d":
        source["required_interval_mm"][0] = 1700
    elif change == "fake_source_count":
        source["source_bar_count"] += 1
    elif change == "source_order":
        physical_packet["source_zones"][1]["components"][0]["axis_coordinates_mm"].reverse()
    elif change == "weaker_diameter":
        source["diameter_mm"] = 20
    elif change == "changed_class":
        source["steel_class"] = "A400"
    elif change == "missing_task":
        physical_packet["manual_joint_tasks"].clear()
    elif change == "extra_task":
        task = copy.deepcopy(physical_packet["manual_joint_tasks"][0])
        task["id"] = "new-task"
        physical_packet["manual_joint_tasks"].append(task)
    elif change == "solved_task":
        physical_packet["manual_joint_tasks"][0]["status"] = "resolved"
    elif change == "unknown_task_bar":
        physical_packet["manual_joint_tasks"][0]["first"]["bar_index"] = 999
    elif change == "missing_provenance":
        run["bar_sources"] = []
    elif change == "duplicate_bar_id":
        other = physical_packet["directions"][1]["runs"][0]
        other["bar_sources"][1]["bar_id"] = other["bar_sources"][0]["bar_id"]
    elif change == "bad_group":
        physical_packet["directions"][2]["runs"][1]["execution_group_id"] = "group"
        physical_packet["expected"]["execution_group_count"] -= 1
    elif change == "bool_quantity":
        run["bar_count"] = True
    elif change == "nan":
        source["background_origin_mm"] = float("nan")
    elif change == "extra_field":
        run["zone_id"] = "fake-single-owner"
    with pytest.raises(ValueError):
        physical_module.validate_packet(physical_packet)


def test_thickening_cannot_penetrate_original_background_contact(physical_module, physical_packet):
    # Move both original and actual bottom-X bar to the original contact axis 18mm from background.
    run = physical_packet["directions"][0]["runs"][0]
    run["start_xy_mm"][1] = run["end_xy_mm"][1] = 18
    for zone in physical_packet["source_zones"]:
        if zone["direction"] == "bottom-X":
            zone["components"][0]["axis_coordinates_mm"] = [18]
    physical_module.validate_packet(physical_packet)
    run["diameter_mm"] = 20
    physical_packet["expected"]["additional_mass_kg"] += 0.000006165 * (20**2 - 18**2) * 2000
    physical_packet["expected"]["position_count"] = 2
    # Also provide enough new 40d so the contact check is the failing condition.
    with pytest.raises(ValueError, match="contact"):
        physical_module.validate_packet(physical_packet)


def test_loader_unicode_bounds_and_duplicate_json(physical_module, physical_packet, tmp_path, monkeypatch):
    path = tmp_path / "физический-план.json"
    path.write_text(json.dumps(physical_packet, ensure_ascii=False), encoding="utf-8-sig")
    assert physical_module.load_packet(str(path)) == physical_packet
    loaded, digest = physical_module.load_packet_with_sha256(str(path))
    assert loaded == physical_packet
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_text('{"units":"mm","units":"mm"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        physical_module.load_packet(str(path))
    path.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="Non-finite"):
        physical_module.load_packet(str(path))
    monkeypatch.setattr(physical_module, "MAX_BYTES", 1)
    with pytest.raises(ValueError, match="oversized"):
        physical_module.load_packet(str(path))


@pytest.mark.parametrize("failure", [None, "mid_batch", "post_commit", "pending", "geometry", "copy"])
def test_wrapper_keeps_full_ownership_and_uses_unchanged_rollback_checks(runtime_harness, physical_packet, failure):  # noqa: F811
    _, doc, db, floor, bar_type, state = runtime_harness
    runtime = importlib.import_module("qm_revit_physical_trial")
    state.fail_at = 2 if failure == "mid_batch" else None
    state.mismatch_after = failure == "post_commit"
    state.commit = "Pending" if failure == "pending" else "Committed"
    state.geometry_blocked = failure == "geometry"
    placement = {"offset_x_mm": 0, "offset_y_mm": 0, "confirmed": True,
                 "axis_depths_mm": dict(zip(("bottom-X", "bottom-Y", "top-X", "top-Y"), (40, 80, 40, 80), strict=True))}
    result = runtime.run_physical_trial(doc, db, floor, physical_packet, {"A500|18": bar_type}, placement,
        lambda: [], lambda: [], copy_confirmed=failure != "copy")
    assert result["schema_version"] == runtime.REPORT_SCHEMA
    assert result["physical_plan"] == physical_packet
    assert result["expected"]["source_zone_count"] == 6
    assert "zone_count" not in result["expected"]
    assert result["execution"]["expected"]["zone_count"] == 5
    assert result["unresolved_intersection_pair_count"] == 1
    assert result["manual_joint_tasks"] == physical_packet["manual_joint_tasks"]
    assert result["placement_eligible"] is False and result["engineering_approval"] is False
    if failure is None:
        assert result["status"] == "passed_rolled_back"
        assert result["execution"]["post_commit_comparison"]["physical_bar_count"] == 11
        assert result["execution"]["restoration"]["verified"]
        assert len(result["temporary_execution_mapping"]) == 5
        assert len(result["temporary_execution_mapping"][0]["bar_sources"][0]["source_refs"]) == 2
    elif failure in ("mid_batch", "post_commit"):
        assert result["status"] == "failed_rolled_back"
        assert result["execution"]["restoration"]["verified"]
    elif failure == "pending":
        assert result["status"] == "rollback_unconfirmed"
        assert state.reads_after_pending == 0
    else:
        assert result["status"] == "blocked_preflight"
        assert not state.made and not state.calls


@pytest.mark.parametrize("cancel", [True, False])
@pytest.mark.parametrize("change_source_after_read", [True, False])
def test_button_cancel_or_missing_types_preserves_actual_host_and_provenance(physical_module, physical_packet, tmp_path, monkeypatch, cancel, change_source_after_read):
    import runpy
    import sys
    from types import ModuleType

    source, output = tmp_path / "план.json", tmp_path / "результат.json"
    source.write_text(json.dumps(physical_packet), encoding="utf-8")
    expected_packet_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    if change_source_after_read:
        original_load = physical_module.load_packet_with_sha256

        def load_then_change(path):
            loaded = original_load(path)
            source.write_text("file changed AFTER validated read", encoding="utf-8")
            return loaded

        monkeypatch.setattr(physical_module, "load_packet_with_sha256", load_then_change)
    probe_module = importlib.import_module("qm_revit_probe")
    trial_module = importlib.import_module("qm_revit_trial")
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
    messages = []

    def alert(message, **_):
        messages.append(message)
        return not cancel

    forms = SimpleNamespace(pick_file=lambda **_: str(source), save_file=lambda **_: str(output), alert=alert)
    pyrevit = ModuleType("pyrevit")
    pyrevit.DB, pyrevit.forms = db, forms
    pyrevit.revit = SimpleNamespace(doc=doc, uidoc=uidoc)
    generic = ModuleType("System.Collections.Generic")
    generic.List = list
    monkeypatch.setitem(sys.modules, "pyrevit", pyrevit)
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", generic)
    monkeypatch.setattr(probe_module, "Probe", lambda *_: SimpleNamespace(issues=[], floor=lambda _: host_data()))
    monkeypatch.setattr(trial_module, "solid_report", lambda *_: {"volume_mm3": 7500000000})
    entry = ROOT / "integrations/pyrevit/QMonitoring.extension/QMonitoring.tab/Diagnostics.panel/PhysicalPlanTrial.pushbutton/script.py"
    runpy.run_path(str(entry))["main"]()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["schema_version"] == "revit-physical-bar-plan-trial-report/v1"
    assert result["status"] == "blocked_setup"
    assert result["host"] == host_data()
    assert result["host_solid"]["volume_mm3"] == 7500000000
    assert result["physical_plan"] == physical_packet
    assert result["manual_joint_tasks"] == physical_packet["manual_joint_tasks"]
    assert result["raw_report_sha256"] == "b" * 64
    assert result["source_report_sha256"] == "a" * 64
    assert result["packet_sha256"]
    assert result["packet_sha256"] == expected_packet_digest
    assert result["packet_sha256_representation"] == "exact-validated-input-bytes"
    assert result["expected"]["source_zone_count"] == 6
    assert "execution" not in result
    if not cancel:
        assert result["missing_bar_types"] == ["A500|18"]
    assert "Нерешённых пересечений" in messages[0]
    assert "нерешённых" in messages[-1].lower()
