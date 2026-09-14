"""The opt-in wrapper must earn feasibility on unchanged source demand."""

from copy import deepcopy
from dataclasses import replace
from importlib import import_module

import pytest

from rebar.models import Axis, Direction, Layer, Rebar
from rebar.optimization import (
    AlgorithmRequest,
    ComplexityAxis,
    DemandCell,
    DemandLevel,
    DemandMap,
    GeneticParetoOptimizer,
    GeneticSourceRecoveryOptimizer,
    LayoutConstraints,
    LayoutProblem,
    LayoutSolution,
    SolutionStatus,
    apply_single_cell_rule,
    build_zone_from_bbox,
    built_in_optimizer_registry,
    evaluate_layout,
)

module = import_module("rebar.optimization.algorithms.genetic_source_recovery")


def _original():
    cells = tuple(DemandCell(
        i + 10, ((500.0 * i, 0.0), (500.0 * i + 500, 0.0),
                 (500.0 * i + 500, 500.0), (500.0 * i, 500.0)),
        (500.0 * i + 250, 250), i + 50, level,
    ) for i, level in enumerate((1, 2, 1)))
    return LayoutProblem(
        DemandMap(
            Direction(Layer.BOTTOM, Axis.X),
            tuple(DemandLevel(i, i + 50, None, None, None, rebar, rebar is not None)
                  for i, rebar in enumerate((None, Rebar(100, 12), Rebar(100, 16)))),
            cells, (0, 0, 1500, 500), source_path="matching-source.dxf",
            meta={"source_id": "unchanged"},
        ),
        LayoutConstraints(min_width_cells=1),
        meta={"provenance": {"id": "original"}},
    )


def _proposal(problem, request, level=1):
    zone = build_zone_from_bbox(problem, problem.demand.bbox, level, "proposal")
    evaluation = evaluate_layout(problem, (zone,), request)
    return LayoutSolution(
        "genetic-pareto", SolutionStatus.FEASIBLE, (zone,), evaluation.metrics,
        request=request, diagnostics=(*evaluation.diagnostics, "WARNING: explicit proposal timeout"),
        meta={"random_seed": 7, "ancestry": {"source": "saved"}},
    )


def test_registry_exposes_opt_in_without_changing_old_default():
    registry = built_in_optimizer_registry()
    assert isinstance(registry.create("genetic-source-recovery"), GeneticSourceRecoveryOptimizer)
    assert isinstance(registry.create("genetic-pareto"), GeneticParetoOptimizer)
    assert import_module("rebar.application.analyze_direction").DEFAULT_ALGORITHMS == ("genetic-pareto",)


def test_wrapper_recovers_real_peak_and_preserves_original_source(monkeypatch):
    original = _original()
    before = deepcopy(original)
    proposal_inputs = []
    repair_inputs = []
    request = AlgorithmRequest(max_details=1, params={
        "population_size": 4, "generations": 1, "random_seed": 7,
        "complexity_axis": ComplexityAxis.PHYSICAL_BAR_COUNT.value,
    })

    def propose(_self, reduced, actual_request):
        proposal_inputs.append((deepcopy(reduced), deepcopy(actual_request)))
        return (_proposal(reduced, actual_request),)

    actual_repair = module.recover_source_demand

    def repair(problem, solution, **kwargs):
        repair_inputs.append((problem, kwargs))
        return actual_repair(problem, solution, **kwargs)

    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", propose)
    monkeypatch.setattr(module, "recover_source_demand", repair)
    result, = GeneticSourceRecoveryOptimizer().solve_many(original, request)

    assert result.status is SolutionStatus.FEASIBLE
    assert result.algorithm == "genetic-source-recovery"
    assert result.metrics.under_reinforced_cell_count == 0
    assert evaluate_layout(original, result.zones, request).valid
    assert result.metrics.detail_count == 1
    assert result.zones[0].level_index == 2
    assert original == before
    assert [cell.level_index for cell in proposal_inputs[0][0].demand.cells] == [0, 1, 0]
    assert proposal_inputs[0][1] == request
    assert [item[1]["bar_penalty_kg"] for item in repair_inputs] == [0.0, 2.0]
    assert all(item[0] is original for item in repair_inputs)
    assert result.meta["ancestry"] == {"source": "saved"}
    assert any("explicit proposal timeout" in message for message in result.diagnostics)
    audit = result.meta["genetic_source_recovery"]
    assert audit["proposal_preprocessing"]["changed_count"] == 3
    assert audit["final_demand_policy"] == "preserve-original-demand"
    assert audit["statistics"]["repair_attempt_count"] == 2
    assert audit["statistics"]["accepted_unique_count"] == 1
    assert audit["statistics"]["duplicate_geometry_count"] == 1
    assert audit["placement_eligible"] is False
    assert result.meta["source_recovery_proposal"]["original_missing_before_recovery"] == 1


