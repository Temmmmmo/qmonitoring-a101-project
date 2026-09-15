"""Straight physical-cut transport: no reuse of an old 40d or shape certificate."""
import copy
import json

import pytest

import test_revit_plan_preview as old


@pytest.fixture
def module(monkeypatch):
    return old.module.__wrapped__(monkeypatch)


def cut_packet():
    before_rows, rows, mapping = [], [], []
    for index, direction in enumerate(("bottom-X", "bottom-Y", "top-X", "top-Y")):
        layer, axis = direction.split("-")
        d = {"layer": layer, "axis": axis}
        bar = {"id": "bar-{0}".format(index), "steel_class": "A500", "diameter_mm": 10,
            "longitudinal_mm": [0, 3000], "coordinate_mm": 1000, "source_bar_ids": ["zone/{0}/0".format(index)]}
        before_rows.append({"direction": d, "bars": [copy.deepcopy(bar)]})
        intervals = [[100, 1000], [1800, 2900]] if index == 0 else [[100, 2900]] if index == 3 else [[0, 3000]]
        pieces = []
        for piece_index, interval in enumerate(intervals):
            item = {**bar, "id": bar["id"]+"/outer-piece-"+str(piece_index+1) if len(intervals) > 1 else bar["id"],
                "longitudinal_mm": interval, "original_longitudinal_mm": [0, 3000],
                "original_bar_id": bar["id"], "original_coordinate_mm": 1000,
                "axis_nudged": False, "physically_cut": interval != [0, 3000]}
            pieces.append(item)
        rows.append({"direction": d, "bars": pieces})
        mapping.append({"direction": direction, "source_bar_id": bar["id"],
            "piece_ids": [p["id"] for p in pieces], "policy": "test-outer-boundary-clip"})
    return {"schema_version": "graphic-bar-plan-draft/v1", "units": "mm", "case_id": "synthetic-trim-only",
        "placement_eligible": False, "engineering_approval": False, "geometry_kind": "straight-bars-only",
        "source_stage": "physically-trimmed-to-outer-contour", "source_report_sha256": "a"*64,
        "source_host_report_sha256": "b"*64, "provenance_status": "sha256_recorded",
        "crop_policy_id": "test-outer-boundary-clip", "source_to_revit_xy_mm": [20, 30],
        "binding_source": "explicit research identity", "coverage_policy": "physical-main-leg-presence-NOT-anchorage",
        "original_FE_geometry_changed": False, "source_demand_removed": False,
        "radius_sized_edge_axis_nudge_enabled": True,
        "removed_wholly_external_bars": [],
        "checks": {"anchorage_40d": "fail", "coverage": "pass", "stock_cutting": "fail",
            "outer_boundary": {"status": "fail", "failure_count": 1},
            "collisions_3d": {"status": "fail", "proven_pair_count": 2, "uncertain_pair_count": 1}},
        "before": {"physical_bar_count": 4, "additional_mass_kg": 12000*0.0006165, "directions": before_rows},
        "after": {"physical_bar_count": 5, "additional_mass_kg": 10800*0.0006165, "position_count": 4, "source_zone_count": 4},
        "directions": rows, "piece_mapping": mapping}


def test_full_after_party_keeps_splits_and_unresolved_originals(module):
    value = cut_packet()
    before = copy.deepcopy(value)
    result = module.build_preview_primitives(value, 20, 30)
    assert module._validate_primitives(result) == result
    assert value == before and result["graphic_input"] == value
    assert len(result["bars"]) == 5 and result["summary"]["position_count"] == 4
    assert result["bars"][0]["start_xy_mm"] == [120, 1030]
    assert result["bars"][0]["end_xy_mm"] == [1020, 1030]
    assert result["trim_graphics"]["changed_original_bar_count"] == 2
    assert result["trim_graphics"]["physically_cut_piece_count"] == 3
    assert result["trim_graphics"]["checks"]["anchorage_40d"] == "fail"
    assert not result["engineering_approval"] and not result["placement_eligible"]


def openings_packet():
    packet = cut_packet()
    remove_parent(packet, 0)
    packet["respect_openings"] = True
    packet["source_stage"] = "physically-trimmed-to-outer-and-openings"
    packet["crop_policy_id"] = "user-physical-outer-and-openings-trim-and-split/2026-09-15-v1"
    for row in packet["piece_mapping"]:
        row["policy"] = packet["crop_policy_id"]
    packet["removed_input_bars"] = packet["removed_wholly_external_bars"]
    packet["removed_wholly_external_bars"] = []  # This parent was in a hole, not outside the slab.
    packet["checks"]["material_boundary"] = {"status": "pass", "failure_count": 0}
    packet["checks"]["openings"] = {"status": "pass", "failure_count": 0}
    return packet


