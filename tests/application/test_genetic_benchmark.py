"""Тесты воспроизводимого экспериментального контура GA."""

from __future__ import annotations

import importlib
import json
from dataclasses import replace

import pytest

from rebar.application import (
    GeneticRunConfig,
    GeneticRunResult,
    PlateAnalysis,
    PlateDirectionSource,
    run_genetic_benchmark,
)
from rebar.golden import (
    PLATE_ZERO_K09,
    get_engineer_reference_case,
    plate_metric_reference_from_engineer,
)
from rebar.optimization import (
    PLATE_DIRECTIONS,
    AlgorithmRequest,
    ComplexityAxis,
    LayoutConstraints,
    PlateDirectionSolution,
    StrongestBBoxOptimizer,
    build_layout_problem,
    build_plate_pareto_front,
    build_plate_problem,
    build_plate_solution,
)
from rebar.reporting import generate_genetic_benchmark_report

benchmark_module = importlib.import_module("rebar.application.genetic_benchmark")


def _plate_analysis(mosaic) -> PlateAnalysis:
    direction_analyses = []
    for direction in PLATE_DIRECTIONS:
        direction_mosaic = replace(mosaic, direction=direction)
        problem = build_layout_problem(
            direction_mosaic,
            LayoutConstraints(min_width_cells=1, enforce_zone_gap=False),
        )
        solution = StrongestBBoxOptimizer().solve(
            problem,
            AlgorithmRequest(max_details=1),
        )
        from rebar.application import DirectionAnalysis

        direction_analyses.append(
            DirectionAnalysis(
                mosaic=direction_mosaic,
                problem=problem,
                solutions=(solution,),
            )
        )
    plate_problem = build_plate_problem(
        analysis.problem for analysis in direction_analyses
    )
    plate_solution = build_plate_solution(
        PlateDirectionSolution(
            direction=analysis.problem.demand.direction,
            solution=analysis.solutions[0],
        )
        for analysis in direction_analyses
    )
    front = build_plate_pareto_front(
        plate_problem,
        {
            analysis.problem.demand.direction: analysis.solutions
            for analysis in direction_analyses
        },
    )
    return PlateAnalysis(
        problem=plate_problem,
        direction_analyses=tuple(direction_analyses),
        solutions=(plate_solution,),
        front=front,
    )


def test_benchmark_passes_reproducible_config_to_plate_analysis(monkeypatch):
    captured = []
    sentinel = object()

    def fake_analyze(sources, **kwargs):
        captured.append((sources, kwargs))
        return sentinel

    monkeypatch.setattr(benchmark_module, "analyze_plate", fake_analyze)
    sources = tuple(
        PlateDirectionSource(f"direction-{index}.dxf", mapping_id="mapping")
        for index in range(4)
    )
    config = GeneticRunConfig(
        population_size=12,
        generations=7,
        random_seed=19,
        candidate_trajectories=5,
        layer_bridge_span=9,
        complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT,
    )

    results = run_genetic_benchmark(sources, (config,), case_id="case")

    assert results == (GeneticRunResult(config=config, analysis=sentinel),)
    assert captured[0][1]["max_details_per_direction"] is None
    assert captured[0][1]["complexity_axis"] is ComplexityAxis.PHYSICAL_BAR_COUNT
    params = captured[0][1]["algorithm_params"]["genetic-pareto"]
    assert params["random_seed"] == 19
    assert params["population_size"] == 12
    assert params["candidate_trajectories"] == 5
    assert params["layer_bridge_span"] == 9
    assert params["complexity_axis"] == "physical_bar_count"
    assert params["operator_policy"] == "ucb1"
    assert params["baseline_seed_algorithms"] == (
        "agglomerative",
        "bsp",
        "greedy-priority",
    )


def test_benchmark_report_writes_parameters_gates_and_golden_deltas(
    tmp_path,
    direction_mosaic,
):
    analysis = _plate_analysis(direction_mosaic)
    config = GeneticRunConfig(4, 1, 7)

    report = generate_genetic_benchmark_report(
        (GeneticRunResult(config, analysis),),
        tmp_path,
        reference=PLATE_ZERO_K09,
    )

    payload = json.loads((tmp_path / "benchmark.json").read_text(encoding="utf-8"))
    assert report == tmp_path / "index.html"
    assert (tmp_path / "runs.csv").is_file()
    assert (tmp_path / "configurations.csv").is_file()
    assert (tmp_path / "candidates.csv").is_file()
    assert payload["runs"][0]["random_seed"] == 7
    assert payload["runs"][0]["plate_front_size"] == 1
    assert payload["candidates"][0]["mass_delta_kg"] is not None
    assert payload["candidates"][0]["gates_fail"] == 0
    assert payload["schema_version"] == 5
    assert payload["reference"]["source_kind"] == "strict_golden"
    assert payload["tuning_policy"]["mass_gate_target_pct"] == 15.0
    assert payload["runs"][0]["mass_gate_gap_pct"] is not None
    assert payload["configuration_summaries"][0]["seed_count"] == 1
    assert payload["configuration_summaries"][0]["median_mass_delta_pct"] == pytest.approx(
        payload["runs"][0]["minimum_mass_delta_pct"]
    )
    assert 0.0 <= payload["runs"][0]["normalized_hypervolume"] <= 1.0
    assert payload["configuration_summaries"][0][
        "median_normalized_hypervolume"
    ] == pytest.approx(payload["runs"][0]["normalized_hypervolume"])
    assert isinstance(payload["candidates"][0]["mass_gate_met"], bool)
    assert payload["comparison"]["selection_policy"] == (
        "minimum_mass_on_generated_valid_plate_front"
    )
    assert payload["comparison"]["engineer"]["position_count"] == 79
    assert payload["comparison"]["ours"]["zone_count"] == 4
    breakdown = payload["comparison"]["ours"]["mass_breakdown"]
    assert breakdown["required_mass_kg"] > 0
    assert (
        breakdown["required_mass_kg"]
        + breakdown["anchorage_addition_kg"]
        + breakdown["cutting_addition_kg"]
    ) == pytest.approx(breakdown["installed_mass_kg"])
    assert len(payload["comparison"]["directions"]) == 4
    report_html = report.read_text(encoding="utf-8")
    assert "Golden инженера ↔ наш кандидат минимальной массы" in report_html
    assert "79 позиций в PDF — не то же самое" in report_html
    assert "Наши параметрические зоны" in report_html


def test_benchmark_report_accepts_verified_plate_level_engineer_metrics(
    tmp_path,
    direction_mosaic,
):
    analysis = _plate_analysis(direction_mosaic)
    reference = plate_metric_reference_from_engineer(
        get_engineer_reference_case("k09-minus-2")
    )

    report = generate_genetic_benchmark_report(
        (GeneticRunResult(GeneticRunConfig(4, 1, 7), analysis),),
        tmp_path,
        reference=reference,
    )

    payload = json.loads((tmp_path / "benchmark.json").read_text(encoding="utf-8"))
    assert payload["reference"] == {
        "id": "k09-minus-2",
        "title": "Корпус 2.9 · плита над минус 2 этажом",
        "source_kind": "verified_pdf_spec",
        "mass_kg": 3260.62,
        "physical_bar_count": 856,
        "position_count": 51,
    }
    assert payload["comparison"]["directions"] == []
    report_html = report.read_text(encoding="utf-8")
    assert "Проверенная спецификация инженера ↔" in report_html
    assert "Покомпонентное сравнение и схемы" not in report_html
    assert "51 позиций в PDF" in report_html
