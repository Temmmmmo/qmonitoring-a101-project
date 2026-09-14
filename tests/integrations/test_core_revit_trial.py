"""Core -> packet -> isolated API doubles; real Revit 0.4.0 still requires specialist execution."""
from __future__ import annotations

import copy
import importlib
import json
import math

import pytest

from rebar.application.core_revit_trial import make_core_revit_trial_sample
from test_revit_trial import (
    EXTENSION, CurveList, Element, api as api, floor_geometry, modules as modules,
    trial as trial, trial_api as trial_api,
)

SAMPLE = EXTENSION.parent / "samples/core-axis-trial.json"


@pytest.fixture
def core(request):
    request.getfixturevalue("trial")
    return importlib.import_module("qm_core_trial")


@pytest.fixture
def case(request):
    return request.getfixturevalue("trial_api")


def type18():
    return {"element_id": 165160, "nominal_diameter_mm": 18, "model_diameter_mm": 18}


def test_delivered_sample_is_generated_by_real_core_and_retains_draft_gates(core):
    data = make_core_revit_trial_sample()
    assert core.load_core_input(str(SAMPLE)) == data
    export = data["core_export"]
    assert not data["placement_eligible"] and not export["checks"]["export_eligible"]
    assert export["metrics"]["zone_count"] == export["metrics"]["component_count"] == 1
    assert export["metrics"]["uniform_run_count"] == 2
    assert export["components"][0]["axis_coordinates_mm"] == [100, 200, 400, 500, 700, 800]
    plan = core.make_core_plan(floor_geometry()[0], type18(), data)
    assert [p["bar_count"] for p in plan["runs"]] == [3, 3]
    assert [p["length_mm"] for p in plan["runs"]] == [5340, 5340]
    assert plan["runs"][0]["axes"][0]["start_mm"] == [1280, 1100, -34]
    assert plan["runs"][1]["axes"][-1]["end_mm"] == [6620, 1800, -34]
    assert all(p["anchorage_added_mm"] == 0 for p in plan["runs"])
    assert plan["mass_kg"] == pytest.approx(63.9986184)
    assert not plan["background_created"]


@pytest.mark.parametrize("length,width", [(4700, 800), (3900, 4100), (3900, 300), (0, 800)])
def test_core_generator_refuses_outside_lab_profile(length, width):
    with pytest.raises(ValueError):
        make_core_revit_trial_sample(length_mm=length, width_mm=width)


def test_core_plan_rejects_resolved_ids_that_do_not_match_binding(core):
    wrong = type18()
    wrong["element_id"] = 165163
    with pytest.raises(ValueError, match="IDs differ"):
        core.make_core_plan(floor_geometry()[0], wrong, make_core_revit_trial_sample())


@pytest.mark.parametrize("length,width", [(1000, 600), (2200, 900), (4000, 1500)])
def test_changed_core_geometry_propagates_to_actual_created_runs(case, length, width):
    adapter, DB, doc, elements, calls, _ = case
    before = set(elements)
    data = make_core_revit_trial_sample(length_mm=length, width_mm=width)
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, core_trial_input=data)
    assert report["status"] == "passed_rolled_back", report
    assert calls.count("create") == 2 and calls.count("commit") == 1
    assert report["post_commit_comparison"]["mass_matches"]
    for b in report["post_commit_readback"]["sets"]:
        for bar in b["bars"]:
            c = bar["curves"][0]
            assert math.dist(c["start_mm"], c["end_mm"]) == pytest.approx(length + 1440)
    assert set(elements) == before