def test_openings_transport_and_caption_keep_no_material_removals_and_all_checks(module):
    packet = openings_packet()
    primitive = module.build_preview_primitives(packet, 20, 30)
    module._validate_primitives(primitive)
    assert primitive["trim_graphics"]["removed_input_bar_count"] == 1
    assert primitive["trim_graphics"]["removed_wholly_external_bar_count"] == 0
    assert primitive["trim_graphics"]["respect_openings"] is True
    caption = module._trim_caption(primitive)
    assert "ОТВЕРСТИЯ УЧТЕНЫ РАЗРЕЗАНИЕМ" in caption
    assert "БЕЗ ПЕРЕСЕЧЕНИЯ С МАТЕРИАЛОМ: 1" in caption
    assert "40d" in caption and "fail" in caption


@pytest.mark.parametrize("bad", ["policy", "mapping_policy", "missing_check", "false_pass", "removed", "unknown"])
def test_openings_transport_cannot_hide_new_checks_or_missing_pieces(module, bad):
    packet = openings_packet()
    if bad == "policy":
        packet["crop_policy_id"] = "outer-only"
    elif bad == "mapping_policy":
        packet["piece_mapping"][0]["policy"] = "outer-only"
    elif bad == "missing_check":
        del packet["checks"]["openings"]
    elif bad == "false_pass":
        packet["checks"]["openings"] = {"status": "fail", "failure_count": 1}
    elif bad == "removed":
        packet["removed_input_bars"] = []
    else:
        packet["checks"]["material_boundary"] = {"status": "not_checked", "failure_count": None}
    with pytest.raises(ValueError):
        module.build_preview_primitives(packet, 0, 0)


@pytest.mark.parametrize("delta", [-5, 0, 5])
def test_only_explicit_one_radius_axis_shift_preserves_full_piece_mapping(module, delta):
    packet = cut_packet()
    for piece in packet["directions"][0]["bars"]:
        piece["coordinate_mm"] += delta
        piece["axis_nudged"] = bool(delta)
    result = module.build_preview_primitives(packet, 20, 30)
    assert result["bars"][0]["start_xy_mm"][1] == 1030+delta
    assert result["trim_graphics"]["radius_sized_axis_nudged_piece_count"] == (2 if delta else 0)


def remove_parent(packet, index):
    original = packet["before"]["directions"][index]["bars"][0]
    packet["directions"][index]["bars"] = []
    packet["piece_mapping"][index]["piece_ids"] = []
    packet["removed_wholly_external_bars"].append({"direction": packet["piece_mapping"][index]["direction"],
        "bar_id": original["id"], "reason": "empty-exact-outer-intersection"})
    bars = [bar for row in packet["directions"] for bar in row["bars"]]
    packet["after"]["physical_bar_count"] = len(bars)
    packet["after"]["additional_mass_kg"] = sum((b["longitudinal_mm"][1]-b["longitudinal_mm"][0])*0.0006165 for b in bars)
    packet["after"]["position_count"] = len({b["longitudinal_mm"][1]-b["longitudinal_mm"][0] for b in bars})
    packet["checks"]["outer_boundary"] = {"status": "pass", "failure_count": 0}
    packet["checks"]["collisions_3d"] = {"status": "pass", "proven_pair_count": 0, "uncertain_pair_count": 0}


def test_explicit_zero_piece_parent_is_preserved_in_full_before_inventory(module):
    packet = cut_packet()
    remove_parent(packet, 0)
    primitive = module.build_preview_primitives(packet, 0, 0)
    module._validate_primitives(primitive)
    assert len(primitive["bars"]) == 3
    assert primitive["trim_graphics"]["removed_wholly_external_bar_count"] == 1
    assert primitive["trim_graphics"]["before_inventory"] == packet["before"]["directions"]
    assert "для 1 исходных стержней не оставлено ни одного отрезка" in module._trim_caption(primitive)
    assert packet["piece_mapping"][0]["piece_ids"] == []


