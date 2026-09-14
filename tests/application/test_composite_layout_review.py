from __future__ import annotations

import copy
import json
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic
from rebar.application.composite_layout_review import (
    LAYOUT_REVIEW_INPUT_SCHEMA, load_review_input, review_composite_layout,
)
from rebar.application.demo import IRREGULAR_PLATE_DEMO, write_demo_dxf
from rebar.dxf_ingest import read_mosaic
from rebar.legend import parse_recipe
from rebar.optimization.contracts.composite_coverage import COMPOSITE_COVERAGE_POLICY

ROOT = Path(__file__).resolve().parents[2]


def recipe_mosaic():
    label = "s300d18+s150d18+s300d25"
    recipe = parse_recipe(label)
    band = Band(0, 2, label, 41.8, recipe.background, None, recipe)
    cells = [Cell([(0, y), (3900, y), (3900, y + 400), (0, y + 400)], (1950, y + 200), 2, band)
             for y in (0, 400)]
    return Mosaic(Direction(Layer.TOP, Axis.X), cells, [band], (0, 0, 3900, 800))


def request_sample(level=0, bbox=(0, 0, 3900, 800), *, phase25=50):
    return {"schema_version": LAYOUT_REVIEW_INPUT_SCHEMA, "mode": "research-only", "units": "mm",
            "policy_id": COMPOSITE_COVERAGE_POLICY, "direction": {"layer": "top", "axis": "X"},
            "min_width_cells": 2, "phase_source": "synthetic experiment only, not project-approved",
            "background_origin_mm": 0, "additional_origins_by_level": {str(level): [0, phase25]},
            "zones": [{"id": "test", "level_index": level, "demand_bbox_mm": list(bbox)}]}


@pytest.mark.parametrize("phase,expected", [(50, "research_checks_passed"), (200, "needs_revision")])
def test_application_wires_the_shared_validator_and_never_opens_export(phase, expected):
    mosaic, request = recipe_mosaic(), request_sample(phase25=phase)
    before = copy.deepcopy((mosaic, request))
    result = review_composite_layout(mosaic, request)
    assert result["status"] == expected and not result["placement_eligible"] and not result["optimizer_executed"]
    assert result["coverage"]["physical_bar_count"] == 9
    assert not result["zone_drafts"][0]["checks"]["export_eligible"]
    assert "composite-demand-coverage" in result["zone_drafts"][0]["checks"]["blocking_check_ids"]
    assert result["phase_approval"] == "not_checked"
    assert result["preprocessing"] == "not_applied_original_demand_retained"
    assert (mosaic, request) == before


@pytest.mark.parametrize("raw", [b"", b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                                b"\xff", b"[" * 3000 + b"]" * 3000, b" " * (256 * 1024 + 1)])
def test_json_loader_rejects_ambiguous_or_unbounded_input(raw):
    with pytest.raises(ValueError):
        load_review_input(raw)


def test_json_loader_supports_unicode_and_utf8_bom():
    request = request_sample()
    request["phase_source"] = "Только тест; фаза не утверждена"
    assert load_review_input(b"\xef\xbb\xbf" + json.dumps(request, ensure_ascii=False).encode()) == request


@pytest.mark.parametrize("field,value", [
    ("mode", "apply"), ("units", "m"), ("policy_id", "automatic"), ("phase_source", ""),
    ("background_origin_mm", None), ("background_origin_mm", True), ("background_origin_mm", 1e100),
    ("min_width_cells", True), ("min_width_cells", 1), ("min_width_cells", 2.0),
    ("direction", {"layer": "top", "axis": "Y"}), ("additional_origins_by_level", {"0": [0]}),
    ("additional_origins_by_level", {"0": [0, None]}), ("additional_origins_by_level", {"-1": [0, 50]}),
    ("additional_origins_by_level", {"00": [0, 50]}), ("additional_origins_by_level", {"9": [0, 50]}),
    ("zones", [{"id": "test", "level_index": True, "demand_bbox_mm": [0, 0, 3900, 800]}]),
    ("zones", [{"id": "test", "level_index": 0, "demand_bbox_mm": [0, 0, 3900, float("inf")]}]),
    ("zones", [{"id": "test", "level_index": 0, "demand_bbox_mm": [0, 0, 3900, 800], "extra": True}]),
])
def test_application_never_guesses_missing_phases_or_silently_accepts_settings(field, value):
    request = request_sample()
    request[field] = value
    with pytest.raises((ValueError, KeyError)):
        review_composite_layout(recipe_mosaic(), request)