def test_core_trial_creates_both_runs_then_commits_rereads_and_restores(case):
    adapter, DB, doc, elements, calls, _ = case
    before = set(elements)
    data = make_core_revit_trial_sample()
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, core_trial_input=data)
    assert report["status"] == "passed_rolled_back", report
    assert report["input"] == data
    assert report["temporary_element_ids"] == [800001, 800003]
    assert report["commit"]["status"] == "committed" and not report["commit_failures"]
    assert report["post_commit_comparison"]["status"] == "matches"
    assert report["post_commit_comparison"]["physical_bar_count"] == 6
    assert report["post_commit_comparison"]["mass_from_readback_kg"] == pytest.approx(63.9986184)
    assert report["group_rollback"]["status"] == "rolled_back" and report["restoration"]["verified"]
    assert calls == ["group_start", "start", "create", "layout", "create", "layout", "regenerate"] + [
        "read_axis"] * 6 + ["commit"] + ["read_axis"] * 6 + ["dispose", "group_rollback", "group_dispose"]
    bars = [bar for s in report["post_commit_readback"]["sets"] for bar in s["bars"]]
    ys = sorted(bar["curves"][0]["start_mm"][1] for bar in bars)
    assert ys == pytest.approx([1100, 1200, 1400, 1500, 1700, 1800])
    assert [b - a for a, b in zip(ys, ys[1:])] == pytest.approx([100, 200, 100, 200, 100])
    assert set(elements) == before and not report["placement_eligible"]
    json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("failure", ["create", "second_create", "layout", "second_layout", "regenerate",
    "readback", "excluded", "shift", "warning", "error", "commit_exception", "commit_rollback",
    "post_commit_missing", "post_commit_readback", "post_commit_second_shift"])
def test_core_partial_failure_rolls_back_all_created_runs_and_types(case, failure):
    adapter, DB, doc, elements, calls, controls = case
    controls.fail = failure
    before = set(elements)
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, core_trial_input=make_core_revit_trial_sample())
    assert report["status"] == "failed_rolled_back", report
    assert report["restoration"]["verified"] and set(elements) == before
    assert calls[-2:] == ["group_rollback", "group_dispose"]
    if failure == "post_commit_second_shift":
        assert report["comparison"]["status"] == "matches"
        assert report["post_commit_comparison"]["status"] == "differs"
        assert report["post_commit_comparison"]["runs"][0]["status"] == "matches"
        assert report["post_commit_comparison"]["runs"][1]["status"] == "differs"
    if failure in ("second_create", "second_layout"):
        assert calls.count("create") == 2 and "commit" not in calls


@pytest.mark.parametrize("failure,status", [("commit_pending", "rollback_unconfirmed"),
    ("group_rollback", "rollback_unconfirmed"), ("group_leak", "restoration_failed")])
def test_core_uncertain_rollback_never_claims_model_restoration(case, failure, status):
    adapter, DB, doc, _, calls, controls = case
    controls.fail = failure
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, core_trial_input=make_core_revit_trial_sample())
    assert report["status"] == status
    assert not report.get("restoration", {}).get("verified", False)
    if failure == "commit_pending":
        assert "post_commit_readback" not in report and "group_rollback" not in calls


@pytest.mark.parametrize("path,value", [
    (("mode",), "commit"), (("placement_eligible",), True), (("units",), "m"),
    (("binding", "host_id"), 1), (("binding", "bar_type_id"), 165163),
    (("binding", "background_action"), "create"), (("binding", "offset_x_mm"), 0),
    (("binding", "offset_y_mm"), 13900), (("binding", "z_policy"), "dxf"),
    (("core_export", "checks", "export_eligible"), True),
    (("core_export", "checks", "blocking_check_ids"), []),
    (("core_export", "direction", "axis"), "Y"),
    (("core_export", "components", 0, "bar_count"), True),
    (("core_export", "components", 0, "bar_count"), 6.0),
    (("core_export", "components", 0, "uniform_runs", 1, "first_axis_mm"), 100),
    (("core_export", "components", 0, "uniform_runs", 0, "actual_step_mm"), 150),
    (("core_export", "components", 0, "axis_coordinates_mm"), [100, 250, 400, 550, 700, 850]),
    (("core_export", "components", 0, "placement", "origin_mm"), None),
    (("core_export", "components", 0, "placement", "axis_depth_from_face_mm"), 37.5),
    (("core_export", "components", 0, "installed_length_mm"), 6780),
    (("core_export", "components", 0, "mass_kg"), float("nan")),
    (("core_export", "components", 0, "diameter_mm"), 25),
    (("core_export", "recipe", "additions"), [{"step": 150, "diameter": 18}, {"step": 300, "diameter": 25}]),
])
def test_core_invalid_input_stops_before_any_transaction(case, path, value):
    adapter, DB, doc, _, calls, _ = case
    data = make_core_revit_trial_sample()
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, core_trial_input=data)
    assert report["status"] == "blocked_preflight" and report["issues"] and not calls