def test_already_sufficient_proposal_skips_unnecessary_repair(monkeypatch):
    original = _original()
    request = AlgorithmRequest(max_details=2)
    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", lambda _self, reduced, req: (
        _proposal(reduced, req, level=2),
    ))

    def forbidden_repair(*_args, **_kwargs):
        raise AssertionError("a source-valid unchanged layout needs no repair")

    monkeypatch.setattr(module, "recover_source_demand", forbidden_repair)
    solution = GeneticSourceRecoveryOptimizer().solve(original, request)
    assert evaluate_layout(original, solution.zones, request).valid
    assert solution.meta["genetic_source_recovery"]["statistics"]["unchanged_source_valid_count"] == 1
    assert solution.meta["genetic_source_recovery"]["statistics"]["repair_attempt_count"] == 0


def test_lying_repair_status_and_metrics_cannot_hide_original_deficiency(monkeypatch):
    original = _original()
    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", lambda _self, reduced, req: (
        _proposal(reduced, req),
    ))

    def lie(_original, proposal, **_kwargs):
        return replace(proposal, status=SolutionStatus.FEASIBLE,
                       metrics=replace(proposal.metrics, under_reinforced_cell_count=0))

    monkeypatch.setattr(module, "recover_source_demand", lie)
    result, = GeneticSourceRecoveryOptimizer().solve_many(original)
    assert result.status is SolutionStatus.ERROR
    assert result.metrics.under_reinforced_cell_count == 1
    assert len(result.zones) == 1  # Failed geometry is visible, not a valid reduced fallback.
    assert not evaluate_layout(original, result.zones, result.request).valid
    assert result.meta["genetic_source_recovery"]["statistics"]["accepted_unique_count"] == 0
    assert any("no original-hard-valid candidate" in message for message in result.diagnostics)


def test_requested_cap_is_not_weakened_by_a_generator_or_repair(monkeypatch):
    original = _original()

    def propose(_self, reduced, request):
        first = build_zone_from_bbox(reduced, (0, 0, 1000, 500), 2, "first")
        second = build_zone_from_bbox(reduced, (1000, 0, 1500, 500), 2, "second")
        evaluation = evaluate_layout(reduced, (first, second))
        return (LayoutSolution("genetic-pareto", SolutionStatus.FEASIBLE, (first, second),
                               evaluation.metrics, request=replace(request, max_details=99)),)

    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", propose)
    result = GeneticSourceRecoveryOptimizer().solve(original, AlgorithmRequest(max_details=1))
    assert result.status is SolutionStatus.ERROR
    assert result.request.max_details == 1
    assert result.metrics.under_reinforced_cell_count == 0  # Still invalid by the cap.
    assert any("max_details=1" in message for message in result.diagnostics)


