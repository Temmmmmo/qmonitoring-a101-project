"""Native Rebar Review MVP contracts; doubles are not a live Revit run."""

from __future__ import annotations

from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "integrations/pyrevit/QMonitoring.extension/lib"
S1 = ROOT / "artifacts/s1_flat_mvp_2026_09_15/revalidated-v3/graphic-bar-plan.json"


@pytest.fixture(scope="module")
def contract():
    sys.path.insert(0, str(LIB))
    return importlib.import_module("qm_rebar_review")


def host_data(size=10000):
    def loop(z):
        points = [[0, 0, z], [size, 0, z], [size, size, z], [0, size, z]]
        return [
            {"kind": "Line", "start_mm": a, "end_mm": points[(i + 1) % 4], "length_mm": size}
            for i, a in enumerate(points)
        ]

    return {
        "element_id": 99,
        "covers": {key: {"distance_mm": 40} for key in ("top", "bottom", "other")},
        "top_faces": [
            {"plane": {"origin_mm": [0, 0, 300], "normal": [0, 0, 1]}, "edge_loops": [loop(300)]}
        ],
        "bottom_faces": [
            {"plane": {"origin_mm": [0, 0, 0], "normal": [0, 0, -1]}, "edge_loops": [loop(0)]}
        ],
    }


def primitives():
    bars = []
    for index, direction in enumerate(("bottom-X", "bottom-Y", "top-X", "top-Y")):
        start, end = (
            ([1000, 1000 + index * 500], [3000, 1000 + index * 500])
            if direction.endswith("X")
            else ([1000 + index * 500, 1000], [1000 + index * 500, 3000])
        )
        bars.append(
            {
                "direction": direction,
                "bar_id": "bar-" + direction,
                "start_xy_mm": start,
                "end_xy_mm": end,
                "diameter_mm": 12,
                "steel_class": "A500",
                "source_refs": [direction],
            }
        )
    mass = sum(0.000006165 * 12**2 * 2000 for _ in bars)
    return {
        "input_schema": "graphic-bar-plan-draft/v1",
        "placement_eligible": False,
        "engineering_approval": False,
        "bars": bars,
        "summary": {
            "physical_bar_count": 4,
            "additional_mass_kg": mass,
            "position_count": 1,
            "source_zone_count": 4,
        },
    }


def native(plan):
    rows = []
    for index, run in enumerate(plan["runs"]):
        line = run["axes"][0]
        rows.append(
            {
                "element_id": 1000 + index,
                "host_id": 99,
                "quantity": 1,
                "number_of_bar_positions": 1,
                "layout_rule": "Single",
                "hook_type_ids": [-1, -1],
                "review_bar_id": run["bar_id"],
                "review_direction": run["direction"],
                "bar_type": {"element_id": 7, "nominal_diameter_mm": 12, "model_diameter_mm": 12},
                "bars": [
                    {
                        "position_index": 0,
                        "curves": [
                            {
                                "kind": "Line",
                                "start_mm": line["start_mm"][:],
                                "end_mm": line["end_mm"][:],
                                "length_mm": 2000,
                            }
                        ],
                    }
                ],
            }
        )
    return rows


def make_plan(contract):
    host = contract.flat_outer_host(host_data())
    types = {"A500|12": {"element_id": 7, "nominal_diameter_mm": 12, "model_diameter_mm": 12}}
    return contract.make_review_plan(
        primitives(), host, types, dict.fromkeys(contract.DIRECTIONS, 50)
    )


def test_flat_native_outer_plan_and_complete_native_readback(contract):
    plan = make_plan(contract)
    result = contract.compare_native(plan, native(plan))
    assert len(plan["runs"]) == result["physical_bar_count"] == 4
    assert result["status"] == "matches" and result["tolerance_mm"] == 0.01
    assert "not Revit material density" in result["mass_formula"]


