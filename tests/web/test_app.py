"""HTTP-тесты локального web-MVP без настоящего сервера."""

from __future__ import annotations

import importlib
from contextlib import ExitStack
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from rebar.application import DirectionAnalysis, PlateAnalysis
from rebar.optimization import (
    PLATE_DIRECTIONS,
    AlgorithmRequest,
    LayoutConstraints,
    PlateDirectionSolution,
    build_layout_problem,
    build_plate_pareto_front,
    build_plate_problem,
    build_plate_solution,
    built_in_optimizer_registry,
)

web_app = importlib.import_module("rebar.web.app")
client = TestClient(web_app.app)


def _analysis(mosaic) -> DirectionAnalysis:
    problem = build_layout_problem(
        mosaic,
        LayoutConstraints(min_width_cells=1, enforce_zone_gap=False),
    )
    solution = built_in_optimizer_registry().create("bbox").solve(
        problem,
        AlgorithmRequest(max_details=1),
    )
    return DirectionAnalysis(mosaic=mosaic, problem=problem, solutions=(solution,))


def _plate_analysis(mosaic, source_paths) -> PlateAnalysis:
    direction_analyses = tuple(
        _analysis(
            replace(
                mosaic,
                direction=direction,
                source_path=str(source.dxf_path),
            )
        )
        for direction, source in zip(PLATE_DIRECTIONS, source_paths)
    )
    problem = build_plate_problem(
        (analysis.problem for analysis in direction_analyses),
        case_id="web-plate",
    )
    solution = build_plate_solution(
        PlateDirectionSolution(
            direction=analysis.problem.demand.direction,
            solution=analysis.solutions[0],
        )
        for analysis in direction_analyses
    )
    front = build_plate_pareto_front(
        problem,
        {
            analysis.problem.demand.direction: analysis.solutions
            for analysis in direction_analyses
        },
    )
    return PlateAnalysis(
        problem=problem,
        direction_analyses=direction_analyses,
        solutions=(solution,),
        front=front,
    )


def test_landing_health_and_options_are_available():
    landing = client.get("/")
    health = client.get("/healthz")
    options = client.get("/api/options")
    script = client.get("/static/app.js")

    assert landing.status_code == 200
    assert "Раскладка дополнительной арматуры" in landing.text
    assert "Параметры задачи" in landing.text
    assert "Рабочая область" in landing.text
    assert "/static/app.js?v=web-mvp-9" in landing.text
    assert landing.headers["cache-control"] == "no-store, max-age=0"
    assert options.headers["cache-control"] == "no-store, max-age=0"
    assert script.headers["cache-control"] == "no-store, max-age=0"
    assert "normalizedOptions(payload)" in script.text
    assert "const OPTIONS_SCHEMA_VERSION = 2;" in script.text
    assert '"/api/analyze-plate"' in script.text
    assert "Скачать черновик JSON" in script.text
    assert "payload.demo_cases.map" not in script.text
    assert health.json() == {"status": "ok"}
    assert options.json()["schema_version"] == 2
    assert {item["id"] for item in options.json()["algorithms"]} == {
        "agglomerative",
        "bbox",
        "bsp",
        "genetic-pareto",
        "genetic-source-recovery",
        "greedy",
        "greedy-priority",
        "row-run-greedy",
        "spatial-partition-greedy",
        "strip-profile-dp",
    }
    assert {item["id"] for item in options.json()["cutting_profiles"]} == {
        "continuous",
        "plate-11700",
    }
    mappings = {item["id"]: item for item in options.json()["mappings"]}
    assert set(mappings) == {
        "auto",
        "k09-above-3-d10-v1",
        "k09-minus-2-d12-v1",
        "legacy-s1-t800-d18-v1",
        "plate-zero-d12-v1",
    }
    assert mappings["k09-above-3-d10-v1"]["title"] == "Плита над 3 этажом · ⌀10"
    assert mappings["k09-minus-2-d12-v1"]["title"] == "Плита над −2 этажом · ⌀12"
    assert options.json()["defaults"]["source_mode"] == "demo"
    assert options.json()["defaults"]["demo_id"] == "irregular-plate-x"
    assert options.json()["defaults"]["algorithms"] == ["genetic-pareto"]
    assert options.json()["defaults"]["max_details"] is None
    assert options.json()["defaults"]["complexity_axis"] == "position_count"
    assert options.json()["defaults"]["genetic_population_size"] == 16
    assert options.json()["references"] == [
        {
            "id": "plate-zero-k09",
            "title": "Корпус 2.9 · плита нуля",
            "expected_mass_kg": 3177.64,
            "expected_bar_count": 1019,
            "expected_position_count": 79,
        }
    ]
    assert options.json()["demo_cases"] == [
        {
            "id": "irregular-plate-x",
            "title": "Плита с несколькими уровнями",
            "description": "96 КЭ · нижнее армирование · ось X · шесть уровней As",
            "default": True,
        }
    ]


