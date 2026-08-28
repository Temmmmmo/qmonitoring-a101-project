"""Тесты малопараметрического селектора точки Парето."""

from __future__ import annotations

import json

import pytest

from rebar.learning import (
    FrontCandidateObservation,
    FrontObservation,
    calibrate_preference,
    fit_preference_model,
    leave_one_project_out,
    load_benchmark_observation,
    rank_candidates,
    weak_target_candidate,
)
from rebar.reporting import generate_preference_calibration_report


def _observation(case_id: str, *, prefer_mass: bool) -> FrontObservation:
    candidates = (
        FrontCandidateObservation(f"{case_id}:light", 100.0, 200, 20),
        FrontCandidateObservation(f"{case_id}:simple", 140.0, 100, 8),
        FrontCandidateObservation(f"{case_id}:middle", 118.0, 140, 12),
    )
    return FrontObservation(
        case_id=case_id,
        title=case_id,
        reference_mass_kg=102.0 if prefer_mass else 138.0,
        reference_bar_count=195 if prefer_mass else 102,
        reference_position_count=10,
        candidates=candidates,
    )


def test_fit_ranks_without_using_held_out_reference():
    observations = (
        _observation("a", prefer_mass=True),
        _observation("b", prefer_mass=True),
    )

    model = fit_preference_model(observations)
    ranking = rank_candidates(_observation("held-out", prefer_mass=False), model.mass_weight)

    assert model.training_case_ids == ("a", "b")
    assert model.training_mean_regret == pytest.approx(0.0)
    assert ranking[0].id.endswith(":light")


def test_leave_one_project_out_never_trains_on_held_out_case():
    observations = (
        _observation("a", prefer_mass=True),
        _observation("b", prefer_mass=True),
        _observation("c", prefer_mass=False),
    )

    folds = leave_one_project_out(observations)

    assert {fold.held_out_case_id for fold in folds} == {"a", "b", "c"}
    assert all(fold.held_out_case_id not in fold.training_case_ids for fold in folds)
    assert all(len(fold.training_case_ids) == 2 for fold in folds)
    assert all(fold.regret >= 0.0 for fold in folds)
    assert all(isinstance(fold.mass_gate_met, bool) for fold in folds)


def test_loader_keeps_only_unique_hard_valid_candidates(tmp_path):
    payload = {
        "reference": {
            "id": "case",
            "title": "Case",
            "source_kind": "verified_pdf_spec",
            "mass_kg": 100.0,
            "physical_bar_count": 100,
            "position_count": 10,
        },
        "candidates": [
            {
                "run_id": "run",
                "candidate_id": "a",
                "valid": True,
                "under_reinforced_cell_count": 0,
                "total_mass_kg": 110.0,
                "physical_bar_count": 90,
                "zone_count": 8,
            },
            {
                "run_id": "run-duplicate",
                "candidate_id": "b",
                "valid": True,
                "under_reinforced_cell_count": 0,
                "total_mass_kg": 110.0,
                "physical_bar_count": 90,
                "zone_count": 8,
            },
            {
                "run_id": "run",
                "candidate_id": "c",
                "valid": True,
                "under_reinforced_cell_count": 0,
                "total_mass_kg": 120.0,
                "physical_bar_count": 80,
                "zone_count": 6,
            },
            {
                "run_id": "run",
                "candidate_id": "unsafe",
                "valid": False,
                "under_reinforced_cell_count": 1,
                "total_mass_kg": 1.0,
                "physical_bar_count": 1,
                "zone_count": 1,
            },
        ],
    }
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    observation = load_benchmark_observation(path)

    assert observation.case_id == "case"
    assert len(observation.candidates) == 2
    assert {candidate.mass_kg for candidate in observation.candidates} == {110.0, 120.0}


def test_report_records_model_folds_and_visuals(tmp_path):
    observations = (
        _observation("a", prefer_mass=True),
        _observation("b", prefer_mass=True),
        _observation("c", prefer_mass=False),
    )
    calibration = calibrate_preference(observations)

    report = generate_preference_calibration_report(
        calibration,
        observations,
        tmp_path,
    )

    payload = json.loads((tmp_path / "calibration.json").read_text(encoding="utf-8"))
    assert report == tmp_path / "index.html"
    assert payload["schema_version"] == 1
    assert payload["method"]["validation"] == "leave-one-project-out"
    assert payload["summary"]["project_count"] == 3
    assert 0.0 <= payload["summary"]["mass_gate_success_rate"] <= 1.0
    assert len(payload["folds"]) == 3
    assert "Первый селектор точки Парето" in report.read_text(encoding="utf-8")
    assert "<svg" in report.read_text(encoding="utf-8")


def test_weak_target_is_defined_by_engineer_scale():
    observation = _observation("case", prefer_mass=False)

    target = weak_target_candidate(observation)

    assert target.id == "case:simple"