def test_native_edges_can_be_reversed_and_reordered_without_changing_contour(contract):
    data = host_data()
    for side in ("top_faces", "bottom_faces"):
        edges = data[side][0]["edge_loops"][0]
        for edge in edges[::2]:
            edge["start_mm"], edge["end_mm"] = edge["end_mm"], edge["start_mm"]
        data[side][0]["edge_loops"][0] = [edges[2], edges[0], edges[3], edges[1]]
    result = contract.flat_outer_host(data)
    assert {tuple(p) for p in result["outer_xy_mm"]} == {
        (0, 0),
        (10000, 0),
        (10000, 10000),
        (0, 10000),
    }


def test_vertex_split_narrow_concave_bay_cannot_enter_bar_body(contract):
    polygon = [
        [-1, -1],
        [11, -1],
        [11, 10],
        [3, 10],
        [3, 6],
        [3, 4],
        [2, 4],
        [2, 6],
        [2, 10],
        [-1, 10],
    ]
    assert contract._corridor_contained([0, 4], [10, 4], 2, polygon) is False
    # Contact along an exterior edge, without a positive interior incursion,
    # remains allowed by the no-cover outer-body MVP profile.
    assert (
        contract._corridor_contained([0, 4], [10, 4], 2, [[0, 2], [10, 2], [10, 6], [0, 6]]) is True
    )


def test_only_exact_excluded_cover_read_errors_are_optional(contract):
    runtime = importlib.import_module("qm_revit_rebar_review")
    cover = {"section": "floor/cover/top", "message": "missing cover"}
    report = {}
    probe = SimpleNamespace(issues=[cover])
    assert runtime._blocking_read_issues(probe, report) == []
    assert report["optional_cover_metadata_issues"] == [cover]
    for section in (
        "floor/top_faces",
        "floor/bottom_faces",
        "floor/type",
        "floor/cover/unknown",
        "bar_type",
    ):
        error = {"section": section, "message": "incomplete"}
        probe.issues = [cover, error]
        assert runtime._blocking_read_issues(probe, report) == [error]


@pytest.mark.parametrize(
    "change", ("missing", "moved", "host", "type", "diameter", "comment", "length", "curve")
)
def test_native_readback_tamper_is_rejected(contract, change):
    plan, rows = make_plan(contract), native(make_plan(contract))
    if change == "missing":
        rows.pop()
    elif change == "moved":
        rows[0]["bars"][0]["curves"][0]["start_mm"][0] += 0.02
    elif change == "host":
        rows[0]["host_id"] = 1
    elif change == "type":
        rows[0]["bar_type"]["element_id"] = 8
    elif change == "diameter":
        rows[0]["bar_type"]["model_diameter_mm"] = 11
    elif change == "comment":
        rows[0]["review_bar_id"] = "another"
    elif change == "length":
        rows[0]["bars"][0]["curves"][0]["length_mm"] += 0.02
    else:
        rows[0]["bars"][0]["curves"][0]["kind"] = "Arc"
    if change == "missing":
        with pytest.raises(ValueError):
            contract.compare_native(plan, rows)
    else:
        assert contract.compare_native(plan, rows)["status"] == "differs"


@pytest.mark.parametrize(
    "change", ("slope", "step", "curve", "disconnected", "outside", "depth", "bar-type")
)
def test_unsupported_host_or_party_blocks_whole_plan(contract, change):
    data, party = host_data(), primitives()
    types = {"A500|12": {"element_id": 7, "nominal_diameter_mm": 12, "model_diameter_mm": 12}}
    depths = dict.fromkeys(contract.DIRECTIONS, 50)
    if change == "slope":
        data["top_faces"][0]["plane"]["normal"] = [0.1, 0, 1]
    elif change == "step":
        data["top_faces"].append(deepcopy(data["top_faces"][0]))
    elif change == "curve":
        data["top_faces"][0]["edge_loops"][0][0]["kind"] = "Arc"
    elif change == "disconnected":
        data["top_faces"][0]["edge_loops"].append(deepcopy(data["top_faces"][0]["edge_loops"][0]))
    elif change == "outside":
        party["bars"][0]["start_xy_mm"][0] = -1
    elif change == "depth":
        depths["top-X"] = -1
    else:
        types["A500|12"]["model_diameter_mm"] = 10
    with pytest.raises(ValueError):
        contract.make_review_plan(party, contract.flat_outer_host(data), types, depths)


