import importlib
import json

import pytest

from rebar.learning.zone_preference import (
    ZoneObservation, build_bundle, build_engineering_preference, evaluate, fit, input_fingerprint,
    predict_report, rank, validated_front,
)


def sample_report():
    directions = []
    for role, (layer, axis) in enumerate((("bottom", "X"), ("bottom", "Y"), ("top", "X"), ("top", "Y"))):
        candidates = []
        for mass, bars, zones in ((10.0, 10, 1), (8.0, 12, 2)):
            candidates.append({"coverage": {"status": "pass", "uncovered_cell_count": 0,
                "geometry_and_patterns_valid": True, "coverage_passed": True,
                "demanded_cell_count": 4, "covered_cell_count": 4,
                "additional_mass_kg": mass, "physical_bar_count": bars, "zone_count": zones,
                "zones": [{"geometry_and_pattern_valid": True} for _ in range(zones)]},
                "metrics": {"additional_mass_kg": mass, "physical_bar_count": bars, "zone_count": zones}})
        directions.append({"direction": {"layer": layer, "axis": axis}, "candidates": candidates,
                           "source": {"sha256": {"dxf": f"{role + 1:064x}", "shk": f"{role + 5:064x}"}}})
    return {"schema_version": "composite-plate-analysis/v1", "search_mode": "zone-merge",
            "direction_count": 4, "directions": directions, "status": "full_coverage_candidates_found",
            "placement_eligible": False, "source_demand_preserved": True,
            "front": [
                {"direction_candidate_indexes": [0, 0, 0, 0], "additional_mass_kg": 40.0,
                 "physical_bar_count": 40, "zone_count": 4},
                {"direction_candidate_indexes": [1, 1, 1, 1], "additional_mass_kg": 32.0,
                 "physical_bar_count": 48, "zone_count": 8},
            ]}


def observations():
    front = validated_front(sample_report())
    return (ZoneObservation("A", "A.json", "a" * 64, front, 39, 41),
            ZoneObservation("B", "B.json", "b" * 64, front, 35, 46),
            ZoneObservation("C", "C.json", "c" * 64, front, 33, 47))


def test_equal_case_weight_and_lopo_never_train_on_held_out_project():
    obs = observations()
    original = fit(obs)
    repeated = fit((*obs, obs[0]))
    assert repeated["coefficients"] == pytest.approx(original["coefficients"])
    result = evaluate((*obs, obs[0]))
    assert result["independent_case_count"] == 3 and result["report_count"] == 4
    assert all(fold["held_out_case_id"] not in fold["training_case_ids"] for fold in result["folds"])
    assert [fold["held_out_case_id"] for fold in result["folds"]].count("A") == 2
    assert result["top3_rate"] == 1.0


def test_inference_uses_only_front_and_existing_indexes():
    obs = observations()
    model = fit(obs)
    front = obs[0].front
    ordered = rank(front, model)
    assert sorted(ordered) == list(range(len(front)))
    changed_reference = ZoneObservation(obs[0].case_id, obs[0].report_id, obs[0].report_sha256,
                                        front, 1000, 1000)
    assert rank(changed_reference.front, model) == ordered
    prediction = predict_report(sample_report(), model)
    assert prediction["selected_index"] == ordered[0]
    assert prediction["top3_indexes"] == list(ordered)


def test_invalid_or_partial_report_cannot_enter_training():
    report = sample_report()
    assert len(validated_front(report)) == 2
    report["directions"][2]["candidates"][1]["coverage"]["uncovered_cell_count"] = 1
    with pytest.raises(ValueError, match="coverage"):
        validated_front(report)
    with pytest.raises(ValueError, match="coverage"):
        predict_report(report, fit(observations()))
    report = sample_report()
    report["front"][0]["additional_mass_kg"] = 1
    with pytest.raises(ValueError, match="метрики"):
        validated_front(report)
    report = sample_report()
    report["front"][0]["direction_candidate_indexes"] = [0, 0, 0]
    with pytest.raises(ValueError, match="четыре направления"):
        validated_front(report)


@pytest.mark.parametrize("tamper", ["duplicate_direction", "false_geometry", "false_coverage", "false_zone_geometry",
                                    "nan_mass", "bool_bars", "negative_cancel", "coverage_metric_mismatch"])
def test_report_validation_rejects_forged_direction_or_metrics(tamper):
    report = sample_report()
    direction = report["directions"][0]
    candidate = direction["candidates"][0]
    if tamper == "duplicate_direction":
        direction["direction"] = report["directions"][1]["direction"]
    elif tamper == "false_geometry":
        candidate["coverage"]["geometry_and_patterns_valid"] = False
    elif tamper == "false_coverage":
        candidate["coverage"]["coverage_passed"] = False
    elif tamper == "false_zone_geometry":
        candidate["coverage"]["zones"][0]["geometry_and_pattern_valid"] = False
    elif tamper == "nan_mass":
        candidate["metrics"]["additional_mass_kg"] = float("nan")
    elif tamper == "bool_bars":
        candidate["metrics"]["physical_bar_count"] = True
    elif tamper == "negative_cancel":
        candidate["metrics"]["additional_mass_kg"] = -10.0
        report["directions"][1]["candidates"][0]["metrics"]["additional_mass_kg"] = 30.0
    else:
        candidate["coverage"]["physical_bar_count"] = 999
    with pytest.raises(ValueError):
        validated_front(report)