def test_total_bar_limit_is_enforced_before_building_large_json(monkeypatch):
    monkeypatch.setattr("rebar.application.composite_layout_review.MAX_TOTAL_BARS", 1)
    with pytest.raises(ValueError, match="лимит"):
        review_composite_layout(recipe_mosaic(), request_sample())


def public_sources(tmp_path):
    dxf = tmp_path / IRREGULAR_PLATE_DEMO.filename
    write_demo_dxf(IRREGULAR_PLATE_DEMO.id, dxf)
    shk = tmp_path / "explicit.shk"
    # Public geometry and a synthetic six-band composite prescription, not private data.
    labels = [b"s300d18"] + [f"s300d18+s150d18+s300d{d}".encode() for d in (20, 22, 25, 28, 32)]
    shk.write_bytes(b"".join(struct.pack("<ffHB", i + 1, i + 2, i, len(label)) + label for i, label in enumerate(labels)))
    return dxf, shk


@pytest.mark.parametrize("bad", [None, "missing_phase", "duplicate_key", "existing_output", "rvt_output"])
def test_cli_uses_real_public_dxf_ingest_and_never_changes_sources(tmp_path, bad):
    dxf, shk = public_sources(tmp_path)
    mosaic = read_mosaic(str(dxf), str(shk))
    data = request_sample(level=3, bbox=mosaic.bbox)
    data["direction"] = {"layer": mosaic.direction.layer.value, "axis": mosaic.direction.axis.value}
    if bad == "missing_phase":
        data["additional_origins_by_level"]["3"].pop()
    layout = tmp_path / "layout.json"
    layout.write_text('{"x":1,"x":2}' if bad == "duplicate_key" else json.dumps(data), encoding="utf-8")
    output = tmp_path / ("keep.rvt" if bad == "rvt_output" else "result.json")
    if bad in ("existing_output", "rvt_output"):
        output.write_bytes(b"preserve")
    before = {p: p.read_bytes() for p in (dxf, shk, layout)}
    run = subprocess.run([sys.executable, str(ROOT / "scripts/verify_composite_layout.py"),
        "--dxf", str(dxf), "--shk", str(shk), "--layout", str(layout), "--output", str(output)],
        capture_output=True, text=True)
    assert all(p.read_bytes() == content for p, content in before.items())
    if bad:
        assert run.returncode == 2, run.stderr
        assert output.read_bytes() == b"preserve" if output.exists() else True
    else:
        assert run.returncode == 1, run.stderr  # mixed diameters: this one zone must NOT cover the whole map
        result = json.loads(output.read_text())
        assert result["source_cell_count"] == 96 and result["status"] == "needs_revision"
        assert not result["placement_eligible"] and not result["optimizer_executed"]
        assert len(result["source_sha256"]["dxf"]) == 64


def test_actual_source_is_reviewed_without_calling_legacy_optimizer(two_background_top_x_sources):
    dxf, shk = two_background_top_x_sources
    mosaic = read_mosaic(str(dxf), str(shk))
    request = request_sample(level=3, bbox=mosaic.bbox, phase25=50)
    result = review_composite_layout(mosaic, request)
    assert result["source_cell_count"] == 2132 and result["coverage"]["demanded_cell_count"] == 1341
    assert result["coverage"]["uncovered_cell_count"] == 17
    assert not result["optimizer_executed"] and not result["placement_eligible"]
    # Deliberately huge hand-specified rectangle; this is no proposed engineering layout.
    assert result["zone_drafts"][0]["components"][0]["installed_length_mm"] > 23600
    request["additional_origins_by_level"]["3"][1] = -50
    shifted = review_composite_layout(mosaic, request)
    assert shifted["status"] == "research_checks_passed" and shifted["coverage"]["uncovered_cell_count"] == 0
    assert shifted["coverage"]["physical_bar_count"] == 188 and not shifted["placement_eligible"]
    request["zones"][0]["level_index"] = 2
    request["additional_origins_by_level"] = {"2": [0, 50]}
    weak = review_composite_layout(mosaic, request)
    assert weak["status"] == "needs_revision" and weak["coverage"]["uncovered_cell_count"] == 165