def test_implicit_ga_maximum_zones_also_limits_recovery(monkeypatch):
    original = _original()
    seen = []

    def propose(_self, reduced, request):
        seen.append(request.max_details)
        return (_proposal(reduced, request),)

    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", propose)
    result = GeneticSourceRecoveryOptimizer().solve(
        original, AlgorithmRequest(params={"maximum_zones": 1}),
    )
    assert seen == [1]
    assert result.status is SolutionStatus.FEASIBLE
    assert result.metrics.detail_count == result.request.max_details == 1


def test_corrupt_proposal_is_rejected_not_reconstructed_as_new_geometry(monkeypatch):
    original = _original()

    def propose(_self, reduced, request):
        good = _proposal(reduced, request)
        return (replace(good, zones=(replace(good.zones[0], mass_kg=0),)),)

    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", propose)
    result = GeneticSourceRecoveryOptimizer().solve(original)
    assert result.status is SolutionStatus.ERROR
    assert result.zones == ()
    assert result.metrics.demanded_cell_count == result.metrics.under_reinforced_cell_count == 3
    assert result.meta["genetic_source_recovery"]["rejections"][0]["reason"] == "invalid unchanged proposal"


def test_empty_proposal_population_gives_explicit_original_metrics(monkeypatch):
    original = _original()
    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", lambda *_args: ())
    result = GeneticSourceRecoveryOptimizer().solve(original)
    assert result.status is SolutionStatus.ERROR
    assert result.metrics.demanded_cell_count == 3
    assert result.metrics.under_reinforced_cell_count == 3
    assert result.meta["genetic_source_recovery"]["statistics"]["proposal_count"] == 0


def test_already_reduced_source_is_rejected_before_running_ga(monkeypatch):
    original = _original()

    def forbidden(*_args):
        raise AssertionError("already-reduced source must be rejected first")

    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", forbidden)
    with pytest.raises(ValueError, match="already modified"):
        GeneticSourceRecoveryOptimizer().solve(apply_single_cell_rule(original, policy="legacy-research"))


@pytest.mark.parametrize("params", [
    {"source_recovery_bar_penalties": []},
    {"source_recovery_bar_penalties": [0] * 9},
    {"source_recovery_bar_penalties": "0,2"},
    {"source_recovery_bar_penalties": [float("nan")]},
    {"source_recovery_bar_penalties": [True]},
    {"source_recovery_bar_penalties": [-1]},
    {"source_recovery_position_penalty_kg": float("inf")},
    {"source_recovery_neighbor_count": 0},
    {"source_recovery_neighbor_count": 65},
    {"source_recovery_neighbor_count": True},
    {"maximum_zones": 0},
])
def test_invalid_or_unbounded_repair_parameters_fail_before_ga(monkeypatch, params):
    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", lambda *_args: pytest.fail("unexpected GA"))
    with pytest.raises(ValueError):
        GeneticSourceRecoveryOptimizer().solve(_original(), AlgorithmRequest(params=params))


def test_no_original_extra_demand_is_valid_without_running_ga(monkeypatch):
    original = _original()
    original = replace(original, demand=replace(original.demand, cells=tuple(
        replace(cell, level_index=0) for cell in original.demand.cells
    )))
    monkeypatch.setattr(GeneticParetoOptimizer, "solve_many", lambda *_args: pytest.fail("unexpected GA"))
    result = GeneticSourceRecoveryOptimizer().solve(original)
    assert result.status is SolutionStatus.FEASIBLE
    assert result.metrics.total_mass_kg == 0
    assert result.zones == ()


@pytest.mark.parametrize("axis", list(ComplexityAxis))
def test_application_forwards_selected_complexity_axis_to_opt_in_optimizer(axis):
    scenario = import_module("rebar.application.analyze_direction")
    request = scenario._algorithm_requests(
        ("genetic-source-recovery",), max_details=3, detail_penalty_kg=0,
        complexity_axis=axis, algorithm_params=None,
    )["genetic-source-recovery"]
    assert request.params["complexity_axis"] == axis.value
