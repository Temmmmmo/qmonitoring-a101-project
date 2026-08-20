"""HTTP-тесты локального web-MVP без настоящего сервера."""

from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

from rebar.application import DirectionAnalysis
from rebar.optimization import (
    AlgorithmRequest,
    LayoutConstraints,
    build_layout_problem,
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


def test_landing_health_and_options_are_available():
    landing = client.get("/")
    health = client.get("/healthz")
    options = client.get("/api/options")

    assert landing.status_code == 200
    assert "Раскладка дополнительной арматуры" in landing.text
    assert "Параметры задачи" in landing.text
    assert "Рабочая область" in landing.text
    assert health.json() == {"status": "ok"}
    assert {item["id"] for item in options.json()["algorithms"]} == {
        "agglomerative",
        "bbox",
        "bsp",
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
    assert payload["source"]["direction"] == {"layer": "bottom", "axis": "X"}
    assert payload["solutions"][0]["algorithm"] == "bbox"
    assert payload["solutions"][0]["physical_bar_count"] > 0
    assert payload["solutions"][0]["svg"].startswith("<svg")
    assert payload["solutions"][0]["zones"]
    assert captured["filename"] == "Нижняя по Х.dxf"
    assert captured["algorithm_names"] == ("bbox",)
    assert captured["max_details"] == 4
    assert captured["cutting_profile"] == "continuous"


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
