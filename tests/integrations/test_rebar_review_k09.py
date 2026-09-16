"""Bounded K09 dual-native-boundary support and pre-dialog diagnostics."""

import importlib.util
import importlib
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "integrations/pyrevit/QMonitoring.extension/lib"
sys.path.insert(0, str(LIB))
review = importlib.import_module("qm_rebar_review")


def host():
    return runpy.run_path(str(ROOT / "tests/integrations/test_rebar_review_mvp.py"))["host_data"]()


def test_collinear_subdivision_is_not_a_different_native_boundary():
    data = host()
    edges = data["bottom_faces"][0]["edge_loops"][0]
    first = edges.pop(0)
    middle = [5000, 0, 0]
    edges.extend([dict(first, end_mm=middle), dict(first, start_mm=middle)])
    result = review.flat_outer_host(data)
    assert result["outer_boundaries_equivalent"] is True
    assert len(result["top_outer_xy_mm"]) == 4 and len(result["bottom_outer_xy_mm"]) == 5


def test_actual_k09_native_snapshot_dual_contour_and_recess():
    path = ROOT / "qmonitoring-working-host-20260914-122250-368000.json"
    if not path.is_file():
        pytest.skip("Local actual K09 native snapshot unavailable")
    data = json.loads(path.read_text())["host"]
    result = review.flat_outer_host(data)
    assert result["host_id"] == 11020633 and result["thickness_mm"] == 200
    assert not result["outer_boundaries_equivalent"]
    top, bottom = result["top_outer_xy_mm"], result["bottom_outer_xy_mm"]
    assert len(top) == 57 and len(bottom) == 75
    assert review._corridor_contained([15850, 11300], [16050, 11300], 6, bottom)
    assert not review._corridor_contained([15850, 11300], [16050, 11300], 6, top)
    assert all(review._corridor_contained([2000, 2000], [4000, 2000], 6, p) for p in (top, bottom))
    data["element_id"] = 12345
    assert (
        review.flat_outer_host(data)["outer_profile"]
        == "flat200-top-subset-bottom-dual-exterior-mvp/v1"
    )
    doubles = runpy.run_path(str(ROOT / "tests/integrations/test_rebar_review_mvp.py"))
    party = doubles["primitives"]()
    party["bars"] = [party["bars"][0]]
    party["bars"][0].update(start_xy_mm=[2000, 2000], end_xy_mm=[4000, 2000])
    party["summary"].update(physical_bar_count=1, additional_mass_kg=0.000006165 * 12**2 * 2000)
    types = {"A500|12": {"element_id": 7, "nominal_diameter_mm": 12, "model_diameter_mm": 12}}
    plan = review.make_review_plan(party, result, types, dict.fromkeys(review.DIRECTIONS, 50))
    assert len(plan["runs"]) == 1
    party["bars"][0].update(start_xy_mm=[15850, 11300], end_xy_mm=[16050, 11300])
    with pytest.raises(ValueError, match="top OUTER"):
        review.make_review_plan(party, result, types, dict.fromkeys(review.DIRECTIONS, 50))


def test_polygon_subset_does_not_skip_vertex_crossing_concave_bay():
    outer = [[0, 0], [10, 0], [10, 10], [6, 10], [6, 6], [5, 6], [5, 10], [0, 10]]
    assert not review._polygon_subset([[1, 6], [9, 6], [9, 9], [1, 9]], outer)
    assert not review._polygon_subset([[1, 7], [9, 7], [9, 9], [1, 9]], outer)


def test_native_conversion_noise_is_not_real_point_one_mm_protrusion():
    polygon = [[3.335e-10, 0], [10000, 0], [10000, 10000], [3.335e-10, 10000]]
    assert review._corridor_contained([0, 8800], [8543.556, 8800], 5, polygon)
    assert not review._corridor_contained([-0.1, 8800], [8543.556, 8800], 5, polygon)
    assert review.OUTER_COMPUTATIONAL_EPS_MM == 0.000001


def test_real_old_975_inventory_has_only_genuine_notch_blocker():
    snapshot = ROOT / "qmonitoring-working-host-20260914-122250-368000.json"
    packet_path = (
        ROOT
        / "artifacts/source_views_2026_09_15/boundary-trim-pruned-web-v1/graphic-bar-plan-pruned.json"
    )
    if not snapshot.is_file() or not packet_path.is_file():
        pytest.skip("Local native K09 and old975 inventory unavailable")
    native = review.flat_outer_host(json.loads(snapshot.read_text())["host"])
    p = review.validated_graphics(json.loads(packet_path.read_text()), 0, 0)
    failed = [
        (b["direction"], b["bar_id"], side)
        for b in p["bars"]
        for side in ("top", "bottom")
        if not review._corridor_contained(
            b["start_xy_mm"], b["end_xy_mm"], b["diameter_mm"] / 2, native[side + "_outer_xy_mm"]
        )
    ]
    assert len(p["bars"]) == 975
    assert failed == [("bottom-Y", "source-recovery-1/0/8", "top")]
    types = {
        review.material_key(b): dict(
            element_id=100000 + b["diameter_mm"],
            nominal_diameter_mm=b["diameter_mm"],
            model_diameter_mm=b["diameter_mm"],
        )
        for b in p["bars"]
    }
    with pytest.raises(ValueError, match="source-recovery-1/0/8"):
        review.make_review_plan(p, native, types, dict.fromkeys(review.DIRECTIONS, 50))


