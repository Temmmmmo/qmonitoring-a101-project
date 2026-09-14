"""Graphic-only Revit integration with offline doubles, NOT Windows acceptance."""
from __future__ import annotations

import copy
import ast
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_full_plate_trial import host_data
from test_physical_plan_trial import small_physical_packet

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "integrations/pyrevit/QMonitoring.extension/lib"


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(LIB))
    return importlib.import_module("qm_revit_plan_preview")


def draft_input():
    old = small_physical_packet()
    raw = {}
    for row in old["directions"]:
        axis = 0 if row["direction"].endswith("X") else 1
        raw[row["direction"]] = [{"id": owner["bar_id"], "steel_class": run["steel_class"],
            "diameter_mm": run["diameter_mm"], "coordinate_mm": run["start_xy_mm"][1-axis]+index*run["spacing_mm"],
            "longitudinal_mm": [run["start_xy_mm"][axis], run["end_xy_mm"][axis]],
            "source_bar_ids": ["{zone_id}/{component_index}/{bar_index}".format(**ref) for ref in owner["source_refs"]]}
            for run in row["runs"] for index, owner in enumerate(run["bar_sources"])]
    raw["bottom-X"][0]["coordinate_mm"] = 1050
    return {"schema_version": "physical-bar-relocation-draft/v1", "units": "mm", "placement_eligible": False,
        "case_id": old["case_id"], "source_packet_sha256": "a"*64, "source_report_sha256": "b"*64,
        "source_host_report_sha256": "c"*64, "source_to_revit_xy_mm": [20, 30], "binding_source": "explicit offline",
        "raw_bars_by_direction": raw, "original_source_zones": old["source_zones"],
        "expected": {k: old["expected"][k] for k in ("physical_bar_count", "additional_mass_kg", "source_zone_count", "position_count")},
        "status": "blocked_working_host", "structural_placement_supported": False,
        "correction": {"policy_id": "offline-test-only", "moved_bar_count": 1,
            "moves": [{"direction": "bottom-X", "bar_id": "bar-0", "axis_shift_mm": 50,
                "longitudinal_shift_mm": 0, "opening_indexes": [0], "opening_bboxes_mm": [[1400, 990, 1500, 1010]],
                "original_axis_mm": 1000, "corrected_axis_mm": 1050}], "host_blocked_before": 3,
            "host_blocked_after": 2, "new_same_direction_body_pairs": 0}}


def test_full_old_plan_not_filtered_or_mutated(module):
    packet = small_physical_packet()
    before = copy.deepcopy(packet)
    result = module.build_preview_primitives(packet, 20, 30)
    module._validate_primitives(result)
    assert packet == before
    assert len(result["bars"]) == 11
    assert result["summary"]["source_zone_count"] == 6
    assert result["summary"]["execution_group_count"] == 5
    assert result["bars"][0]["start_xy_mm"] == [1020, 1030]
    assert result["bars"][2]["start_xy_mm"] == [1320, 1030]
    assert sum(b["intersection"] for b in result["bars"]) == 2
    assert len(result["bars"][0]["source_refs"]) == 2
    assert not result["placement_eligible"] and not result["engineering_approval"]


def test_shifted_draft_separate_adapter_preserves_owners_count_mass_and_marks(module):
    packet = draft_input()
    before = copy.deepcopy(packet)
    primitives = module.build_preview_primitives(packet, 20, 30)
    assert packet == before
    assert primitives["original_source_zones"] == packet["original_source_zones"]
    assert len(primitives["bars"]) == 11
    assert primitives["bars"][0]["start_xy_mm"] == [1020, 1080]
    assert primitives["bars"][0]["original_axis_mm"] == 1000
    assert primitives["bars"][0]["relocated"]
    assert sum(b["relocated"] for b in primitives["bars"]) == 1
    assert primitives["intersection_pair_count"] == 1
    assert primitives["summary"]["additional_mass_kg"] == small_physical_packet()["expected"]["additional_mass_kg"]
    assert primitives["recorded_binding_matches_entered"]
    other = module.build_preview_primitives(packet, 0, 0)
    assert not other["recorded_binding_matches_entered"]


