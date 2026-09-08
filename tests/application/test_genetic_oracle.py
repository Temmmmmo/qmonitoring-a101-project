"""Проверки сопоставимости GA/exact и честного представления недостигнутых бюджетов."""

import json
from dataclasses import replace

import pytest

from rebar.application.genetic_benchmark import GeneticRunConfig
from rebar.application.genetic_oracle import compare_genetic_with_oracle, small_oracle_problems
from rebar.optimization import ComplexityAxis, GeneticParetoOptimizer, evaluate_layout
from rebar.reporting.genetic_oracle import generate_genetic_oracle_report


@pytest.mark.parametrize("policy", ["uniform", "ucb1"])
@pytest.mark.parametrize("axis", list(ComplexityAxis))
def test_comparison_checks_public_ga_against_same_candidate_set(policy, axis):
    problem = small_oracle_problems()[0]
    config = GeneticRunConfig(8, 3, 17, operator_policy=policy, complexity_axis=axis)

    run = compare_genetic_with_oracle(problem, config)
    repeated = compare_genetic_with_oracle(problem, config)

    assert run.oracle.complete
    assert run.candidate_zones == repeated.candidate_zones
    assert run.budget_gaps == repeated.budget_gaps
    assert run.minimum_mass_gap_pct >= 0
    assert run.rejected_genetic_count == 0
    assert 0 < run.recovered_exact_points <= len(run.oracle.solutions)
    assert all(evaluate_layout(problem, solution.zones).valid for solution in run.genetic_solutions)
    assert all(point.gap_pct is None or point.gap_pct >= 0 for point in run.budget_gaps)


def test_report_preserves_same_pool_fingerprint_across_policies(tmp_path):
    problem = small_oracle_problems()[0]
    runs = tuple(
        compare_genetic_with_oracle(problem, GeneticRunConfig(8, 3, 7, operator_policy=policy))
        for policy in ("uniform", "ucb1")
    )

    report = generate_genetic_oracle_report(runs, tmp_path)
    payload = json.loads((tmp_path / "oracle.json").read_text())

    assert report == tmp_path / "index.html"
    assert (tmp_path / "runs.csv").is_file()
    assert payload["schema_version"] == 1
    assert payload["exact_scope"] == "finite_candidate_set_with_deterministic_detailing"
    assert payload["summaries"][0]["candidate_set_id"] == payload["summaries"][1]["candidate_set_id"]
    assert payload["runs"][0]["problem"]["demand"]["cells"]
    assert payload["runs"][0]["candidate_zones"]
    assert payload["runs"][0]["oracle"]["solutions"][0]["meta"]["genome_candidate_indexes"]
    assert all(row["complete"] for row in payload["summaries"])
    assert "не глобальный оптимум" in report.read_text()


def test_missing_budget_is_not_zero_gap_or_full_front_recall(tmp_path, monkeypatch):
    problem = replace(small_oracle_problems()[0], case_id="<script>not-html</script>")
    config = GeneticRunConfig(8, 3, 7)
    full = compare_genetic_with_oracle(problem, config)
    cheapest = min(full.genetic_solutions, key=lambda solution: solution.metrics.total_mass_kg)
    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", lambda *args: (cheapest,))

    run = compare_genetic_with_oracle(problem, config)
    report = generate_genetic_oracle_report((run,), tmp_path)
    payload = json.loads((tmp_path / "oracle.json").read_text())

    assert run.minimum_mass_gap_pct == 0
    assert any(point.gap_pct is None and point.genetic_mass_kg is None for point in run.budget_gaps)
    assert run.recovered_exact_points < len(run.oracle.solutions)
    assert payload["summaries"][0]["unreached_budget_count"] > 0
    assert payload["summaries"][0]["exact_front_recall"] < 1
    assert "не достигнут" in report.read_text()
    assert "<script>not-html</script>" not in report.read_text()
    assert "&lt;script&gt;not-html&lt;/script&gt;" in report.read_text()


@pytest.mark.parametrize("tamper", ["geometry", "metrics", "pool_size"])
def test_comparison_rejects_incomparable_ga_results(monkeypatch, tamper):
    problem = small_oracle_problems()[0]
    config = GeneticRunConfig(8, 3, 7)
    solution = compare_genetic_with_oracle(problem, config).genetic_solutions[0]
    if tamper == "geometry":
        solution = replace(solution, zones=(replace(solution.zones[0], id="unexpected"),))
    elif tamper == "metrics":
        solution = replace(solution, metrics=replace(
            solution.metrics, total_mass_kg=solution.metrics.total_mass_kg + 1,
        ))
    else:
        solution = replace(solution, meta={**solution.meta, "candidate_pool_size": 100})
    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", lambda *args: (solution,))

    with pytest.raises(ValueError, match="не совпадает|не совпадают"):
        compare_genetic_with_oracle(problem, config)


def test_report_rejects_empty_run_list(tmp_path):
    with pytest.raises(ValueError, match="хотя бы один запуск"):
        generate_genetic_oracle_report((), tmp_path)


@pytest.mark.parametrize("policy", ["uniform", "ucb1"])
@pytest.mark.parametrize("atoms", ["whole_tiles", "demand_fragments"])
def test_oracle_comparison_includes_layered_pool_and_local_search(policy, atoms):
    config = GeneticRunConfig(
        8, 3, 7, operator_policy=policy, candidate_expansion="layered", local_search_passes=4,
        coverage_atoms=atoms,
    )
    run = compare_genetic_with_oracle(small_oracle_problems()[0], config)
    assert run.oracle.complete
    assert run.minimum_mass_gap_pct == 0
    assert run.rejected_genetic_count == 0
    assert all(solution.meta["candidate_expansion"] == "layered" for solution in run.genetic_solutions)


@pytest.mark.parametrize("policy", ["uniform", "ucb1"])
def test_irregular_cell_boundary_uses_correct_fragmented_coverage_in_oracle_comparison(policy):
    problems = [problem for problem in small_oracle_problems() if problem.case_id.startswith("off-grid-boundary")]
    assert len(problems) == 2
    for problem in problems:
        run = compare_genetic_with_oracle(problem, GeneticRunConfig(8, 3, 7, operator_policy=policy))
        assert run.rejected_genetic_count == 0
        assert run.minimum_mass_gap_pct == 0
        assert run.recovered_exact_points == len(run.oracle.solutions)