def test_demo_runs_real_dxf_pipeline_without_upload():
    response = client.post(
        "/api/demo",
        data={
            "demo_id": "irregular-plate-x",
            "algorithms": "bbox",
            "max_details": "4",
            "min_width_cells": "1",
            "cutting_profile": "continuous",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"]["filename"] == "Плита с несколькими уровнями"
    assert payload["source"]["source_kind"] == "demo"
    assert payload["source"]["source_id"] == "irregular-plate-x"
    assert payload["source"]["cell_count"] == 96
    assert payload["source"]["zone_count_bounds"] == {"minimum": 1, "maximum": 96}
    assert payload["source"]["single_cell_preprocessing"]["policy"].endswith("-v1")
    assert payload["source"]["mapping_id"] == "plate-zero-d12-v1"
    assert payload["source"]["a101_profile_id"] == "a101-2.4.4-ats3-zero-t240-v1"
    assert payload["solutions"][0]["algorithm"] == "bbox"
    assert payload["solutions"][0]["metrics"]["under_reinforced_cell_count"] == 0
    result = payload["solutions"][0]
    assert result["constructability"]["position_count"] == len(result["bar_schedule"])
    assert sum(row["physical_bar_count"] for row in result["bar_schedule"]) == result["physical_bar_count"]
    assert sum(row["total_mass_kg"] for row in result["bar_schedule"]) == pytest.approx(
        result["metrics"]["total_mass_kg"],
    )
    assert "#9F7FFF" in payload["solutions"][0]["svg"]
    assert "#FF0000" in payload["solutions"][0]["svg"]
    assert "ACI 181" in payload["solutions"][0]["svg"]


def test_composite_upload_returns_explicit_422_before_running_legacy_ga(two_background_top_x_sources):
    dxf, shk = two_background_top_x_sources
    with dxf.open("rb") as geometry, shk.open("rb") as scale:
        response = client.post("/api/analyze", files={
            "dxf": (dxf.name, geometry, "application/dxf"),
            "shk": (shk.name, scale, "application/octet-stream"),
        })
    assert response.status_code == 422
    assert "несколько дополнительных наборов" in response.json()["detail"]
    assert "recipe" in response.json()["detail"]


def test_demo_rejects_unknown_case():
    response = client.post("/api/demo", data={"demo_id": "not-a-demo"})

    assert response.status_code == 400
    assert "неизвестный демонстрационный пример" in response.json()["detail"]


def test_demo_rejects_zone_cap_above_finite_element_count():
    response = client.post(
        "/api/demo",
        data={
            "demo_id": "irregular-plate-x",
            "algorithms": "bbox",
            "max_details": "97",
            "min_width_cells": "1",
        },
    )

    assert response.status_code == 422
    assert "1..96" in response.json()["detail"]


def test_demo_genetic_algorithm_returns_clickable_pareto_front():
    response = client.post(
        "/api/demo",
        data={
            "demo_id": "irregular-plate-x",
            "algorithms": "genetic-pareto",
            "max_details": "12",
            "min_width_cells": "1",
            "genetic_population_size": "8",
            "genetic_generations": "5",
            "genetic_seed": "7",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == 3
    assert payload["pareto_front"]["complexity_axis"] == "position_count"
    assert payload["pareto_front"]["source_candidate_count"] >= 2
    assert len(payload["pareto_front"]["points"]) >= 2
    assert len(payload["solutions"]) == len(payload["pareto_front"]["points"])
    assert all(
        solution["algorithm"] == "genetic-pareto"
        for solution in payload["solutions"]
    )
    assert len(
        {
            solution["metrics"]["detail_count"]
            for solution in payload["solutions"]
        }
    ) >= 2


def test_recovery_algorithm_receives_explicit_search_parameters(monkeypatch, direction_mosaic):
    captured = {}

    def analyze(_path, **kwargs):
        captured.update(kwargs)
        return _analysis(direction_mosaic)

    monkeypatch.setattr(web_app, "analyze_direction", analyze)
    response = client.post("/api/demo", data={
        "algorithms": "genetic-source-recovery", "genetic_population_size": "8",
        "genetic_generations": "3", "genetic_seed": "17",
        "genetic_operator_policy": "uniform", "complexity_axis": "physical_bar_count",
    })
    assert response.status_code == 200
    assert captured["algorithm_names"] == ("genetic-source-recovery",)
    assert captured["algorithm_params"]["genetic-source-recovery"] == {
        "population_size": 8, "generations": 3, "random_seed": 17,
        "operator_policy": "uniform", "ucb_exploration": 2**0.5,
    }
    assert captured["complexity_axis"].value == "physical_bar_count"


def test_analyze_returns_metrics_zones_and_inline_svg(monkeypatch, direction_mosaic):
    captured = {}

    def fake_analyze(path, **kwargs):
        captured["filename"] = path.name
        captured.update(kwargs)
        return _analysis(direction_mosaic)

    monkeypatch.setattr(web_app, "analyze_direction", fake_analyze)
    response = client.post(
        "/api/analyze",
        files={"dxf": ("Нижняя по Х.dxf", b"dummy dxf", "application/dxf")},
        data={
            "mapping_id": "auto",
            "algorithms": "bbox",
            "max_details": "4",
            "min_width_cells": "1",
            "cutting_profile": "continuous",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source"]["filename"] == "Нижняя по Х.dxf"
    assert payload["source"]["source_kind"] == "upload"
    assert payload["source"]["direction"] == {"layer": "bottom", "axis": "X"}
    assert payload["solutions"][0]["algorithm"] == "bbox"
    assert payload["solutions"][0]["physical_bar_count"] > 0
    assert payload["solutions"][0]["gate_assessment"]["summary"]["pass"] >= 1
    assert payload["solutions"][0]["svg"].startswith("<svg")
    assert payload["solutions"][0]["zones"]
    assert captured["filename"] == "Нижняя по Х.dxf"
    assert captured["algorithm_names"] == ("bbox",)
    assert captured["max_details"] == 4
    assert captured["cutting_profile"] == "continuous"
    assert captured["complexity_axis"].value == "position_count"


@pytest.mark.parametrize("axis", ["position_count", "physical_bar_count", "zone_count"])
def test_analyze_passes_explicit_complexity_axis_to_shared_pipeline(monkeypatch, direction_mosaic, axis):
    captured = {}

    def fake_analyze(path, **kwargs):
        captured.update(kwargs)
        return _analysis(direction_mosaic)

    monkeypatch.setattr(web_app, "analyze_direction", fake_analyze)
    response = client.post("/api/analyze", files={
        "dxf": ("Нижняя по Х.dxf", b"dummy", "application/dxf"),
    }, data={"algorithms": "bbox", "complexity_axis": axis})
    assert response.status_code == 200
    assert captured["complexity_axis"].value == axis


def test_unknown_complexity_axis_is_rejected_before_optimization(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid axis must not start optimization")

    monkeypatch.setattr(web_app, "analyze_direction", unexpected)
    response = client.post("/api/analyze", files={
        "dxf": ("Нижняя по Х.dxf", b"dummy", "application/dxf"),
    }, data={"complexity_axis": "ambiguous-details"})
    assert response.status_code == 422


def test_analyze_rejects_wrong_extensions_and_conflicting_mapping():
    wrong_dxf = client.post(
        "/api/analyze",
        files={"dxf": ("input.txt", b"text", "text/plain")},
    )
    conflict = client.post(
        "/api/analyze",
        files={
            "dxf": ("Нижняя по Х.dxf", b"dxf", "application/dxf"),
            "shk": ("scale.shk", b"scale", "application/octet-stream"),
        },
        data={"mapping_id": "plate-zero-d12-v1"},
    )

    assert wrong_dxf.status_code == 400
    assert ".dxf" in wrong_dxf.json()["detail"]
    assert conflict.status_code == 400
    assert "либо .shk, либо" in conflict.json()["detail"]


def test_analyze_plate_returns_aggregate_metrics_and_four_svgs(
    monkeypatch,
    direction_mosaic,
):
    captured = {}

    def fake_analyze(sources, **kwargs):
        captured["sources"] = sources
        captured.update(kwargs)
        return _plate_analysis(direction_mosaic, sources)

    monkeypatch.setattr(web_app, "analyze_plate", fake_analyze)
    response = client.post(
        "/api/analyze-plate",
        files={
            "dxf_bottom_x": ("Нижняя по Х.dxf", b"bottom x", "application/dxf"),
            "dxf_bottom_y": ("Нижняя по У.dxf", b"bottom y", "application/dxf"),
            "dxf_top_x": ("Верхняя по Х.dxf", b"top x", "application/dxf"),
            "dxf_top_y": ("Верхняя по У.dxf", b"top y", "application/dxf"),
        },
        data={
            "case_id": "plate-web-test",
            "mapping_id": "plate-zero-d12-v1",
            "algorithms": "bbox",
            "max_details": "4",
            "min_width_cells": "1",
            "cutting_profile": "continuous",
            "reference_id": "plate-zero-k09",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == 4
    assert payload["kind"] == "plate"
    assert payload["plate"]["direction_count"] == 4
    assert payload["pareto_front"]["complexity_axis"] == "zone_count"
    assert payload["pareto_front"]["combination_count"] == 1
    assert [source["filename"] for source in payload["sources"]] == [
        "Нижняя по Х.dxf",
        "Нижняя по У.dxf",
        "Верхняя по Х.dxf",
        "Верхняя по У.dxf",
    ]
    solution = payload["solutions"][0]
    assert solution["algorithm"] == "bbox"
    assert solution["valid"] is True
    assert solution["metrics"]["direction_count"] == 4
    assert solution["metrics"]["zone_count"] == 4
    assert solution["constructability"]["zone_count"] == 4
    assert solution["candidate_id"].startswith("plate:")
    assert solution["gate_assessment"]["reference_id"] == "plate-zero-k09"
    gate_items = {item["id"]: item for item in solution["gate_assessment"]["items"]}
    assert gate_items["plate-directions"]["absolute_deviation"] == 0.0
    assert gate_items["reference-mass"]["target"] == 3177.64
    assert len(solution["direction_solutions"]) == 4
    revit_export = solution["revit_export"]
    assert revit_export["schema_version"] == "plate-solution-revit/v1"
    assert revit_export["contract_status"] == "draft"
    assert revit_export["checks"]["export_eligible"] is False
    assert "a101-allowed-positions" in revit_export["checks"]["blocking_check_ids"]
    assert len(revit_export["directions"]) == 4
    assert revit_export["position_count"] == len(revit_export["bar_schedule"])
    assert revit_export["position_count"] == solution["constructability"]["position_count"]
    assert not revit_export["steel_class_declared"]
    positions = {row["mark"]: row for row in revit_export["bar_schedule"]}
    quantities = {mark: 0 for mark in positions}
    for direction in revit_export["directions"]:
        for zone in direction["zones"]:
            assert zone["position_mark"] in positions
            quantities[zone["position_mark"]] += zone["bar_count"]
            assert zone["bbox_semantics"] == "bar_axis_envelope"
            assert zone["nominal_step_mm"] == zone["step_mm"]
            assert zone["axis_pattern"]["period_mm"] == zone["step_mm"]
            index = 1 if direction["axis"] == "X" else 0
            axes, body = zone["bbox_mm"], zone["straight_bar_body_bbox_mm"]
            assert body[index] == axes[index] - zone["diameter_mm"] / 2
            assert body[index + 2] == axes[index + 2] + zone["diameter_mm"] / 2
    assert quantities == {mark: row["physical_bar_count"] for mark, row in positions.items()}
    assert all(
        zone["mark"] and zone["callout"]
        for direction in revit_export["directions"]
        for zone in direction["zones"]
    )
    assert all(
        item["solution"]["svg"].startswith("<svg")
        for item in solution["direction_solutions"]
    )
    assert captured["max_details_per_direction"] == 4
    assert all(source.mapping_id == "plate-zero-d12-v1" for source in captured["sources"])


def test_analyze_plate_rejects_non_dxf_and_automatic_mapping():
    files = {
        "dxf_bottom_x": ("Нижняя по Х.txt", b"bottom x", "text/plain"),
        "dxf_bottom_y": ("Нижняя по У.dxf", b"bottom y", "application/dxf"),
        "dxf_top_x": ("Верхняя по Х.dxf", b"top x", "application/dxf"),
        "dxf_top_y": ("Верхняя по У.dxf", b"top y", "application/dxf"),
    }
    wrong_extension = client.post("/api/analyze-plate", files=files)
    files["dxf_bottom_x"] = ("Нижняя по Х.dxf", b"bottom x", "application/dxf")
    automatic_mapping = client.post(
        "/api/analyze-plate",
        files=files,
        data={"mapping_id": "auto"},
    )

    assert wrong_extension.status_code == 400
    assert "Все четыре файла" in wrong_extension.json()["detail"]
    assert automatic_mapping.status_code == 400
    assert "общий .shk" in automatic_mapping.json()["detail"]


def test_analyze_plate_accepts_one_explicit_shared_shk(
    monkeypatch,
    direction_mosaic,
):
    captured = {}

    def fake_analyze(sources, **kwargs):
        captured["sources"] = sources
        return _plate_analysis(direction_mosaic, sources)

    monkeypatch.setattr(web_app, "analyze_plate", fake_analyze)
    response = client.post(
        "/api/analyze-plate",
        files={
            "dxf_bottom_x": ("Нижняя по Х.dxf", b"bottom x", "application/dxf"),
            "dxf_bottom_y": ("Нижняя по У.dxf", b"bottom y", "application/dxf"),
            "dxf_top_x": ("Верхняя по Х.dxf", b"top x", "application/dxf"),
            "dxf_top_y": ("Верхняя по У.dxf", b"top y", "application/dxf"),
            "shk": ("Общая шкала.shk", b"shared scale", "application/octet-stream"),
        },
        data={
            "mapping_id": "auto",
            "algorithms": "bbox",
            "min_width_cells": "1",
        },
    )

    assert response.status_code == 200
    sources = captured["sources"]
    assert all(source.mapping_id == "auto" for source in sources)
    assert all(source.shk_path is not None for source in sources)
    assert len({str(source.shk_path) for source in sources}) == 1
    assert str(sources[0].shk_path).endswith("shared-Общая шкала.shk")


def test_analyze_plate_rejects_bad_or_conflicting_shared_shk():
    dxf_files = {
        "dxf_bottom_x": ("Нижняя по Х.dxf", b"bottom x", "application/dxf"),
        "dxf_bottom_y": ("Нижняя по У.dxf", b"bottom y", "application/dxf"),
        "dxf_top_x": ("Верхняя по Х.dxf", b"top x", "application/dxf"),
        "dxf_top_y": ("Верхняя по У.dxf", b"top y", "application/dxf"),
    }
    wrong_extension = client.post(
        "/api/analyze-plate",
        files={**dxf_files, "shk": ("scale.txt", b"scale", "text/plain")},
        data={"mapping_id": "auto"},
    )
    conflict = client.post(
        "/api/analyze-plate",
        files={
            **dxf_files,
            "shk": ("scale.shk", b"scale", "application/octet-stream"),
        },
        data={"mapping_id": "plate-zero-d12-v1"},
    )

    assert wrong_extension.status_code == 400
    assert "расширение .shk" in wrong_extension.json()["detail"]
    assert conflict.status_code == 400
    assert "либо общий .shk, либо" in conflict.json()["detail"]


def test_analyze_plate_real_multipart_pipeline(plate_zero_dxf_files):
    assert len(plate_zero_dxf_files) == 4
    field_names = ("dxf_bottom_x", "dxf_bottom_y", "dxf_top_x", "dxf_top_y")

    with ExitStack() as stack:
        files = {
            field_name: (
                path.name,
                stack.enter_context(path.open("rb")),
                "application/dxf",
            )
            for field_name, path in zip(field_names, plate_zero_dxf_files)
        }
        response = client.post(
            "/api/analyze-plate",
            files=files,
            data={
                "case_id": "plate-zero-web",
                "mapping_id": "plate-zero-d12-v1",
                "algorithms": "bbox",
                "max_details": "32",
                "min_width_cells": "2",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["plate"]["case_id"] == "plate-zero-web"
    assert payload["solutions"][0]["valid"] is True
    assert payload["solutions"][0]["metrics"]["direction_count"] == 4
    assert payload["solutions"][0]["metrics"]["physical_bar_count"] > 0
    assert len(payload["solutions"][0]["direction_solutions"]) == 4