@pytest.mark.parametrize("change", ["approved", "supported", "missing_direction", "mass", "quantity", "positions",
    "zones", "owner_missing", "owner_duplicate", "id_duplicate", "nan", "bool", "too_short", "source_axis",
    "steel", "weaker", "move_count", "move_unknown", "move_wrong_axis", "hash", "extra_field", "coordinate_shape"])
def test_draft_rejects_corrupt_graphic_contract(module, change):
    packet = draft_input()
    bar = packet["raw_bars_by_direction"]["bottom-X"][0]
    if change == "approved":
        packet["placement_eligible"] = True
    elif change == "supported":
        packet["structural_placement_supported"] = True
    elif change == "missing_direction":
        packet["raw_bars_by_direction"].pop("top-Y")
    elif change == "mass":
        packet["expected"]["additional_mass_kg"] += 1
    elif change == "quantity":
        packet["expected"]["physical_bar_count"] -= 1
    elif change == "positions":
        packet["expected"]["position_count"] += 1
    elif change == "zones":
        packet["expected"]["source_zone_count"] -= 1
    elif change == "owner_missing":
        bar["source_bar_ids"].pop()
    elif change == "owner_duplicate":
        bar["source_bar_ids"].append(bar["source_bar_ids"][0])
    elif change == "id_duplicate":
        packet["raw_bars_by_direction"]["top-Y"][1]["id"] = "bar-0"
    elif change == "nan":
        bar["coordinate_mm"] = float("nan")
    elif change == "bool":
        bar["diameter_mm"] = True
    elif change == "too_short":
        bar["longitudinal_mm"][0] = 1100
    elif change == "source_axis":
        packet["original_source_zones"][0]["components"][0]["axis_coordinates_mm"][0] = 1200
    elif change == "steel":
        bar["steel_class"] = "A400"
    elif change == "weaker":
        bar["diameter_mm"] = 16
    elif change == "move_count":
        packet["correction"]["moved_bar_count"] = 0
    elif change == "move_unknown":
        packet["correction"]["moves"][0]["bar_id"] = "unknown"
    elif change == "move_wrong_axis":
        packet["correction"]["moves"][0]["corrected_axis_mm"] = 1060
    elif change == "hash":
        packet["source_packet_sha256"] = "z"*64
    elif change == "extra_field":
        bar["is_approved"] = True
    else:
        packet["source_to_revit_xy_mm"] = [0, 0, 0]
    with pytest.raises((ValueError, KeyError, TypeError)):
        module.build_preview_primitives(packet, 0, 0)


