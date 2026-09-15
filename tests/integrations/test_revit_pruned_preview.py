"""Graphic transport checks; no native Revit or engineering certificate."""
from copy import deepcopy
from pathlib import Path
import importlib
import json
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "integrations/pyrevit/QMonitoring.extension/lib"
PRUNED = ROOT / "artifacts/source_views_2026_09_15/boundary-trim-pruned-web-v1/graphic-bar-plan-pruned.json"
S1 = ROOT / "artifacts/s1_flat_mvp_2026_09_15/revalidated-v3/graphic-bar-plan.json"


@pytest.fixture(scope="module")
def preview():
    sys.path.insert(0, str(LIB))
    return importlib.import_module("qm_revit_plan_preview")


def _packet():
    if not PRUNED.is_file():
        pytest.skip("Local realistic pruned export is unavailable")
    return json.loads(PRUNED.read_text(encoding="utf-8"))


def test_real_pruned_export_preserves_nonregression_without_fake_pass(preview):
    packet = _packet()
    primitives = preview.build_preview_primitives(packet, 0, 0)
    preview._validate_primitives(primitives)
    assert len(primitives["bars"]) == packet["expected"]["physical_bar_count"] == 975
    checks = primitives["trim_graphics"]["checks"]
    assert checks["coverage"] == checks["anchorage_40d"] == checks["stock_cutting"] == "fail"
    assert "НЕ ВЫДАЧА" in preview._trim_caption(primitives)
    assert packet["cleanup"]["previously_covered_geometry_lost"] == []


@pytest.mark.parametrize("change", ("source-hash", "retained-duplicate", "mapping-omit",
    "retained-geometry", "lost-covered", "increase-area", "change-status", "false-approval",
    "collision-false-zero", "collision-removed-id"))
def test_pruned_rejects_source_or_coverage_substitution(preview, change):
    packet = deepcopy(_packet())
    cleanup = packet["cleanup"]
    if change == "source-hash":
        packet["source_trim_packet_sha256"] = "0" * 64
    elif change == "retained-duplicate":
        packet["retained_bar_ids"][0] = packet["retained_bar_ids"][1]
    elif change == "mapping-omit":
        cleanup["piece_mapping"].pop()
    elif change == "retained-geometry":
        source = packet["source_trim_packet"]
        source["directions"][0]["bars"][0]["coordinate_mm"] += 1
    elif change == "lost-covered":
        cleanup["previously_covered_geometry_lost"] = [{"cell_id": 1}]
    elif change == "increase-area":
        cell = cleanup["coverage_after"]["control_40d"]["directions"][0]["cells"][0]
        cell["uncovered_area_mm2"] += 0.01
    elif change == "change-status":
        cleanup["coverage_after"]["control_40d"]["status"] = "pass"
    elif change == "collision-false-zero":
        cleanup["collisions"]["proven_collision_pair_count"] = 0
    elif change == "collision-removed-id":
        removed = next(row for row in cleanup["piece_mapping"] if not row["output_bar_ids"])
        cleanup["collisions"]["proven_collision_pairs"][0]["first"]["direction"] = removed["direction"]
        cleanup["collisions"]["proven_collision_pairs"][0]["first"]["bar_id"] = removed["input_bar_id"]
    else:
        packet["engineering_approval"] = True
    with pytest.raises(ValueError):
        preview.build_preview_primitives(packet, 0, 0)


def test_s1_flat_caption_and_failed_checks_stay_visible(preview):
    if not S1.is_file():
        pytest.skip("Local S1 export is unavailable")
    packet = json.loads(S1.read_text(encoding="utf-8"))
    primitives = preview.build_preview_primitives(packet, 0, 0)
    preview._validate_primitives(primitives)
    caption = preview._trim_caption(primitives)
    assert "внешний контур DXF" in caption
    assert "отверстия, перепады высоты и защитный слой не учтены" in caption
    assert "НЕ измеренная геометрия Revit" in caption
    assert all(primitives["trim_graphics"]["checks"][key] == "fail"
        for key in ("coverage", "anchorage_40d", "stock_cutting"))
