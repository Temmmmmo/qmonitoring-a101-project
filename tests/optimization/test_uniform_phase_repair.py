"""Локальный phase repair — опционален, не меняет материал и не скрывает ограничения."""

from copy import deepcopy
from dataclasses import replace
from importlib import import_module

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar
from rebar.application.genetic_oracle import small_oracle_problems
from rebar.optimization import LayoutConstraints, build_layout_problem
from rebar.optimization.services import build_zone_from_bbox, evaluate_layout
from rebar.optimization.services.bar_geometry import bar_coordinates, bars_conflict, longitudinal_interval
from rebar.optimization.services.phase import feasible_first_bar_coordinates
from rebar.optimization.services.uniform_phase_repair import repair_uniform_zone_phases


def _case(axis=Axis.X, layer=Layer.BOTTOM, *, count=3, step=300, spacing=0):
    background = Rebar(300, 12)
    band = Band(1, 254, f"s300d12+s{step}d12", 7.54, background, Rebar(step, 12))
    levels = [Band(0, 181, "s300d12", 3.77, background, None), band]
    cells = []
    for index in range(count):
        start = index * 1000
        bounds = (start, 0, start + 1000, 500) if axis is Axis.X else (0, start, 500, start + 1000)
        x0, y0, x1, y1 = bounds
        cells.append(Cell([(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                          ((x0 + x1) / 2, (y0 + y1) / 2), 254, band))
    bounds = (0, 0, count * 1000, 500) if axis is Axis.X else (0, 0, 500, count * 1000)
    problem = build_layout_problem(
        Mosaic(Direction(layer, axis), cells, levels, bounds),
        LayoutConstraints(min_width_cells=2, minimum_clear_spacing_mm=spacing),
    )
    zones = tuple(build_zone_from_bbox(
        problem,
        (i * 1000, 0, (i + 1) * 1000, 500) if axis is Axis.X else (0, i * 1000, 500, (i + 1) * 1000),
        1, f"zone-{i}", seed_cell_ids=(i,),
    ) for i in range(count))
    return problem, zones


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
@pytest.mark.parametrize("layer", (Layer.BOTTOM, Layer.TOP))
def test_repair_removes_bar_conflicts_but_keeps_zone_gap_separate(axis, layer):
    problem, zones = _case(axis, layer)
    snapshot = deepcopy((problem, zones))
    result = repair_uniform_zone_phases(problem, zones)

    assert len(result.conflicting_zone_pairs_before) == 2
    assert result.conflicting_zone_pairs_after == ()
    assert len(result.zone_gap_pairs_before) == len(result.zone_gap_pairs_after) == 2
    assert result.moves == 1
    assert result.evaluation.valid
    assert result.evaluation.metrics.under_reinforced_cell_count == 0
    assert any("крайними стержнями" in message for message in result.evaluation.diagnostics)
    assert not result.placement_eligible
    assert (problem, zones) == snapshot
    for previous, current in zip(zones, result.zones, strict=True):
        for name in ("id", "demand_bbox", "level_index", "rebar", "width_mm", "required_length_mm",
                     "anchored_length_mm", "installed_length_mm", "bar_count", "mass_kg", "covered_cell_ids"):
            assert getattr(current, name) == getattr(previous, name)
        assert longitudinal_interval(axis, current.bbox) == longitudinal_interval(axis, previous.bbox)
        domain = feasible_first_bar_coordinates(problem, previous)
        assert min(domain) <= current.first_bar_coordinate_mm <= max(domain)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_explicit_minimum_clear_spacing_is_preserved(axis):
    problem, zones = _case(axis, count=2, spacing=40)
    result = repair_uniform_zone_phases(problem, zones)
    assert not result.conflicting_zone_pairs_after
    assert not bars_conflict(axis, *result.zones, minimum_clear_spacing_mm=40)
    assert min(abs(first - second) for first in bar_coordinates(result.zones[0])
               for second in bar_coordinates(result.zones[1])) >= 52 - 1e-6


def test_unresolvable_spacing_is_reported_without_success_claim():
    problem, zones = _case(count=2, spacing=400)
    result = repair_uniform_zone_phases(problem, zones)
    assert result.conflicting_zone_pairs_before == result.conflicting_zone_pairs_after
    assert result.zones == zones
    assert result.moves == 0
    assert not result.placement_eligible
    assert any("конфликтуют" in message for message in result.evaluation.diagnostics)


def test_input_order_does_not_change_deterministic_phase_choice():
    problem, zones = _case(count=5)
    forward = repair_uniform_zone_phases(problem, zones)
    reverse = repair_uniform_zone_phases(problem, tuple(reversed(zones)))
    assert {zone.id: zone.first_bar_coordinate_mm for zone in forward.zones} == {
        zone.id: zone.first_bar_coordinate_mm for zone in reverse.zones
    }
    assert forward.conflicting_zone_pairs_after == reverse.conflicting_zone_pairs_after
    assert repair_uniform_zone_phases(problem, zones) == forward


@pytest.mark.parametrize("budget", (0, 1, 2, 5))
def test_search_budget_is_bounded_and_never_worsens_conflict_count(budget):
    problem, zones = _case(count=5)
    result = repair_uniform_zone_phases(problem, zones, maximum_pair_checks=budget)
    assert result.pair_checks <= budget
    assert len(result.conflicting_zone_pairs_after) <= len(result.conflicting_zone_pairs_before)
    assert result.evaluation.valid
    assert result.evaluation.metrics.physical_bar_count == sum(zone.bar_count for zone in zones)
    if budget == 0:
        assert result.zones == zones
        assert result.budget_exhausted


def test_disabled_passes_preserve_original_and_stable_solution_is_not_moved():
    problem, zones = _case()
    disabled = repair_uniform_zone_phases(problem, zones, maximum_passes=0)
    assert disabled.zones == zones
    assert disabled.moves == disabled.passes == disabled.pair_checks == 0
    once = repair_uniform_zone_phases(problem, zones)
    stable = repair_uniform_zone_phases(problem, once.zones)
    assert stable.zones == once.zones
    assert stable.moves == 0


def test_periodic_150_limitation_remains_explicit_even_if_uniform_conflicts_are_resolved():
    problem, zones = _case(step=150)
    result = repair_uniform_zone_phases(problem, zones)
    assert result.conflicting_zone_pairs_after == ()
    assert any("@150" in message and "100/200" in message for message in result.limitations)
    assert not result.placement_eligible


def test_missing_demand_cannot_be_hidden_by_phase_repair():
    problem, zones = _case()
    with pytest.raises(ValueError, match="hard-valid"):
        repair_uniform_zone_phases(problem, zones[:1])


def test_mutated_builder_cannot_change_bar_count(monkeypatch):
    problem, zones = _case()
    module = import_module("rebar.optimization.services.uniform_phase_repair")

    def invalid_builder(*args, **kwargs):
        zone = build_zone_from_bbox(*args, **kwargs)
        return replace(zone, bar_count=zone.bar_count + 1)

    monkeypatch.setattr(module, "build_zone_from_bbox", invalid_builder)
    with pytest.raises(ValueError, match="изменил состав"):
        repair_uniform_zone_phases(problem, zones)
    assert evaluate_layout(problem, zones).valid


def test_constructor_errors_are_not_swallowed(monkeypatch):
    problem, zones = _case()
    module = import_module("rebar.optimization.services.uniform_phase_repair")

    def invalid_builder(*args, **kwargs):
        raise ValueError("controlled builder failure")

    monkeypatch.setattr(module, "build_zone_from_bbox", invalid_builder)
    with pytest.raises(ValueError, match="controlled builder failure"):
        repair_uniform_zone_phases(problem, zones)


def test_empty_demand_with_empty_layout_is_supported():
    problem, _zones = _case()
    problem = replace(problem, demand=replace(problem.demand, cells=()))
    result = repair_uniform_zone_phases(problem, ())
    assert result.zones == ()
    assert result.evaluation.valid
    assert result.moves == 0


@pytest.mark.parametrize("options", (
    {"maximum_passes": -1}, {"maximum_passes": True},
    {"maximum_pair_checks": -1}, {"maximum_pair_checks": 1.5},
    {"maximum_phase_candidates": 9}, {"maximum_phase_candidates": False},
))
def test_invalid_budget_is_rejected(options):
    problem, zones = _case()
    with pytest.raises(ValueError, match="должен быть целым"):
        repair_uniform_zone_phases(problem, zones, **options)


@pytest.mark.parametrize("spacing", (float("nan"), float("inf")))
def test_nonfinite_spacing_cannot_silently_disable_physical_conflicts(spacing):
    problem, zones = _case()
    problem = replace(problem, constraints=replace(problem.constraints, minimum_clear_spacing_mm=spacing))
    with pytest.raises(ValueError, match="minimum_clear_spacing_mm"):
        repair_uniform_zone_phases(problem, zones)


def test_json_decoded_seed_lists_are_accepted_without_mutating_original_metadata():
    problem, zones = _case()
    decoded = tuple(replace(zone, meta={**zone.meta, "seed_cell_ids": list(zone.meta["seed_cell_ids"])})
                    for zone in zones)
    original = deepcopy(decoded)
    result = repair_uniform_zone_phases(problem, decoded)
    assert result.conflicting_zone_pairs_after == ()
    assert decoded == original


def test_legacy_seed_levels_remain_provenance_not_a_claim_of_sufficient_coverage():
    problem = small_oracle_problems()[0]
    weak = build_zone_from_bbox(problem, problem.demand.bbox, 1, "weak", first_bar_coordinate_mm=0)
    weak = replace(weak, meta={**weak.meta, "seed_cell_ids": [0, 1, 2]})
    strong = build_zone_from_bbox(problem, (500, 0, 1000, 500), 2, "strong", seed_cell_ids=(1,))
    zones = (weak, strong)
    result = repair_uniform_zone_phases(problem, zones)
    assert len(result.conflicting_zone_pairs_before) == 1
    assert result.conflicting_zone_pairs_after == ()
    assert result.evaluation.metrics.under_reinforced_cell_count == 0
    assert result.zones[0].meta == weak.meta
    assert 1 not in result.zones[0].covered_cell_ids
    assert 1 in result.zones[1].covered_cell_ids


def test_unknown_provenance_id_is_not_hidden_when_rebuilding():
    problem, zones = _case()
    invalid = tuple(replace(zone, meta={**zone.meta, "seed_cell_ids": [999999]}) for zone in zones)
    with pytest.raises(ValueError, match="неизвестные исходные"):
        repair_uniform_zone_phases(problem, invalid)