def test_real_s1_graphics_is_complete_and_keeps_visible_failures(contract):
    if not S1.is_file():
        pytest.skip("Local S1 draft unavailable")
    packet = json.loads(S1.read_text(encoding="utf-8"))
    result = contract.validated_graphics(packet, 0, 0)
    assert len(result["bars"]) == result["summary"]["physical_bar_count"] == 1897
    assert all(
        result["trim_graphics"]["checks"][key] == "fail"
        for key in ("coverage", "anchorage_40d", "stock_cutting")
    )


def test_python2_grammar_and_no_save_sync_delete_or_blanket_obstacle_scan():
    from lib2to3.pgen2 import driver
    from lib2to3 import pygram, pytree

    parser = driver.Driver(pygram.python_grammar, convert=pytree.convert)
    for relative in ("qm_rebar_review.py", "qm_revit_rebar_review.py"):
        content = (LIB / relative).read_text(encoding="utf-8")
        parser.parse_string(content + "\n")
        for forbidden in (
            ".Save(",
            ".SaveAs(",
            "SynchronizeWithCentral(",
            ".Delete(",
            "find_obstacles(",
        ):
            assert forbidden not in content
    button = LIB.parent / "QMonitoring.tab/Review.panel/RebarReview.pushbutton/script.py"
    parser.parse_string(button.read_text(encoding="utf-8") + "\n")