def test_manifest_strict_grouped_case_provenance_and_one_point_rejection(tmp_path):
    cli = importlib.import_module("scripts.calibrate_zone_preference")
    report = sample_report()
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.json").write_text(json.dumps(report))
    rows = [{"case_id": case, "report_path": f"{name}.json", "reference_mass_kg": mass,
             "reference_bar_count": bars, "reference_source": "verified PDF"}
            for case, name, mass, bars in (("A", "a", 39, 41), ("A", "b", 39, 41), ("B", "c", 35, 46))]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": "zone-preference-manifest/v1", "cases": rows}))
    observations_, digest = cli.load_manifest(manifest)
    assert len(observations_) == 3 and len(digest) == 64
    output = tmp_path / "calibration.json"
    assert cli.main([str(manifest), "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["independent_case_count"] == 2
    assert result["model"]["training_case_ids"] == ["A", "B"]
    assert all(fold["held_out_case_id"] not in fold["training_case_ids"] for fold in result["folds"])
    rows[1]["reference_bar_count"] = 99
    manifest.write_text(json.dumps({"schema_version": "zone-preference-manifest/v1", "cases": rows}))
    with pytest.raises(ValueError, match="разные инженерные метки"):
        cli.load_manifest(manifest)
    rows[1]["reference_bar_count"] = 41
    report["front"] = report["front"][:1]
    (tmp_path / "b.json").write_text(json.dumps(report))
    manifest.write_text(json.dumps({"schema_version": "zone-preference-manifest/v1", "cases": rows}))
    with pytest.raises(ValueError, match="одноточечный"):
        cli.load_manifest(manifest)


def test_known_source_uses_held_out_model_and_unknown_source_full_model_without_changing_knee():
    report = sample_report()
    original_fingerprint = input_fingerprint(report)
    obs = observations()
    known = ZoneObservation("A", "a", "a" * 64, obs[0].front, 39, 41,
                            "verified PDF", original_fingerprint)
    second_report = sample_report()
    second_report["directions"][0]["source"]["sha256"]["shk"] = "f" * 64
    other = ZoneObservation("B", "b", "b" * 64, obs[1].front, 35, 46,
                            "verified PDF", input_fingerprint(second_report))
    bundle = build_bundle((known, other), evaluate((known, other)))
    known_prediction = build_engineering_preference(report, bundle)
    assert known_prediction["status"] == "available"
    assert known_prediction["prediction_scope"] == "held_out_engineering_case"
    assert known_prediction["training_case_ids"] == ["B"]
    assert known_prediction["model_id"].startswith("ridge-mass-bars-alpha-0.01/v1+")
    assert all(0 <= index < len(report["front"]) for index in known_prediction["top_indexes"])
    unknown_report = sample_report()
    unknown_report["directions"][0]["source"]["sha256"]["shk"] = "e" * 64
    unknown = build_engineering_preference(unknown_report, bundle)
    assert unknown["prediction_scope"] == "new_input"
    assert unknown["training_case_ids"] == ["A", "B"]
    assert build_engineering_preference(report, None)["status"] == "unavailable"
    assert build_engineering_preference(report, {"schema_version": "bad"})["status"] == "unavailable"
    contaminated = {**bundle, "held_out_models": {**bundle["held_out_models"], "A": bundle["full_model"]}}
    assert build_engineering_preference(report, contaminated)["status"] == "unavailable"
    stale_id = {**bundle, "model_id": "ridge-mass-bars-alpha-0.01/v1+000000000000"}
    assert build_engineering_preference(report, stale_id)["status"] == "unavailable"
    report["front"] = []
    assert build_engineering_preference(report, bundle)["status"] == "unavailable"


def test_rank_rejects_changed_feature_order():
    front = observations()[0].front
    model = fit(observations())
    model["feature_names"] = ["intercept", "bars_over_front_min_minus_one", "mass_over_front_min_minus_one"]
    with pytest.raises(ValueError, match="неизвестная модель"):
        rank(front, model)


def test_fingerprint_preserves_direction_and_scale_roles():
    original = sample_report()
    fingerprint = input_fingerprint(original)
    swapped = sample_report()
    swapped["directions"][0]["source"], swapped["directions"][1]["source"] = (
        swapped["directions"][1]["source"], swapped["directions"][0]["source"])
    assert input_fingerprint(swapped) != fingerprint
    changed_scale = sample_report()
    changed_scale["directions"][0]["source"]["sha256"] = {
        "dxf": changed_scale["directions"][0]["source"]["sha256"]["dxf"], "png": "a" * 64}
    assert input_fingerprint(changed_scale) != fingerprint


def test_empty_demand_direction_has_zero_legal_metrics_but_plate_remains_positive():
    report = sample_report()
    direction = report["directions"][0]
    for candidate in direction["candidates"]:
        candidate["metrics"].update(additional_mass_kg=0.0, physical_bar_count=0, zone_count=0)
        candidate["coverage"].update(additional_mass_kg=0.0, physical_bar_count=0, zone_count=0,
                                      demanded_cell_count=0, covered_cell_count=0, zones=[])
    for point in report["front"]:
        index = point["direction_candidate_indexes"][0]
        point["additional_mass_kg"] -= (10.0 if index == 0 else 8.0)
        point["physical_bar_count"] -= (10 if index == 0 else 12)
        point["zone_count"] -= (1 if index == 0 else 2)
    assert validated_front(report) == ((30.0, 30, 3), (24.0, 36, 6))
    direction["candidates"][0]["metrics"]["zone_count"] = 1
    with pytest.raises(ValueError, match="нулевой спрос"):
        validated_front(report)
