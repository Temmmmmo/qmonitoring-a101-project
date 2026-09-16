"""Repaired transport negatives; no claim of a live Revit or native FE solver."""

import hashlib
import importlib
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def packet():
    return json.loads((ROOT / "tests/fixtures/graphic-bar-plan-repaired-v1.json").read_text())


@pytest.fixture(scope="module")
def preview():
    sys.path.insert(0, str(ROOT / "integrations/pyrevit/QMonitoring.extension/lib"))
    return importlib.import_module("qm_revit_plan_preview")


def rehash(packet):
    repair = packet["repair"]
    repair["checker_json"] = json.dumps(repair["checks"], sort_keys=True)
    repair["checker_sha256"] = hashlib.sha256(repair["checker_json"].encode()).hexdigest()


def test_full_repaired_inventory_and_visible_blockers(packet, preview):
    p = preview.build_preview_primitives(packet, 12, 34)
    preview._validate_primitives(p)
    assert len(p["bars"]) == p["summary"]["physical_bar_count"] == 3
    added = [b for b in p["bars"] if b["repair_kind"] == "added"]
    assert len(added) == 1 and "original_bar_id" not in added[0]
    caption = preview._trim_caption(p)
    assert "контроль 40d: fail" in caption and "раскрой: fail" in caption
    assert "Actual RVT: not_checked" in caption and "НЕ пересчитывается" in caption
    review = importlib.import_module("qm_rebar_review")
    assert len(review.validated_graphics(packet, 12, 34)["bars"]) == 3


@pytest.mark.parametrize(
    "attack",
    [
        "source_hash",
        "checker_hash",
        "approval",
        "unknown_lane",
        "fake_lane",
        "unknown_fe",
        "missing_source",
        "missing_addition",
        "material",
        "window",
        "background",
        "retained_geometry",
        "metrics",
        "lost",
        "omit_fe",
        "coverage_pass",
        "actual_pass",
        "count_pairs",
        "pair_unknown",
        "nudge_policy",
    ],
)
def test_repaired_tamper_rejected(packet, preview, attack):
    repair, checks = packet["repair"], packet["repair"]["checks"]
    added = packet["directions"][2]["bars"][-1]
    if attack == "source_hash":
        packet["source_trim_packet_sha256"] = "0" * 64
    elif attack == "checker_hash":
        repair["checker_sha256"] = "0" * 64
    elif attack == "approval":
        packet["engineering_approval"] = True
    elif attack == "unknown_lane":
        added["source_bar_ids"] = added["provenance"]["lane_ids"] = ["invented"]
    elif attack == "fake_lane":
        repair["source_lanes"][0]["source"]["id"] = "invented"
    elif attack == "unknown_fe":
        added["provenance"]["source_fe_ids"] = [999]
    elif attack == "missing_source":
        packet["operations"].pop()
    elif attack == "missing_addition":
        packet["additions"] = []
    elif attack == "material":
        added["diameter_mm"] = 12
    elif attack == "window":
        added["coordinate_mm"] = 9999
    elif attack == "background":
        added["coordinate_mm"] = 300
    elif attack == "retained_geometry":
        packet["directions"][2]["bars"][0]["longitudinal_mm"][1] += 1
    elif attack == "metrics":
        packet["expected"]["physical_bar_count"] = 2
    elif attack == "lost":
        checks["previously_covered_area_lost_mm2"]["geometric_presence"] = 0.0001
    elif attack == "omit_fe":
        repair["source_fe_ids_by_direction"]["top-X"] = []
    elif attack == "coverage_pass":
        checks["coverage_with_control_40d"]["status"] = "pass"
    elif attack == "actual_pass":
        packet["checks"]["collisions_3d"]["status"] = "pass"
    elif attack == "nudge_policy":
        checks["maximum_axis_nudge_mm"] = 1
    elif attack in ("count_pairs", "pair_unknown"):
        end = dict(shape_kind="straight", role="additional", direction="top-X", bar_id="a")
        second = dict(end, bar_id="removed" if attack == "pair_unknown" else "b")
        checks["collisions"]["proven_collision_pairs"] = [
            dict(status="collision", first=end, second=second)
        ]
        if attack == "pair_unknown":
            checks["collisions"]["proven_collision_pair_count"] = 1
    if attack != "checker_hash":
        rehash(packet)
    with pytest.raises(ValueError):
        preview.build_preview_primitives(packet, 0, 0)


def test_repaired_primitives_cannot_be_mutated_after_load(packet, preview):
    p = preview.build_preview_primitives(packet, 0, 0)
    p["bars"].pop()
    with pytest.raises(ValueError):
        preview._validate_primitives(p)


def test_repaired_whole_batch_native_readback_contract(packet, preview):
    import runpy

    doubles = runpy.run_path(str(ROOT / "tests/integrations/test_rebar_review_mvp.py"))
    review = importlib.import_module("qm_rebar_review")
    p = review.validated_graphics(packet, 0, 0)
    host = review.flat_outer_host(doubles["host_data"]())
    types = {"A500|10": {"element_id": 7, "nominal_diameter_mm": 10, "model_diameter_mm": 10}}
    plan = review.make_review_plan(p, host, types, dict.fromkeys(review.DIRECTIONS, 50))
    rows = doubles["native"](plan)
    for row, run in zip(rows, plan["runs"]):
        row["bar_type"].update(nominal_diameter_mm=10, model_diameter_mm=10)
        axis = run["axes"][0]
        row["bars"][0]["curves"][0]["length_mm"] = review.distance(axis["start_mm"], axis["end_mm"])
    assert review.compare_native(plan, rows)["physical_bar_count"] == 3
    rows.pop()
    with pytest.raises(ValueError):
        review.compare_native(plan, rows)


def test_exact_action_length_limits_not_self_authorizing(packet, preview):
    packet["repair"]["checks"]["maximum_new_length_mm"] = 20000
    rehash(packet)
    with pytest.raises(ValueError):
        preview.build_preview_primitives(packet, 0, 0)


def test_real_s1_repaired_exact_loader_and_inventory(preview):
    path = (
        ROOT / "artifacts/s1_flat_mvp_2026_09_16/post-trim-repaired/graphic-bar-plan-repaired.json"
    )
    if not path.is_file():
        pytest.skip("Local repaired S1 unavailable")
    packet, digest = preview.load_preview_input(str(path))
    p = preview.build_preview_primitives(packet, 0, 0)
    assert len(digest) == 64 and len(p["bars"]) == 1907
    assert sum(b["repair_kind"] == "added" for b in p["bars"]) == 10
    assert sum(b["repair_kind"] == "modified" for b in p["bars"]) == 1
    checks = p["trim_graphics"]["checks"]
    assert (
        checks["coverage"] == "pass"
        and checks["anchorage_40d"] == checks["stock_cutting"] == "fail"
    )
    assert checks["collisions_3d"]["status"] == "not_checked"
    assert checks["conditional_collisions_3d"]["proven_pair_count"] == 1383


def test_repair_helper_python2_grammar():
    from lib2to3.pgen2 import driver
    from lib2to3 import pygram, pytree

    parser = driver.Driver(pygram.python_grammar, convert=pytree.convert)
    content = (
        ROOT / "integrations/pyrevit/QMonitoring.extension/lib/qm_revit_repaired_preview.py"
    ).read_text()
    parser.parse_string(content + "\n")