@pytest.fixture
def runtime_harness(monkeypatch, contract):
    runtime = importlib.import_module("qm_revit_rebar_review")
    state = SimpleNamespace(
        ids={7, 99},
        rebars={},
        calls=[],
        fail_at=None,
        tamper_after=False,
        read_count=0,
        commit="Committed",
        issue_section=None,
    )
    document = SimpleNamespace(
        IsFamilyDocument=False,
        IsReadOnly=False,
        IsModifiable=False,
        IsWorkshared=False,
        Title="COPY",
        Application=SimpleNamespace(VersionNumber="2024"),
    )

    class Id:
        def __init__(self, value):
            self.IntegerValue = self.Value = value

    class Floor:
        Document = document

    class BarType:
        Document = document

    class Rebar:
        pass

    floor, bar_type = Floor(), BarType()
    floor.Id, bar_type.Id = Id(99), Id(7)
    document.GetElement = (
        lambda value: floor if value.Value == 99 else state.rebars.get(value.Value)
    )
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
            self.status, self.group = "Uninitialized", "REBAR REVIEW" in name

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

        def Assimilate(self):
            self.status = "Committed"
            state.calls.append("assimilate")
            return self.status

        def RollBack(self):
            state.calls.append("rollback_group" if self.group else "rollback_transaction")
            state.ids, state.rebars, self.status = {7, 99}, {}, "RolledBack"
            return self.status

        def Dispose(self):
            state.calls.append("dispose_group" if self.group else "dispose_transaction")

        def GetFailureHandlingOptions(self):
            return Options()

        def SetFailureHandlingOptions(self, _):
            pass

    db = SimpleNamespace(
        Floor=Floor,
        ElementId=Id,
        Structure=SimpleNamespace(RebarBarType=BarType, Rebar=Rebar),
        Transaction=Scope,
        TransactionGroup=Scope,
        BuiltInParameter=SimpleNamespace(ALL_MODEL_INSTANCE_COMMENTS="comments"),
        TransactionStatus=SimpleNamespace(
            Started="Started", Committed="Committed", RolledBack="RolledBack"
        ),
    )

    class Probe:
        def __init__(self, doc, DB):
            self.doc, self.DB, self.issues = doc, DB, []

        def floor(self, _):
            if state.issue_section:
                self.issues.append(
                    {"section": state.issue_section, "message": "injected incomplete read"}
                )
            return deepcopy(host_data())

        def bar_type(self, _):
            return {"element_id": 7, "nominal_diameter_mm": 12, "model_diameter_mm": 12}

    def create(probe, host, typ, run, factory):
        if len(state.rebars) == state.fail_at:
            raise ValueError("injected create failure")
        value = 1000 + len(state.rebars)
        item = Rebar()
        item.Id = Id(value)
        item._comment = None
        item.get_Parameter = lambda _: SimpleNamespace(
            IsReadOnly=False, Set=lambda text: setattr(item, "_comment", text) is None
        )
        state.rebars[value] = item
        state.ids.add(value)
        return item

    plan_holder = {}
    original_plan = runtime.make_review_plan

    def capture(*args):
        result = original_plan(*args)
        plan_holder["value"] = result
        return result

    def read_created(probe, ids, identities):
        state.read_count += 1
        rows = native(plan_holder["value"])
        if state.tamper_after and state.read_count == 2:
            rows[0]["bars"][0]["curves"][0]["end_mm"][0] += 0.02
        return rows

    monkeypatch.setattr(runtime, "Probe", Probe)
    monkeypatch.setattr(runtime, "document_ids", lambda *_: set(state.ids))
    monkeypatch.setattr(runtime, "create_trial_rebar", create)
    monkeypatch.setattr(runtime, "_read_created", read_created)
    monkeypatch.setattr(runtime, "make_review_plan", capture)
    monkeypatch.setattr(runtime, "failure_recorder", lambda *_: object())
    monkeypatch.setattr(
        runtime,
        "inspect_trial_worksharing",
        lambda *_: {"status": "not_applicable", "mode": "non_workshared"},
    )
    monkeypatch.setattr(runtime, "authorize_trial_worksharing", lambda *_: None)
    monkeypatch.setattr(runtime, "assign_new_rebar_workset", lambda *_: None)
    monkeypatch.setattr(runtime, "verify_new_rebar_worksets", lambda *_: None)
    monkeypatch.setattr(runtime, "finish_trial_worksharing", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime, "ownership_notice", lambda _: "No worksharing changes")
    return runtime, document, db, floor, bar_type, state


@pytest.mark.parametrize(
    "mode", ("keep", "decline", "mid-batch", "post-tamper", "pending", "cover", "geometry-read")
)
def test_transaction_keep_or_full_rollback(runtime_harness, contract, mode):
    runtime, doc, db, floor, typ, state = runtime_harness
    state.fail_at = 2 if mode == "mid-batch" else None
    state.tamper_after = mode == "post-tamper"
    state.commit = "Pending" if mode == "pending" else "Committed"
    state.issue_section = (
        "floor/cover/top"
        if mode == "cover"
        else "floor/top_faces"
        if mode == "geometry-read"
        else None
    )
    source = primitives()
    source.update(
        {
            "input_schema": "graphic-bar-plan-draft/v1",
            "case_id": "case",
            "trim_graphics": {"checks": {"coverage": "fail"}},
            "source_blockers": ["not-approved"],
        }
    )
    result = runtime.run_rebar_review(
        doc,
        db,
        floor,
        source,
        {"A500|12": typ},
        dict.fromkeys(contract.DIRECTIONS, 50),
        lambda: [],
        mode in ("keep", "cover"),
        copy_confirmed=True,
    )
    assert result["placement_eligible"] is result["engineering_approval"] is False
    if mode in ("keep", "cover"):
        assert result["status"] == "kept_diagnostic_rebar_review"
        assert len(result["kept_element_ids"]) == 4 and "assimilate" in state.calls
        assert result["batch_limit"]["maximum_physical_bar_count"] == 5000
        assert result["timing_seconds"]["total"] >= 0
        assert state.ids != {7, 99}
    elif mode == "pending":
        assert result["status"] == "rollback_unconfirmed" and "rollback_group" not in state.calls
    elif mode == "geometry-read":
        assert result["status"] == "blocked_preflight" and not state.calls
    else:
        assert result["status"] == "failed_rolled_back" and result["restoration"]["verified"]
        assert state.ids == {7, 99} and "assimilate" not in state.calls