@pytest.mark.parametrize("change", [
    lambda p: p["removed_wholly_external_bars"].clear(),
    lambda p: p["removed_wholly_external_bars"].append(copy.deepcopy(p["removed_wholly_external_bars"][0])),
    lambda p: p["removed_wholly_external_bars"][0].__setitem__("bar_id", "not-an-original"),
    lambda p: p["removed_wholly_external_bars"][0].__setitem__("bar_id", "bar-1"),
    lambda p: p["removed_wholly_external_bars"][0].__setitem__("reason", ""),
])
def test_missing_or_false_full_removal_record_never_hides_a_parent(module, change):
    packet = cut_packet()
    remove_parent(packet, 0)
    change(packet)
    with pytest.raises(ValueError):
        module.build_preview_primitives(packet, 0, 0)


@pytest.mark.parametrize("change", [
    lambda p: p.__setitem__("geometry_kind", "U-bars"),
    lambda p: p["directions"][0]["bars"][0].__setitem__("shape_kind", "U"),
    lambda p: p["directions"][0]["bars"][0].__setitem__("segments", [{"arc": True}]),
    lambda p: p["before"].__setitem__("additional_mass_kg", 100),
    lambda p: p["after"].__setitem__("additional_mass_kg", 100),
    lambda p: p["after"].__setitem__("physical_bar_count", 4),
    lambda p: p["after"].__setitem__("position_count", 1),
    lambda p: p["directions"].pop(),
    lambda p: p["piece_mapping"].pop(),
    lambda p: p["piece_mapping"][0].__setitem__("piece_ids", []),
    lambda p: p["piece_mapping"][0]["piece_ids"].append(p["piece_mapping"][0]["piece_ids"][0]),
    lambda p: p["directions"][0]["bars"][0].__setitem__("original_bar_id", "unknown"),
    lambda p: p["directions"][0]["bars"][0].__setitem__("original_longitudinal_mm", [0, 9999]),
    lambda p: p["directions"][0]["bars"][0].__setitem__("source_bar_ids", ["foreign"]),
    lambda p: p["directions"][0]["bars"][0].__setitem__("physically_cut", False),
    lambda p: p["directions"][0]["bars"][0].__setitem__("coordinate_mm", 1002),
    lambda p: p["directions"][0]["bars"][0].__setitem__("original_coordinate_mm", 999),
    lambda p: p["directions"][0]["bars"][0].__setitem__("coordinate_mm", float("nan")),
    lambda p: p["checks"].__setitem__("anchorage_40d", "approved"),
    lambda p: p["checks"]["outer_boundary"].__setitem__("status", "pass"),
    lambda p: p["checks"]["collisions_3d"].__setitem__("status", "pass"),
    lambda p: p["checks"]["collisions_3d"].__setitem__("status", "not_checked"),
    lambda p: p["checks"]["outer_boundary"].__setitem__("failure_count", True),
    lambda p: p.__setitem__("source_demand_removed", True),
    lambda p: p.__setitem__("placement_eligible", True),
])
def test_bad_graphic_transport_never_claims_success(module, change):
    value = cut_packet()
    change(value)
    with pytest.raises(ValueError):
        module.build_preview_primitives(value, 0, 0)


@pytest.mark.parametrize("different_axes", [False, True])
def test_disabled_nudge_or_divergent_piece_axes_fail(module, different_axes):
    packet = cut_packet()
    for piece in packet["directions"][0]["bars"]:
        piece["coordinate_mm"] += 5
        piece["axis_nudged"] = True
    if different_axes:
        packet["directions"][0]["bars"][1]["coordinate_mm"] -= 10
    else:
        packet["radius_sized_edge_axis_nudge_enabled"] = False
    with pytest.raises(ValueError):
        module.build_preview_primitives(packet, 0, 0)


def test_overlapping_or_outside_pieces_fail_even_if_mass_is_updated(module):
    for interval in ([900, 2900], [-100, 2900]):
        packet = cut_packet()
        packet["directions"][0]["bars"][1]["longitudinal_mm"] = interval
        count = sum(b["longitudinal_mm"][1]-b["longitudinal_mm"][0] for d in packet["directions"] for b in d["bars"])
        packet["after"]["additional_mass_kg"] = count*0.0006165
        with pytest.raises(ValueError):
            module.build_preview_primitives(packet, 0, 0)


