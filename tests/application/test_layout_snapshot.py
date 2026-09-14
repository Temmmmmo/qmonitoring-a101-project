"""Snapshot export checks provenance and demand, not stored green status flags."""
from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar.application import layout_snapshot
from rebar.golden import get_engineer_reference_case
from rebar.optimization import (
    PLATE_DIRECTIONS, LayoutConstraints, PlateDirectionSolution, StrongestBBoxOptimizer,
    build_layout_problem, build_plate_problem, build_plate_solution,
)
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.preprocessing import apply_single_cell_rule
from rebar.reporting.serialization import to_jsonable


@pytest.fixture
def snapshot(tmp_path, monkeypatch, direction_mosaic):
    case_id = "k09-typical-3-14"
    case = get_engineer_reference_case(case_id)
    mapping = layout_snapshot.CASE_MAPPINGS[case_id]
    constraints = LayoutConstraints(allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM,
                                    cutting_profile="plate-11700", min_width_cells=2)
    problems, sources, mosaics = [], [], {}
    for direction in PLATE_DIRECTIONS:
        path = tmp_path / (str(direction) + ".dxf")
        path.write_bytes(b"Mock DXF parsing boundary " + str(direction).encode())
        mosaic = replace(direction_mosaic, source_path=str(path), direction=direction,
            meta={**direction_mosaic.meta, "rebar_mapping": {"id": mapping}})
        mosaics[str(path)] = mosaic
        problems.append(apply_single_cell_rule(build_layout_problem(mosaic, constraints), policy="preserve"))
        sources.append({"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    monkeypatch.setattr(layout_snapshot, "load_direction_mosaic",
                        lambda path, mapping_id: mosaics[str(path)])
    problem = build_plate_problem(problems, case_id=case_id)
    solution = build_plate_solution(PlateDirectionSolution(p.demand.direction,
        StrongestBBoxOptimizer().solve(p)) for p in problems)
    assert solution.valid
    pdf = tmp_path / "matched-engineer.pdf"
    pdf.write_bytes(b"Mock hash-verified PDF source; extraction is outside this unit test")
    candidate = {"id": "plate:1", "valid": True, "metrics": to_jsonable(solution.metrics),
                 "solution": to_jsonable(solution)}
    data = {"case_id": case_id, "input_set_id": case.input_sets[0].id,
        "engineering_profile": {"single_cell_policy": "preserve", "host_supplied": False,
            "cutting_profile": "plate-11700", "min_width_cells": 2},
        "source_dxf": sources, "source_pdf": {"path": str(pdf), "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()},
        "problems": to_jsonable(problem), "candidates": [candidate, {**copy.deepcopy(candidate), "id": "plate:2"}],
        "engineer": {"mass_kg": case.expected_mass_kg, "physical_bar_count": case.expected_bar_count,
            "specification_rows": case.expected_position_count},
        "selection": {"minimum_mass": "plate:2", "minimum_bars_under_15pct_mass": "plate:2"}}
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path, data


def load(path, **kwargs):
    return layout_snapshot.load_layout_trial_snapshot(path, steel_class="A500", **kwargs)


@pytest.mark.parametrize("selection", layout_snapshot.SELECTIONS)
def test_selection_recomputed_and_four_directions_kept(snapshot, selection):
    path, data = snapshot
    # Do not trust either precomputed selection or precomputed status flags.
    data["candidates"][0]["valid"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    bundle = load(path, selection=selection)
    assert bundle["review"]["snapshot"]["candidate_id"] == "plate:1"
    assert len(bundle["packet"]["directions"]) == 4
    assert bundle["review"]["original_under_reinforced_cell_count"] == 0
    assert bundle["packet"]["placement_eligible"] is False


def test_explicit_selection_roundtrips_decoder(snapshot):
    path, data = snapshot
    raw = data["candidates"][0]["solution"]["direction_solutions"][0]["solution"]
    assert to_jsonable(layout_snapshot.decode_solution(raw)) == raw
    bundle = load(path, candidate_id="plate:2")
    assert bundle["review"]["snapshot"]["candidate_id"] == "plate:2"


def test_no_extra_bars_selection_enforces_engineer_count_not_snapshot_flags(snapshot, monkeypatch):
    path, data = snapshot
    actual_count = data["candidates"][0]["metrics"]["physical_bar_count"]
    case = get_engineer_reference_case(data["case_id"])
    reduced_limit = SimpleNamespace(input_sets=case.input_sets, expected_bar_count=actual_count - 1,
                                   expected_mass_kg=case.expected_mass_kg,
                                   expected_position_count=case.expected_position_count)
    monkeypatch.setattr(layout_snapshot, "get_engineer_reference_case", lambda _id: reduced_limit)
    data["engineer"]["physical_bar_count"] = reduced_limit.expected_bar_count
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="No independently validated"):
        layout_snapshot.load_layout_snapshot(path, selection="minimum_mass_without_extra_bars")
    # Other selectors are unchanged: this is a count filter, not global invalidity.
    assert layout_snapshot.load_layout_snapshot(path, selection="minimum_mass").solution.valid


def test_no_extra_bars_selection_has_no_hidden_mass_15pct_filter(snapshot, monkeypatch):
    path, data = snapshot
    actual_mass = data["candidates"][0]["metrics"]["total_mass_kg"]
    case = get_engineer_reference_case(data["case_id"])
    lower_mass = SimpleNamespace(input_sets=case.input_sets, expected_mass_kg=actual_mass / 2,
                                expected_bar_count=case.expected_bar_count,
                                expected_position_count=case.expected_position_count)
    monkeypatch.setattr(layout_snapshot, "get_engineer_reference_case", lambda _id: lower_mass)
    data["engineer"]["mass_kg"] = lower_mass.expected_mass_kg
    path.write_text(json.dumps(data), encoding="utf-8")
    loaded = layout_snapshot.load_layout_snapshot(path, selection="minimum_mass_without_extra_bars")
    assert loaded.solution.valid
    assert loaded.engineer_comparison["mass_threshold_15pct_met"] is False


@pytest.mark.parametrize("source", ["dxf", "pdf"])
def test_changed_source_hash_rejected(snapshot, source):
    path, data = snapshot
    record = data["source_dxf"][0] if source == "dxf" else data["source_pdf"]
    Path(record["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="Changed source"):
        load(path, candidate_id="plate:1")


@pytest.mark.parametrize("change", ["mapping", "constraints", "cell", "legend", "lowered", "engineer", "candidate_mass", "fake_mass", "duplicate_source", "wrong_path", "partial"])
def test_forged_snapshot_rejected(snapshot, change):
    path, data = snapshot
    problem = data["problems"]["direction_problems"][0]
    if change == "mapping":
        problem["demand"]["meta"]["rebar_mapping"]["id"] = "plate-zero-d12-v1"
    elif change == "constraints":
        problem["constraints"]["anchorage_diameters"] = 15
    elif change == "cell":
        problem["demand"]["cells"][0]["level_index"] = 1
    elif change == "legend":
        problem["demand"]["levels"][1]["additional"]["diameter"] = 12
    elif change == "lowered":
        data["engineering_profile"]["single_cell_policy"] = "legacy-research"
    elif change == "engineer":
        data["engineer"]["mass_kg"] *= 10
    elif change == "candidate_mass":
        data["candidates"][0]["metrics"]["total_mass_kg"] -= 10
    elif change == "fake_mass":
        raw = data["candidates"][0]["solution"]["direction_solutions"][0]["solution"]
        raw["zones"][0]["mass_kg"] = 1
    elif change == "duplicate_source":
        data["source_dxf"][1] = data["source_dxf"][0]
    elif change == "wrong_path":
        problem["demand"]["source_path"] = data["source_pdf"]["path"]
    else:
        data["candidates"][0]["solution"]["direction_solutions"].pop()
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        load(path, candidate_id="plate:1")


def test_unknown_candidate_does_not_fallback(snapshot):
    with pytest.raises(ValueError, match="Unknown candidate ID"):
        load(snapshot[0], candidate_id="best-looking")


def test_strict_json_and_size_limit(snapshot, monkeypatch):
    path, _ = snapshot
    monkeypatch.setattr(layout_snapshot, "MAX_SNAPSHOT_BYTES", 8)
    with pytest.raises(ValueError, match="exceeds"):
        load(path, candidate_id="plate:1")
    monkeypatch.setattr(layout_snapshot, "MAX_SNAPSHOT_BYTES", 100)
    for text in ('{"case_id":"a","case_id":"b"}', '{"case_id":NaN}'):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            load(path, candidate_id="plate:1")


def cli_module():
    path = Path(__file__).resolve().parents[2] / "scripts/export_layout_plate_trial.py"
    spec = importlib.util.spec_from_file_location("layout_export_cli_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_writes_exact_packet_review_readme_and_refuses_overwrite(snapshot, tmp_path):
    path, _ = snapshot
    output = tmp_path / "new-result"
    cli = cli_module()
    summary = cli.export_snapshot(path, output, selection="minimum_mass", steel_class="A500")
    assert summary["candidate_id"] == "plate:1"
    assert {p.name for p in output.iterdir()} == {"full-plate-trial.json", "engineer-review.json", "README.md"}
    packet = json.loads((output / "full-plate-trial.json").read_text())
    review = json.loads((output / "engineer-review.json").read_text())
    assert packet["expected"]["physical_bar_count"] == summary["physical_bar_count"]
    assert review["snapshot"]["candidate_id"] == "plate:1"
    readme = (output / "README.md").read_text()
    assert "все четыре направления" in readme
    assert "placement_eligible=false" in readme
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    with pytest.raises(ValueError, match="already exists"):
        cli.export_snapshot(path, output, candidate_id="plate:2", steel_class="A500")
    assert {p.name: p.read_bytes() for p in output.iterdir()} == before


def test_invalid_input_does_not_create_output_directory(snapshot, tmp_path):
    path, data = snapshot
    data["engineering_profile"]["single_cell_policy"] = "legacy-research"
    path.write_text(json.dumps(data), encoding="utf-8")
    output = tmp_path / "must-not-exist"
    with pytest.raises(ValueError):
        cli_module().export_snapshot(path, output, candidate_id="plate:1", steel_class="A500")
    assert not output.exists()