@pytest.mark.parametrize("change", ["no_copy", "both_inputs", "wrong_type", "wrong_diameter", "obstacle"])
def test_core_preflight_keeps_reference_copy_type_and_obstacle_guards(case, change):
    adapter, DB, doc, elements, calls, controls = case
    if change == "wrong_type":
        elements[165160] = Element(165160)
    elif change == "wrong_diameter":
        elements[165160].BarModelDiameter = 25 / 304.8
    elif change == "obstacle":
        controls.obstacles.append(Element(99, "Existing bar"))
    report = adapter.run_trial(doc, DB, CurveList, copy_confirmed=change != "no_copy",
        core_trial_input=make_core_revit_trial_sample(), trial_input={} if change == "both_inputs" else None)
    assert report["status"] == "blocked_preflight" and not calls


@pytest.mark.parametrize("payload", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'null',
                                    b'[]', b'\xff', b' ' * 65537])
def test_core_loader_refuses_bad_json_without_touching_file(core, tmp_path, payload):
    path = tmp_path / "схема.json"
    path.write_bytes(payload)
    with pytest.raises(ValueError):
        core.load_core_input(str(path))
    assert path.read_bytes() == payload


def test_union_comparison_rejects_duplicate_or_missing_sets(core, case):
    adapter, DB, doc, _, _, _ = case
    r = adapter.run_trial(doc, DB, CurveList, copy_confirmed=True, core_trial_input=make_core_revit_trial_sample())
    b = copy.deepcopy(r["post_commit_readback"])
    b["sets"][1]["element_id"] = b["sets"][0]["element_id"]
    assert core.compare_core_trial(r["plan"], b)["status"] == "differs"
    b["sets"].pop()
    assert core.compare_core_trial(r["plan"], b)["status"] == "differs"


def test_real_030_post_commit_report_independently_rechecked(request):
    _, geometry = request.getfixturevalue("trial")
    folder = EXTENSION.parents[2] / "revit_info/030"
    path = folder / "qmonitoring-json-trial-20260909-110220-350000.json"
    reference_path = folder / "qmonitoring-reference-20260909-110200-880000.json"
    if not path.is_file() or not reference_path.is_file():
        pytest.skip("Private real 0.3.0 reports are not distributed")
    original = path.read_bytes()
    r = json.loads(original)
    reference = json.loads(reference_path.read_bytes())
    assert r["probe_version"] == "0.3.0" and r["status"] == "passed_rolled_back"
    assert r["commit"] == {"status": "committed", "returned": "Committed", "final": "Committed"}
    assert r["group_rollback"] == {"status": "rolled_back", "returned": "RolledBack", "final": "RolledBack"}
    assert r["restoration"] == {"added_ids": [], "removed_ids": [], "reference_unchanged": True, "verified": True}
    assert r["readback"] == r["post_commit_readback"]
    assert r["reference_before"]["area"] == reference["area"]
    assert r["reference_before"]["floor"] == reference["floor"]
    assert not r["issues"] and not r["commit_failures"] and not r["placement_eligible"]
    assert not r["document"]["is_modified_before"] and not r["document"]["is_modified_after"]
    plan = geometry.make_trial_plan(reference["floor"], r["readback"]["bar_type"], r["input"])
    assert plan == r["plan"]
    for key in ("readback", "post_commit_readback"):
        check = geometry.compare_trial(plan, r[key])
        assert check["status"] == "matches" and len(check["checks"]) == 55
        for bar in r[key]["bars"]:
            curve = bar["curves"][0]
            assert math.dist(curve["start_mm"], curve["end_mm"]) == pytest.approx(3900)
            assert reference["floor"]["top_faces"][0]["plane"]["origin_mm"][2] - curve["start_mm"][2] - 12.5 == 25
    assert path.read_bytes() == original