def test_io_and_post_validation_primitives_cannot_hide_changes(module, tmp_path):
    path = tmp_path / "отрезки.json"
    path.write_text(json.dumps(cut_packet(), ensure_ascii=False), encoding="utf-8")
    packet, digest = module.load_preview_input(str(path))
    assert len(digest) == 64 and packet == cut_packet()
    primitive = module.build_preview_primitives(packet, 0, 0)
    primitive["trim_graphics"]["checks"]["anchorage_40d"] = "pass"
    with pytest.raises(ValueError, match="changed"):
        module._validate_primitives(primitive)


def test_unknown_backend_geometry_is_explicitly_unknown_not_zero(module):
    packet = cut_packet()
    packet["checks"]["outer_boundary"] = {"status": "not_checked", "failure_count": None}
    packet["checks"]["collisions_3d"] = {"status": "not_checked", "proven_pair_count": None, "uncertain_pair_count": None}
    primitive = module.build_preview_primitives(packet, 0, 0)
    assert "not_checked" in module._trim_caption(primitive)
    assert "None = не проверено, не ноль" in module._trim_caption(primitive)
    assert "XY отличается" in module._trim_caption(primitive)


@pytest.fixture
def harness(module, monkeypatch):
    h = old.harness.__wrapped__(module, monkeypatch)
    h.primitives = module.build_preview_primitives(cut_packet(), 20, 30)
    create = h.DB.TextNote.Create

    def note_create(*args):
        note = create(*args)
        note.Text = note.text
        return note

    h.DB.TextNote.Create = note_create
    return h


def test_committed_all_piece_coordinates_and_required_disclaimer_readback(module, harness):
    report = old.run(module, harness, confirmed=True)
    assert report["status"] == "graphic_preview_created", report["issues"]
    assert report["readback"]["checked_bar_line_count"] == 5
    assert report["trim_graphics"]["checks"] == cut_packet()["checks"]
    caption = harness.doc.elements[report["trim_caption_id"]].Text
    assert "40d" in caption and "fail" in caption and "Раскрой 11700" in caption
    assert "БЕЗ анкеровки" in caption and "НЕ АРМАТУРА" in caption
    assert "остаточных непрошедших стержней: 1" in caption
    assert "доказанных пар: 2; непроверенных пар: 1" in caption
    assert any(row["style"] == "physically_cut" and row["physically_cut"] for row in report["bar_element_mapping"])
    assert not report["placement_eligible"] and report["structural_elements_created"] == 0


def test_entirely_external_inventory_draws_explicit_empty_result_without_ghosts(module, harness):
    packet = cut_packet()
    for index in range(4):
        remove_parent(packet, index)
    harness.primitives = module.build_preview_primitives(packet, 20, 30)
    # Independent count-based empty native diagnostic; no synthetic negative counts.
    original_check = module.check_preview_host
    try:
        module.check_preview_host = lambda *args: {"bars": [], "outside_count": 0, "unknown_count": 0, "contained_count": 0}
        report = old.run(module, harness, confirmed=True)
    finally:
        module.check_preview_host = original_check
    assert report["status"] == "graphic_preview_created", report["issues"]
    assert report["readback"]["checked_bar_line_count"] == 0 and report["bar_element_mapping"] == []
    assert report["expected"]["physical_bar_count"] == report["expected"]["additional_mass_kg"] == report["expected"]["position_count"] == 0
    assert report["trim_graphics"]["removed_wholly_external_bar_count"] == 4
    assert len(report["trim_graphics"]["before_inventory"]) == 4
    assert not report["placement_eligible"]


@pytest.mark.parametrize("tamper", ["caption", "coordinates"])
def test_wrong_committed_40d_caption_or_actual_piece_rolls_back(module, harness, monkeypatch, tamper):
    original = old.Transaction.Commit

    def commit(transaction):
        status = original(transaction)
        for element in harness.doc.elements.values():
            if tamper == "caption" and hasattr(element, "Text") and "ФИЗИЧЕСКАЯ ОБРЕЗКА" in element.Text:
                element.Text = element.Text.replace("fail", "pass")
            elif tamper == "coordinates" and isinstance(element, old.DetailCurve):
                element.GeometryCurve.ends[0].X += 1
        return status

    monkeypatch.setattr(old.Transaction, "Commit", commit)
    report = old.run(module, harness, confirmed=True)
    assert report["status"] == "failed_rolled_back", report["issues"]
    assert harness.doc.elements == harness.before and not report["created_annotation_ids"]
