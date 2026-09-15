"""Fresh real miniature pipeline: pruning is not an incomplete exact-trim packet."""
from copy import deepcopy
import hashlib
import json
import xml.etree.ElementTree as ET

import pytest

from rebar.application.boundary_trim_web import boundary_trim_web_report
from rebar.application.physical_layout_recovery import _bytes
from rebar.application.trimmed_cleanup_web import pruned_trimmed_web_report
from rebar.models import Axis, Direction, Layer
from rebar.optimization.contracts.shaped_physical import Line3D, ShapedPhysicalBar
import test_boundary_trim_web as trim_fixtures

trim_case = trim_fixtures.trim_case
trim_result = trim_fixtures.trim_result


@pytest.fixture(scope="module")
def pruned_result(trim_case):
    problem, recovery, host = trim_case
    return boundary_trim_web_report(problem, recovery, host, confirm_identity_xy=True,
                                     stock_time_limit_s=3, cleanup_redundant=True)


def test_pruned_export_preserves_entire_trim_history_and_independent_new_checks(trim_result, pruned_result):
    result = pruned_result
    assert result["output_kind"] == "pruned-trimmed-physical-bars"
    assert "graphic_bar_plan_draft" not in result
    assert result["boundary_trim"] == trim_result["boundary_trim"]
    assert result["original_source_zones"] == trim_result["original_source_zones"]
    assert result["source_graphics"] == trim_result["source_graphics"]
    packet = result["graphic_bar_plan_pruned"]
    assert packet["schema_version"] == "graphic-bar-plan-pruned/v1"
    assert set(packet) == {"schema_version", "units", "case_id", "geometry_kind", "source_stage",
        "source_trim_packet", "source_trim_packet_json", "source_trim_packet_sha256", "retained_bar_ids",
        "expected", "cleanup", "placement_eligible", "engineering_approval"}
    source = trim_result["graphic_bar_plan_draft"]
    assert packet["source_trim_packet"] == source
    assert packet["source_trim_packet_json"] == _bytes(source).decode("utf-8")
    assert json.loads(packet["source_trim_packet_json"]) == source
    assert hashlib.sha256(packet["source_trim_packet_json"].encode()).hexdigest() == packet["source_trim_packet_sha256"]
    checks = packet["cleanup"]
    assert checks == result["trimmed_cleanup"] and checks["accepted_nonregression"]
    assert checks["retained_geometry_exactly_unchanged"] and not checks["original_FE_geometry_changed"]
    assert checks["previously_covered_geometry_lost"] == []
    assert not packet["placement_eligible"] and not packet["engineering_approval"]
    original = {(f'{d["direction"]["layer"]}-{d["direction"]["axis"]}', b["id"]): b
                for d in source["directions"] for b in d["bars"]}
    retained = {(row["direction"], row["bar_id"]) for row in packet["retained_bar_ids"]}
    assert len(retained) == len(packet["retained_bar_ids"]) == packet["expected"]["physical_bar_count"]
    mapped = {(r["direction"], r["input_bar_id"]): r for r in checks["piece_mapping"]}
    assert set(mapped) == set(original)
    assert set(original)-retained == {key for key, row in mapped.items() if row["output_bar_ids"] == []}
    assert len(original)-len(retained) == checks["removed_bar_count"]
    for key in retained:
        assert mapped[key]["output_bar_ids"] == [key[1]]
    assert packet["expected"]["source_zone_count"] == source["after"]["source_zone_count"]
    assert result["front"][0]["physical_bar_count"] == checks["physical_metrics"]["physical_bar_count"]
    assert result["front"][0]["additional_mass_kg"] == checks["physical_metrics"]["mass_kg"]
    assert result["front"][0]["stock_cutting"] == checks["stock_cutting"]
    assert result["front"][0]["additional_mass_kg"] == pytest.approx(sum(
        p["total_mass_kg"] for p in result["front"][0]["bar_schedule"]))
    assert result["front"][0]["position_count"] == len(result["front"][0]["bar_schedule"])
    seen = set()
    for d, old in zip(result["directions"], trim_result["directions"]):
        assert d["source_svg"] == old["source_svg"] and d["source_zone_drafts"] == old["source_zone_drafts"]
        candidate = d["candidates"][0]
        key = f'{d["direction"]["layer"]}-{d["direction"]["axis"]}'
        bars = candidate["physical_bars"]
        for bar in bars:
            assert bar == original[key, bar["id"]]
            seen.add((key, bar["id"]))
        ns = "{http://www.w3.org/2000/svg}"
        plain, overlay = ET.fromstring(candidate["svg"]), ET.fromstring(candidate["overlay_svg"])
        assert len(plain.findall(f'.//{ns}line')) == len(bars)
        assert len(overlay.findall(f'.//{ns}line')) == len(bars)
        assert len(overlay.findall(f'.//{ns}g[@class="source-zones"]/{ns}g')) == len(d["source_zone_drafts"])
        assert not plain.findall(f'.//{ns}clipPath')
    assert seen == retained


@pytest.mark.parametrize("kwargs", [{"cleanup_redundant": 1},
    {"cleanup_redundant": True, "respect_openings": False}])
def test_cleanup_not_silently_applied_to_other_processing_profiles(trim_case, kwargs):
    problem, recovery, host = trim_case
    with pytest.raises(ValueError, match="Cleanup"):
        boundary_trim_web_report(problem, recovery, host, confirm_identity_xy=True, **kwargs)


def test_packet_history_is_not_a_shared_mutable_alias(pruned_result):
    result = deepcopy(pruned_result)
    packet = result["graphic_bar_plan_pruned"]
    packet["source_trim_packet"]["after"]["physical_bar_count"] = -1
    assert result["boundary_trim"]["physical_metrics"]["physical_bar_count"] >= 0
    assert result["front"][0]["physical_bar_count"] >= 0
    assert json.loads(packet["source_trim_packet_json"])["after"]["physical_bar_count"] >= 0


@pytest.mark.parametrize("mutation", ["coordinate", "missing", "duplicate", "mass", "nan"])
def test_fresh_typed_trim_must_match_every_graphic_input_before_cleanup(trim_case, monkeypatch, mutation):
    import rebar.application.boundary_trim_web as module
    original = module._graphic_packet
    def corrupted(*args, **kwargs):
        packet = original(*args, **kwargs)
        bars = packet["directions"][0]["bars"]
        if mutation == "coordinate":
            bars[0]["coordinate_mm"] += 1
        elif mutation == "missing":
            bars.pop()
        elif mutation == "duplicate":
            bars.append(deepcopy(bars[0]))
        else:
            packet["after"]["additional_mass_kg"] = float("nan") if mutation == "nan" else -1
        return packet
    monkeypatch.setattr(module, "_graphic_packet", corrupted)
    problem, recovery, host = trim_case
    with pytest.raises(ValueError, match="trim"):
        boundary_trim_web_report(problem, recovery, host, confirm_identity_xy=True,
                                 stock_time_limit_s=3, cleanup_redundant=True)


def test_shaped_bar_cannot_be_exported_as_only_its_main_line():
    bar = ShapedPhysicalBar("not-a-straight", Direction(Layer.TOP, Axis.X), "A500", 10, ("source",),
        (Line3D((0, 0, 0), (1000, 0, 0)),), "U", (0,), "synthetic", "synthetic", 1000)
    stage = {"output_kind": "boundary-trimmed-physical-bars", "placement_eligible": False,
             "engineering_approval": False, "boundary_trim": {"respect_openings": True}}
    with pytest.raises(ValueError, match="straight single-main-leg"):
        pruned_trimmed_web_report(stage, (bar,), (), None, None)