@pytest.mark.parametrize("tamper", [False, True])
def test_auto_runtime_provenance_and_blocked_preflight(runtime_harness, contract, tamper):
    runtime, doc, db, floor, typ, state = runtime_harness
    source = primitives()
    policy = contract.computed_axis_depth_policy(
        source,
        contract.flat_outer_host(host_data()),
        {"A500|12": dict(element_id=7, nominal_diameter_mm=12, model_diameter_mm=12)},
    )
    policy["user_confirmed"] = True
    depths = deepcopy(policy["actual_depths_mm"])
    if tamper:
        depths["bottom-Y"] -= 1
    report = runtime.run_rebar_review(
        doc,
        db,
        floor,
        source,
        {"A500|12": typ},
        depths,
        lambda: [],
        True,
        copy_confirmed=True,
        axis_depth_policy=policy,
    )
    assert report["axis_depth_policy"]["mode"] == "auto"
    assert report["computed_axis_depths_mm"] == policy["computed_depths_mm"]
    if tamper:
        assert report["status"] == "blocked_preflight" and not state.calls
        assert report["axis_depth_revalidation"]["status"] == "not_checked"
    else:
        assert report["status"] == "kept_diagnostic_rebar_review"
        assert (
            report["axis_depth_revalidation"]["status"]
            == "consistent_with_current_native_host_and_types"
        )


def test_code_only_package_is_deterministic_and_dependency_complete(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    packager = importlib.import_module("package_revit_rebar_review")
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    packager.build_package(first)
    packager.build_package(second)
    assert first.read_bytes() == second.read_bytes()
    with ZipFile(first) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["version"] == "0.1.3" and manifest["creates_structural_rebar"] is True
        assert manifest["review_only"] is True
        assert not any(name.endswith((".rvt", ".rfa", ".dxf", ".shk")) for name in names)
        assert set(name for name in names if name.endswith(".json")) == {"manifest.json"}
        for module in packager.MODULES:
            assert "QMonitoringRebarReview.extension/lib/" + module in names


def test_exact_element_id_and_comment_binding_are_read_back(monkeypatch):
    runtime = importlib.import_module("qm_revit_rebar_review")

    class Rebar:
        def __init__(self, text):
            self.text = text

        def get_Parameter(self, _):
            return SimpleNamespace(AsString=lambda: self.text)

    expected = 'QM REBAR REVIEW MVP ["bottom-X","bar-1"]'
    rebar = Rebar(expected)
    probe = SimpleNamespace(
        DB=SimpleNamespace(
            ElementId=lambda value: value,
            Structure=SimpleNamespace(Rebar=Rebar),
            BuiltInParameter=SimpleNamespace(ALL_MODEL_INSTANCE_COMMENTS="comments"),
        ),
        doc=SimpleNamespace(GetElement=lambda _: rebar),
    )
    monkeypatch.setattr(runtime, "read_trial_rebar", lambda *_: {"element_id": 10})
    rows = runtime._read_created(probe, [10], {10: expected})
    assert rows[0]["review_bar_id"] == "bar-1"
    rebar.text = 'QM REBAR REVIEW MVP ["bottom-X","another"]'
    with pytest.raises(ValueError, match="comment differs"):
        runtime._read_created(probe, [10], {10: expected})
    monkeypatch.setattr(runtime, "read_trial_rebar", lambda *_: {"element_id": 11})
    rebar.text = expected
    with pytest.raises(ValueError, match="element identity"):
        runtime._read_created(probe, [10], {10: expected})