def test_real_k09_dual_exterior_whole_918_inventory_and_readback_double():
    snapshot = ROOT / "qmonitoring-working-host-20260914-122250-368000.json"
    packet_path = (
        ROOT
        / "artifacts/k09_delivery_2026_09_16/dual-exterior-repair-v1/graphic-bar-plan-repaired.json"
    )
    if not snapshot.is_file() or not packet_path.is_file():
        pytest.skip("Local native K09 dual-exterior repaired inventory unavailable")
    native = review.flat_outer_host(json.loads(snapshot.read_text())["host"])
    p = review.validated_graphics(json.loads(packet_path.read_text()), 0, 0)
    types = {
        review.material_key(b): dict(
            element_id=100000 + b["diameter_mm"],
            nominal_diameter_mm=b["diameter_mm"],
            model_diameter_mm=b["diameter_mm"],
        )
        for b in p["bars"]
    }
    # Explicit diagnostic test depths, NOT approval of these four native Zs.
    plan = review.make_review_plan(p, native, types, dict(zip(review.DIRECTIONS, [50, 70, 50, 70])))
    assert len(plan["runs"]) == 918 and plan["expected"]["position_count"] == 106
    dmax = {
        direction: max(run["diameter_mm"] for run in plan["runs"] if run["direction"] == direction)
        for direction in review.DIRECTIONS
    }
    assert dmax == {"bottom-X": 12, "bottom-Y": 12, "top-X": 16, "top-Y": 16}
    assert 70 - 50 - (dmax["top-X"] + dmax["top-Y"]) / 2 == 4
    doubles = runpy.run_path(str(ROOT / "tests/integrations/test_rebar_review_mvp.py"))
    rows = doubles["native"](plan)
    for row, run in zip(rows, plan["runs"]):
        row["host_id"] = run["host_id"]
        row["bar_type"].update(
            element_id=run["bar_type_id"],
            nominal_diameter_mm=run["diameter_mm"],
            model_diameter_mm=run["diameter_mm"],
        )
        row["bars"][0]["curves"][0]["length_mm"] = run["length_mm"]
    result = review.compare_native(plan, rows)
    assert result["status"] == "matches" and result["physical_bar_count"] == 918
    rows[-1]["bars"][0]["curves"][0]["start_mm"][0] += 0.1
    assert review.compare_native(plan, rows)["status"] == "differs"
    assert p["trim_graphics"]["checks"]["coverage"] == "fail"
    assert p["trim_graphics"]["checks"]["collisions_3d"]["status"] == "not_checked"


def test_dual_contour_gate_rejects_body_in_only_one_outer_projection():
    doubles = runpy.run_path(str(ROOT / "tests/integrations/test_rebar_review_mvp.py"))
    native = review.flat_outer_host(host())
    native["top_outer_xy_mm"] = [
        [0, 0],
        [10000, 0],
        [10000, 10000],
        [3000, 10000],
        [3000, 1000],
        [2000, 1000],
        [2000, 10000],
        [0, 10000],
    ]
    party = doubles["primitives"]()
    types = {"A500|12": {"element_id": 7, "nominal_diameter_mm": 12, "model_diameter_mm": 12}}
    with pytest.raises(ValueError, match="top OUTER"):
        review.make_review_plan(party, native, types, dict.fromkeys(review.DIRECTIONS, 50))


def test_non_k09_real_boundary_difference_still_unsupported():
    data = host()
    data["top_faces"][0]["edge_loops"][0][0]["start_mm"][0] = 1
    data["top_faces"][0]["edge_loops"][0][-1]["end_mm"][0] = 1
    with pytest.raises(ValueError, match="Unequal"):
        review.flat_outer_host(data)


def test_pre_save_dialog_error_has_reason_traceback_and_real_report(monkeypatch, tmp_path, capsys):
    alerts = []
    forms = SimpleNamespace(alert=alerts.append)
    monkeypatch.setitem(
        sys.modules,
        "pyrevit",
        SimpleNamespace(
            DB=SimpleNamespace(), forms=forms, revit=SimpleNamespace(doc=None, uidoc=None)
        ),
    )
    monkeypatch.setitem(sys.modules, "System.Collections.Generic", SimpleNamespace(List=list))
    script = LIB.parent / "QMonitoring.tab/Review.panel/RebarReview.pushbutton/script.py"
    spec = importlib.util.spec_from_file_location("review_button_diagnostic_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.tempfile, "gettempdir", lambda: str(tmp_path))
    module.main()
    reports = list(tmp_path.glob("qmonitoring-rebar-review-setup-*.json"))
    assert len(reports) == 1
    result = json.loads(reports[0].read_text())
    assert result["status"] == "blocked_setup" and result["issues"][0]["traceback"]
    assert "Причина:" in alerts[-1] and str(reports[0]) in alerts[-1]
    assert "Отчёт: None" not in alerts[-1]
    assert "Traceback" in capsys.readouterr().out