@pytest.mark.parametrize("packet_factory", [small_physical_packet, draft_input])
def test_loader_unicode_hash_bounds_duplicates_and_finite(module, tmp_path, monkeypatch, packet_factory):
    packet = packet_factory()
    path = tmp_path / "план.json"
    content = json.dumps(packet, ensure_ascii=False).encode("utf-8-sig")
    path.write_bytes(content)
    data, digest = module.load_preview_input(str(path))
    assert data == packet and digest == hashlib.sha256(content).hexdigest()
    path.write_text('{"units":"mm","units":"mm"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        module.load_preview_input(str(path))
    path.write_text('{"value": NaN}', encoding="utf-8")
    with pytest.raises(ValueError, match="Non-finite"):
        module.load_preview_input(str(path))
    path.write_text('{"ignored_nested_value": {"opening_bboxes_mm": [1e999]}}', encoding="utf-8")
    with pytest.raises(ValueError, match="Non-finite"):
        module.load_preview_input(str(path))
    path.write_bytes(content)
    monkeypatch.setattr(module, "MAX_INPUT_BYTES", len(content)-1)
    with pytest.raises(ValueError, match="oversized"):
        module.load_preview_input(str(path))


def test_layout_contains_outside_bars_not_just_host_bbox(module):
    primitives = module.build_preview_primitives(small_physical_packet(), 100000, -5000)
    layout = module.panel_layout(primitives, host_data())
    assert layout["width_mm"] >= 103400
    assert len(layout["panels"]) == 4
    assert layout["panels"]["top-X"]["translation_xy_mm"][1] < 0


class Id:
    def __init__(self, value):
        self.IntegerValue = self.Value = value

    def __hash__(self):
        return hash(self.Value)

    def __eq__(self, other):
        return isinstance(other, Id) and self.Value == other.Value


Id.InvalidElementId = Id(-1)


class Disposable:
    def Dispose(self):
        self.disposed = True


class Point:
    def __init__(self, x, y, z):
        self.X, self.Y, self.Z = x, y, z


Point.BasisZ = Point(0, 0, 1)


class Line(Disposable):
    def __init__(self, start, end):
        self.ends = [start, end]

    def GetEndPoint(self, index):
        return self.ends[index]

    CreateBound = staticmethod(lambda start, end: Line(start, end))


class DetailCurve:
    pass


class View(Disposable):
    def SetElementOverrides(self, identifier, style):
        assert self.doc.IsModifiable
        assert self.doc.elements[identifier.Value].OwnerViewId == self.Id
        self.overrides[identifier.Value] = style.color


class Style(Disposable):
    def SetProjectionLineColor(self, color):
        self.color = color
        return self

    def SetProjectionLineWeight(self, weight):
        self.weight = weight
        return self


class Options(Disposable):
    def __getattr__(self, name):
        if name.startswith("Set"):
            return lambda *args: self
        raise AttributeError(name)


class Transaction(Disposable):
    def __init__(self, doc, name):
        self.doc, self.name, self.status = doc, name, "Uninitialized"
        doc.scopes.append(self)

    def Start(self):
        self.before = dict(self.doc.elements)
        self.status = "Started"
        self.doc.IsModifiable = True
        return self.status

    def GetStatus(self):
        return self.status

    def GetFailureHandlingOptions(self):
        return Options()

    def SetFailureHandlingOptions(self, options):
        pass

    def Commit(self):
        self.status = self.doc.commit_status
        self.doc.IsModifiable = False
        if self.status == "RolledBack":
            self.doc.elements = self.before
        if self.doc.tamper_commit and self.status == "Committed":
            for curve in self.doc.elements.values():
                if isinstance(curve, DetailCurve):
                    curve.GeometryCurve.ends[0].Z += 1
        return self.status

    def RollBack(self):
        if self.doc.rollback_fail:
            raise RuntimeError("rollback failed")
        self.doc.elements = self.before
        self.doc.IsModifiable = False
        self.status = "RolledBack"
        return self.status


class Group(Transaction):
    def Start(self):
        result = super().Start()
        self.doc.IsModifiable = False
        return result

    def Assimilate(self):
        self.status = "Committed"
        return self.status


class Collector:
    def __init__(self, doc):
        self.doc = doc

    def OfClass(self, cls):
        return [x for x in self.doc.elements.values() if isinstance(x, cls)]


class Family:
    pass


class TextType:
    def get_Parameter(self, kind):
        return SimpleNamespace(AsDouble=lambda: 2.5/304.8)


class Floor:
    pass


@pytest.fixture
def harness(module, monkeypatch):
    doc = SimpleNamespace(IsFamilyDocument=False, IsReadOnly=False, IsModifiable=False, IsWorkshared=False,
        Application=SimpleNamespace(VersionNumber="2024", ShortCurveTolerance=0.002),
        commit_status="Committed", tamper_commit=False, rollback_fail=False, scopes=[], elements={}, next_id=1000)
    floor = Floor()
    floor.Id, floor.Document = Id(999), doc
    doc.elements[999] = floor
    family = Family()
    family.Id, family.ViewFamily = Id(10), "Drafting"
    text = TextType()
    text.Id = Id(11)
    doc.elements[10], doc.elements[11] = family, text
    doc.GetElement = lambda identifier: doc.elements.get(identifier.Value)
    doc.Regenerate = lambda: None

    def add(element, owner=None):
        assert doc.IsModifiable
        doc.next_id += 1
        element.Id = Id(doc.next_id)
        element.OwnerViewId = owner
        doc.elements[element.Id.Value] = element
        return element

    def view_create(document, type_id):
        assert document is doc and type_id == Id(10)
        view = View()
        view.doc, view.overrides = doc, {}
        return add(view)

    def curve_create(view, curve):
        element = DetailCurve()
        element.GeometryCurve = curve
        return add(element, view.Id)

    def note_create(document, view_id, point, text, type_id):
        return add(SimpleNamespace(text=text, position=point), view_id)

    View.Create = staticmethod(view_create)
    doc.Create = SimpleNamespace(NewDetailCurve=curve_create)
    DB = SimpleNamespace(Floor=Floor, ElementId=Id, Line=Line, XYZ=Point, DetailCurve=DetailCurve, ViewDrafting=View,
        TextNote=SimpleNamespace(Create=note_create), ViewFamilyType=Family, TextNoteType=TextType,
        ViewFamily=SimpleNamespace(Drafting="Drafting"), FilteredElementCollector=Collector,
        BuiltInParameter=SimpleNamespace(TEXT_SIZE="text-size"), OverrideGraphicSettings=Style,
        Color=lambda *values: values, Transaction=Transaction, TransactionGroup=Group,
        TransactionStatus=SimpleNamespace(Started="Started", Committed="Committed", RolledBack="RolledBack"),
        IFailuresPreprocessor=object, FailureProcessingResult=SimpleNamespace(Continue="continue", ProceedWithRollBack="rollback"))
    monkeypatch.setattr(module, "Probe", lambda document, db: SimpleNamespace(issues=[], floor=lambda floor: copy.deepcopy(host_data())))
    geometry = {"solid": object(), "geometry_owner": Disposable(), "options": Disposable(), "curved_edge_occurrences": 0,
        "outlines": [{"start_xy_mm": [0, 0], "end_xy_mm": [5000, 0]},
                     {"start_xy_mm": [2000, 2000], "end_xy_mm": [2000, 2300]}],
        "outline_scope": "all-edges"}
    monkeypatch.setattr(module, "read_preview_host", lambda *args: geometry)

    def checks(probe, host, native, bars, factory, progress):
        module._progress(progress, "preflight", 0, len(bars))
        return {"bars": [{"direction": b["direction"], "bar_id": b["bar_id"],
            "status": "outside" if i == 0 else "unknown" if i == 1 else "contained"} for i, b in enumerate(bars)],
            "outside_count": 1, "unknown_count": 1, "contained_count": len(bars)-2}

    monkeypatch.setattr(module, "check_preview_host", checks)
    primitives = module.build_preview_primitives(small_physical_packet(), 0, 0)
    return SimpleNamespace(doc=doc, DB=DB, floor=floor, primitives=primitives, before=dict(doc.elements), native=geometry)


def run(module, h, **kwargs):
    return module.create_plan_preview(h.doc, h.DB, h.floor, h.primitives, list, **kwargs)


def test_new_view_full_axes_color_and_committed_readback(module, harness):
    h = harness
    report = run(module, h, confirmed=True)
    assert report["status"] == "graphic_preview_created", report["issues"]
    assert report["readback"]["status"] == "matches"
    assert report["readback"]["checked_bar_line_count"] == 11
    assert report["created_view_id"] in h.doc.elements
    assert set(h.before).issubset(h.doc.elements)
    assert all(h.doc.elements[key] is value for key, value in h.before.items())
    assert len([e for e in h.doc.elements.values() if isinstance(e, View)]) == 1
    assert all(row["element_id"] in h.doc.elements for row in report["bar_element_mapping"])
    assert report["bar_element_mapping"][0]["style"] == "outside"
    assert report["bar_element_mapping"][1]["style"] == "unknown"
    assert sum(row["style"] == "intersection" for row in report["bar_element_mapping"]) == 2
    assert "НЕ АРМАТУРА" in report["view_name"]
    assert not report["placement_eligible"] and report["structural_elements_created"] == 0
    assert not report["checkout_requested"] and not report["save_requested"]
    assert h.native["geometry_owner"].disposed and h.native["options"].disposed


def test_per_direction_counts_and_colors_without_filtering(module, harness, monkeypatch):
    statuses = {"bottom-X": "outside", "bottom-Y": "unknown",
                "top-X": "contained", "top-Y": "outside"}
    labels = {"bottom-X": "НИЗ X", "bottom-Y": "НИЗ Y", "top-X": "ВЕРХ X", "top-Y": "ВЕРХ Y"}
    rows = [{"direction": bar["direction"], "bar_id": bar["bar_id"], "status": statuses[bar["direction"]]}
            for bar in harness.primitives["bars"]]
    check = {"bars": rows, **{status+"_count": sum(row["status"] == status for row in rows)
                             for status in ("outside", "unknown", "contained")}}
    monkeypatch.setattr(module, "check_preview_host", lambda *args: copy.deepcopy(check))
    report = run(module, harness, confirmed=True)
    assert report["status"] == "graphic_preview_created", report["issues"]
    assert len(report["bar_element_mapping"]) == len(rows)
    notes = {getattr(element, "text", "") for element in harness.doc.elements.values()}
    for direction in module.DIRECTIONS:
        count = sum(row["direction"] == direction for row in rows)
        outside = count if statuses[direction] == "outside" else 0
        unknown = count if statuses[direction] == "unknown" else 0
        assert f"{labels[direction]}: {count} стержней; вне контура {outside}; не проверено {unknown}" in notes
    for row in report["bar_element_mapping"]:
        assert row["host_status"] == statuses[row["direction"]]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("axis", [0, 1, 2])
@pytest.mark.parametrize("delta, valid", [(0, True), (0.009, True), (0.011, False),
                                        (float("nan"), False), (float("inf"), False)])
def test_endpoint_loop_retains_tolerance_orientation_and_nonfinite_rejection(module, reverse, axis, delta, valid):
    expected = [[0, 0, 0], [100, 200, 0]]
    actual = copy.deepcopy(expected)
    actual[0][axis] += delta
    if reverse:
        actual.reverse()
    assert module._matching_endpoints(actual, expected) is valid


def test_committed_readback_accepts_reversed_line_direction(module, harness):
    report = run(module, harness, confirmed=True)
    assert report["status"] == "graphic_preview_created"
    for row in report["bar_element_mapping"]:
        harness.doc.elements[row["element_id"]].GeometryCurve.ends.reverse()
    result = module._readback_preview(harness.doc, harness.DB, harness.primitives, report)
    assert result["checked_bar_line_count"] == len(harness.primitives["bars"])


def test_ironpython_workaround_has_no_nested_comprehension_scopes():
    # Structural guard, NOT a claim to execute IronPython on the CI CPython VM.
    # Real 2.7.12 trace: MutableTuple`16 -> MutableTuple`4 in _draw counts.
    tree = ast.parse((LIB / "qm_revit_plan_preview.py").read_text(encoding="utf-8"))
    scopes = (ast.GeneratorExp, ast.ListComp, ast.SetComp, ast.DictComp)
    for node in ast.walk(tree):
        if isinstance(node, scopes):
            assert not any(child is not node and isinstance(child, scopes) for child in ast.walk(node)), node.lineno
        if isinstance(node, ast.FunctionDef) and node.name in ("_draw", "_readback_preview", "_matching_endpoints"):
            assert not any(isinstance(child, scopes) for child in ast.walk(node)), node.name


def test_replay_real_902_native_statuses_through_full_drawing_and_readback(module, harness, monkeypatch):
    draft_path = ROOT / "artifacts/small_opening_delivery_2026_09_14/verified128/physical-bar-relocation-draft.json"
    report_path = ROOT / "qmonitoring-plan-preview-20260914-162843-904000.json"
    if not draft_path.exists() or not report_path.exists():
        pytest.skip("Local unpublished Revit report and complete relocation draft are absent")
    previous = json.loads(report_path.read_text(encoding="utf-8-sig"))
    packet, digest = module.load_preview_input(str(draft_path))
    assert digest == previous["packet_sha256"]
    assert previous["worksharing"]["mode"] == "detached_with_worksets"
    harness.primitives = module.build_preview_primitives(packet, *previous["offset_xy_mm"])
    # Replay recorded native statuses; this does not rerun Autodesk Solid APIs.
    monkeypatch.setattr(module, "check_preview_host", lambda *args: copy.deepcopy(previous["geometry_check"]))
    report = run(module, harness, confirmed=True)
    assert report["status"] == "graphic_preview_created", report["issues"]
    assert report["readback"]["checked_bar_line_count"] == 902
    assert report["expected"]["additional_mass_kg"] == pytest.approx(2989.953486)
    rows = report["bar_element_mapping"]
    assert len(rows) == len({(row["direction"], row["bar_id"]) for row in rows}) == 902
    assert sum(row["host_status"] == "outside" for row in rows) == 405
    assert sum(row["host_status"] == "contained" for row in rows) == 497
    assert not any(row["host_status"] == "unknown" for row in rows)
    assert not report["placement_eligible"] and report["structural_elements_created"] == 0


@pytest.mark.parametrize("mode", ["detached_with_worksets", "live_local", "non_workshared"])
def test_worksharing_preview_no_floor_checkout_or_active_workset_change(module, harness, monkeypatch, mode):
    h = harness
    h.doc.IsWorkshared = mode != "non_workshared"
    h.doc.GetWorksetTable = lambda: SimpleNamespace(GetActiveWorksetId=lambda: Id(8))
    monkeypatch.setattr(module, "classify_document", lambda *args: {"status": "supported", "mode": mode})
    # Deliberately provide no WorksharingUtils / checkout / Save / Sync APIs.
    report = run(module, h, confirmed=True)
    assert report["status"] == "graphic_preview_created", report["issues"]
    assert report["active_workset_unchanged"] and not report["checkout_requested"]


@pytest.mark.parametrize("mode", ["cloud_workshared", "central_workshared", "server_workshared"])
def test_unsupported_worksharing_stops_before_transaction(module, harness, monkeypatch, mode):
    monkeypatch.setattr(module, "classify_document", lambda *args: {"status": "blocked", "mode": mode, "issues": [mode]})
    report = run(module, harness, confirmed=True)
    assert report["status"] == "blocked_preflight"
    assert not harness.doc.scopes and harness.doc.elements == harness.before


def test_without_consent_no_transaction(module, harness):
    report = run(module, harness)
    assert report["status"] == "blocked_preflight"
    assert not harness.doc.scopes and harness.doc.elements == harness.before


@pytest.mark.parametrize("stage", ["preflight", "drawing"])
def test_cancel_never_retains_partial_view(module, harness, stage):
    def progress(name, index, total):
        return (name != "preflight") if stage == "preflight" else not (name != "preflight" and index >= 3)
    report = run(module, harness, confirmed=True, progress=progress)
    assert report["status"] == ("cancelled" if stage == "preflight" else "cancelled_rolled_back")
    assert harness.doc.elements == harness.before
    assert report["created_view_id"] is None and not report["bar_element_mapping"]


def test_commit_endpoint_tamper_rolls_back_outer_group(module, harness):
    harness.doc.tamper_commit = True
    report = run(module, harness, confirmed=True)
    assert report["status"] == "failed_rolled_back"
    assert harness.doc.elements == harness.before
    assert report["created_view_id"] is None
    assert "endpoints differ" in report["issues"][0]["message"]


@pytest.mark.parametrize("commit_status", ["RolledBack", "Pending"])
def test_failed_or_pending_commit_never_success(module, harness, commit_status):
    harness.doc.commit_status = commit_status
    report = run(module, harness, confirmed=True)
    assert report["status"] == ("failed_rolled_back" if commit_status == "RolledBack" else "rollback_unconfirmed")
    assert report["created_view_id"] is None


def test_draw_exception_and_failed_rollback_not_claimed_success(module, harness, monkeypatch):
    def fail(*args):
        raise RuntimeError("draw exploded")
    harness.doc.rollback_fail = True
    monkeypatch.setattr(module, "_draw", fail)
    report = run(module, harness, confirmed=True)
    assert report["status"] == "rollback_unconfirmed"
    assert report["created_view_id"] is None


def test_draw_new_draft_includes_shift_annotation(module, harness):
    harness.primitives = module.build_preview_primitives(draft_input(), 20, 30)
    report = run(module, harness, confirmed=True)
    assert report["status"] == "graphic_preview_created", report["issues"]
    assert report["readback"]["checked_bar_line_count"] == 11
    assert any("сдвинуто 1" in getattr(item, "text", "") for item in harness.doc.elements.values())


def test_current_full_902_packet_local_regression(module):
    path = ROOT / "artifacts/revit_delivery_2026_09_14/host-aware-v1/physical-bar-plan-trial.json"
    if not path.exists():
        pytest.skip("Local unpublished physical source packet is absent")
    packet, _ = module.load_preview_input(str(path))
    primitive = module.build_preview_primitives(packet, 0, 0)
    assert len(primitive["bars"]) == 902
    assert primitive["summary"]["additional_mass_kg"] == pytest.approx(2989.953486, abs=1e-6)
    assert primitive["summary"]["source_zone_count"] == 139
    assert primitive["intersection_pair_count"] == 12


def test_preview_source_has_no_structural_mutation_api():
    runtime = (LIB / "qm_revit_plan_preview.py").read_text(encoding="utf-8")
    button = (LIB.parent / "QMonitoring.tab/Diagnostics.panel/PlanPreview.pushbutton/script.py").read_text(encoding="utf-8")
    for forbidden in (".CheckoutElements(", ".SynchronizeWithCentral(", ".Save(", ".SaveAs(", ".SetActiveWorksetId(",
                      ".NewModelCurve(", ".CreateFromCurves(", ".SetUnobscuredInView("):
        assert forbidden not in runtime+button
    assert "ViewDrafting.Create(" in runtime
    assert "NewDetailCurve(" in runtime


class NativeLoop(Disposable):
    def __init__(self):
        self.lines = []

    def Append(self, curve):
        self.lines.append(curve)


class NativeList(list):
    def Add(self, value):
        self.append(value)


@pytest.fixture
def native_check(module):
    from shapely.geometry import Polygon, box
    state = SimpleNamespace(raise_boolean=False, envelopes=[], temporaries=[])

    def extrusion(loops, normal, height):
        polygon = Polygon([(line.ends[0].X*304.8, line.ends[0].Y*304.8) for line in loops[0].lines])
        element = Disposable()
        element.polygon, element.height = polygon, height*304.8
        state.envelopes.append(element)
        state.temporaries.append(element)
        return element

    def difference(envelope, solid, operation):
        if state.raise_boolean:
            raise RuntimeError("native Boolean failed")
        element = Disposable()
        element.Volume = sum(envelope.polygon.difference(poly).area*h for poly, h in solid.sections)/(304.8**3)
        state.temporaries.append(element)
        return element

    DB = SimpleNamespace(XYZ=Point, Line=Line, CurveLoop=NativeLoop,
        UnitUtils=SimpleNamespace(ConvertToInternalUnits=lambda value, unit: value/304.8),
        UnitTypeId=SimpleNamespace(Millimeters="mm"),
        GeometryCreationUtilities=SimpleNamespace(CreateExtrusionGeometry=extrusion),
        BooleanOperationsUtils=SimpleNamespace(ExecuteBooleanOperation=difference),
        BooleanOperationsType=SimpleNamespace(Difference="difference"))
    probe = SimpleNamespace(DB=DB, mm=lambda value: value*304.8)
    footprint = box(0, 0, 5000, 5000).difference(box(2000, 980, 2100, 1020))
    geometry = {"solid": SimpleNamespace(sections=[(footprint, 300)])}
    return state, probe, geometry


def test_native_geometry_marks_all_inside_hole_bbox_and_boolean_unknown(module, native_check):
    state, probe, geometry = native_check
    bars = module.build_preview_primitives(small_physical_packet(), 0, 0)["bars"]
    report = module.check_preview_host(probe, host_data(), geometry, bars, NativeList)
    assert len(report["bars"]) == 11
    assert report["outside_count"] >= 2  # No omission of the axes through a hole.
    assert report["bars"][0]["status"] == "outside"
    assert report["unknown_count"] == 0
    assert all(x.disposed for x in state.temporaries)
    lo_x, lo_y, hi_x, hi_y = state.envelopes[0].polygon.bounds
    assert (lo_x, lo_y, hi_x, hi_y) == pytest.approx((975, 966, 3025, 1034))
    state.raise_boolean = True
    report = module.check_preview_host(probe, host_data(), geometry, bars, NativeList)
    assert report["unknown_count"] == 11 and report["outside_count"] == 0
    assert all(row["reason"] == "native_boolean_failed" for row in report["bars"])
    outside = copy.deepcopy(bars[:1])
    outside[0]["start_xy_mm"][0] = -10
    report = module.check_preview_host(probe, host_data(), geometry, outside, NativeList)
    assert report["outside_count"] == 1 and report["unknown_count"] == 0


def test_native_whole_height_test_does_not_miss_upper_step(module, native_check):
    from shapely.geometry import box
    _, probe, geometry = native_check
    geometry["solid"].sections = [(box(0, 0, 5000, 5000), 150), (box(0, 0, 5000, 5000).difference(box(2000, 900, 2100, 1100)), 150)]
    bars = module.build_preview_primitives(small_physical_packet(), 0, 0)["bars"][:1]
    report = module.check_preview_host(probe, host_data(), geometry, bars, NativeList)
    assert report["outside_count"] == 1
    assert report["bars"][0]["outside_volume_mm3"] > 0


def test_native_cancel_does_not_return_a_partial_check(module, native_check):
    _, probe, geometry = native_check
    bars = module.build_preview_primitives(small_physical_packet(), 0, 0)["bars"]
    with pytest.raises(module.PreviewCancelled):
        module.check_preview_host(probe, host_data(), geometry, bars, NativeList, lambda stage, i, n: i < 2)


def test_read_host_retains_all_face_edges_and_deduplicates_only_projection(module):
    class Solid:
        Volume = 1

    class Instance:
        pass

    class Geometry(list, Disposable):
        pass

    def edge(a, b):
        return SimpleNamespace(AsCurve=lambda: Line(Point(*a), Point(*b)))

    solid = Solid()
    solid.Faces = [SimpleNamespace(EdgeLoops=[[edge((0, 0, 0), (10, 0, 0)), edge((10, 0, 0), (10, 5, 0))]]),
        SimpleNamespace(EdgeLoops=[[edge((0, 0, 10), (10, 0, 10)), edge((2, 2, 5), (3, 2, 5)), edge((2, 2, 0), (2, 2, 10))]])]
    geometry = Geometry([solid])
    probe = SimpleNamespace(DB=SimpleNamespace(Options=Options, ViewDetailLevel=SimpleNamespace(Fine="fine"),
        Solid=Solid, GeometryInstance=Instance, Line=Line), point=lambda p: [p.X, p.Y, p.Z])
    result = module.read_preview_host(probe, SimpleNamespace(get_Geometry=lambda options: geometry))
    assert len(result["outlines"]) == 3  # Duplicate projected edge + exact vertical vanish, step retained.
    assert {tuple(e["start_xy_mm"]) for e in result["outlines"]} == {(0, 0), (10, 0), (2, 2)}
    assert result["curved_edge_occurrences"] == 0
    assert "ALL" in result["outline_scope"]
    with pytest.raises(ValueError, match="Nested"):
        module.read_preview_host(probe, SimpleNamespace(get_Geometry=lambda options: Geometry([solid, Instance()])))
    with pytest.raises(ValueError, match="exactly one"):
        module.read_preview_host(probe, SimpleNamespace(get_Geometry=lambda options: Geometry([solid, solid])))
