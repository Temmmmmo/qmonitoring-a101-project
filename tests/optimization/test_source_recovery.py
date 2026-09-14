"""Local repair keeps original demand and independently checked geometry."""

from dataclasses import replace

import pytest

from rebar.application.genetic_oracle import small_oracle_problems
from rebar.models import Axis, Direction, Layer
from rebar.optimization import AlgorithmRequest, LayoutSolution, SolutionStatus, evaluate_layout
from rebar.optimization.algorithms.source_recovery import recover_source_demand
from rebar.optimization.services import build_zone_from_bbox


def _case(axis, layer):
    original = small_oracle_problems()[0]
    original = replace(original, demand=replace(original.demand, direction=Direction(layer, axis)))
    downgraded = replace(original, demand=replace(original.demand, cells=tuple(
        replace(cell, level_index=1) for cell in original.demand.cells
    )))
    zone = build_zone_from_bbox(downgraded, original.demand.bbox, 1, "existing")
    evaluation = evaluate_layout(downgraded, (zone,))
    assert evaluation.valid
    old = LayoutSolution("old", SolutionStatus.FEASIBLE, (zone,), evaluation.metrics)
    return original, old


@pytest.mark.parametrize("axis", list(Axis))
@pytest.mark.parametrize("layer", list(Layer))
def test_repair_restores_full_original_coverage_without_mutation(axis, layer):
    original, old = _case(axis, layer)
    before_cells, before_zones = original.demand.cells, old.zones
    recovered = recover_source_demand(original, old)
    checked = evaluate_layout(original, recovered.zones)
    assert checked.valid and checked.metrics.under_reinforced_cell_count == 0
    assert recovered.metrics == checked.metrics
    assert original.demand.cells == before_cells and old.zones == before_zones
    assert recovered.meta["source_recovery"]["original_missing_cell_count"] == 1
    assert recovered.meta["source_recovery"]["installation_approved"] is False
    assert recovered.meta["source_recovery"]["moves"]
    assert all(z.anchored_length_mm == pytest.approx(z.required_length_mm + 80*z.rebar.diameter)
               for z in recovered.zones)


def test_already_sufficient_zone_is_not_replaced():
    original, old = _case(Axis.X, Layer.BOTTOM)
    strong = build_zone_from_bbox(original, original.demand.bbox, 2, "strong")
    evaluation = evaluate_layout(original, (strong,))
    old = replace(old, zones=(strong,), metrics=evaluation.metrics)
    recovered = recover_source_demand(original, old)
    assert recovered.zones == old.zones
    assert recovered.metrics == old.metrics


def test_bar_penalty_can_select_upgrade_instead_of_extra_zone():
    original, old = _case(Axis.Y, Layer.TOP)
    old = replace(old, request=AlgorithmRequest(max_details=1))
    recovered = recover_source_demand(original, old, bar_penalty_kg=2)
    assert len(recovered.zones) == 1
    assert recovered.metrics.under_reinforced_cell_count == 0
    assert evaluate_layout(original, recovered.zones, old.request).valid


@pytest.mark.parametrize("penalty", [-1, float("nan"), float("inf"), True, "2"])
@pytest.mark.parametrize("field", ["bar_penalty_kg", "position_penalty_kg"])
def test_invalid_penalty_is_not_silently_accepted(penalty, field):
    original, old = _case(Axis.X, Layer.BOTTOM)
    with pytest.raises(ValueError):
        recover_source_demand(original, old, **{field: penalty})


@pytest.mark.parametrize("neighbors", [True, 0, 65, 1.5])
def test_neighbor_budget_is_explicitly_bounded(neighbors):
    original, old = _case(Axis.X, Layer.BOTTOM)
    with pytest.raises(ValueError):
        recover_source_demand(original, old, neighbor_count=neighbors)
