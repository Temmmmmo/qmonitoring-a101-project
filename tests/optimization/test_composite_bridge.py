"""The optional post-MILP bridge preserves every old mass/count budget."""

from dataclasses import replace

import pytest

from rebar.models import Axis
from rebar.optimization.algorithms.composite_bridge import bridge_composite_recombined
from rebar.optimization.contracts.composite_search import CompositeSearchPoint, CompositeSearchResult
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_windows import covering_composite_window
from test_composite_merge import _problem


def _four_islands(axis=Axis.X, *, separation=300):
    original = _problem(axis)
    exemplar = original.demand.cells[1]
    cells = []
    for index, start in enumerate((0, 600 + separation, 3000, 3600 + separation)):
        target = (start, 0) if axis is Axis.X else (0, start)
        delta = (target[0] - min(x for x, _ in exemplar.poly),
                 target[1] - min(y for _, y in exemplar.poly))
        cells.append(replace(exemplar, id=100 + index, level_index=1, aci=1,
            poly=tuple((x + delta[0], y + delta[1]) for x, y in exemplar.poly),
            centroid=(exemplar.centroid[0] + delta[0], exemplar.centroid[1] + delta[1])))
    points = [point for cell in cells for point in cell.poly]
    bbox = (min(x for x, _ in points), min(y for _, y in points),
            max(x for x, _ in points), max(y for _, y in points))
    demand = replace(original.demand, cells=tuple(cells), bbox=bbox)
    problem = replace(original, demand=demand)
    placement = dict(problem.placements)[1]
    zones = []
    for cell in cells:
        x, y = zip(*cell.poly)
        requested = (min(x), min(y), max(x), max(y))
        window = covering_composite_window(demand, requested, 1, placement,
            constraints=problem.constraints)
        zones.append(build_composite_zone(demand, window, 1, f"original-{len(zones)}",
            placement, constraints=problem.constraints))
    check = evaluate_composite_coverage(demand, tuple(zones),
        policy_id=problem.policy_id, constraints=problem.constraints)
    assert check.status == "pass"
    baseline = CompositeSearchResult((CompositeSearchPoint(tuple(zones), check),), 0,
                                     {"runtime_s": 10.0})
    return problem, baseline


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_two_independent_gaps_merge_with_cached_pairs_and_full_checker(axis):
    problem, baseline = _four_islands(axis)
    result = bridge_composite_recombined(problem, baseline)
    assert len(result.points) == 1
    best = result.points[0]
    assert len(best.zones) == 2
    assert best.coverage.additional_mass_kg < baseline.points[0].coverage.additional_mass_kg
    assert result.telemetry["bridge"]["accepted_merges"] == 2
    assert result.telemetry["bridge"]["evaluated_pairs"] == 2
    assert result.telemetry["bridge"]["retained_mass_gain_kg"] == pytest.approx(
        baseline.points[0].coverage.additional_mass_kg - best.coverage.additional_mass_kg)
    assert result.telemetry["runtime_s"] >= 10
    assert "bridge" in result.telemetry["algorithm"]
    assert all(zone.recipe == baseline.points[0].zones[0].recipe
               and zone.placement == baseline.points[0].zones[0].placement
               and len(zone.components) == 1 for zone in best.zones)
    assert evaluate_composite_coverage(problem.demand, best.zones,
        policy_id=problem.policy_id, constraints=problem.constraints) == best.coverage


def test_overlength_and_host_rejection_retain_exact_old_front(monkeypatch):
    problem, baseline = _four_islands()
    limited = bridge_composite_recombined(problem, baseline, maximum_bar_length_mm=2000)
    assert limited.points == baseline.points
    assert limited.telemetry["bridge"]["rejections"]["overlength"] >= 1

    def forbidden(*args, **kwargs):
        raise ValueError("synthetic host rejects bridge")

    monkeypatch.setattr("rebar.optimization.algorithms.composite_bridge.fit_composite_zone_to_host", forbidden)
    from rebar.optimization.contracts.host import RectangularHostEnvelope
    host = RectangularHostEnvelope((-1000, -1000, 6000, 2000), (), 0, 300, 25, 25, 25, "host")
    hosted = bridge_composite_recombined(replace(problem, host_envelope=host), baseline)
    assert hosted.points == baseline.points
    assert hosted.telemetry["bridge"]["rejections"]["ValueError"] >= 1


def test_no_cheaper_union_or_pair_budget_preserves_old_front(monkeypatch):
    problem, baseline = _four_islands()
    import rebar.optimization.algorithms.composite_bridge as bridge_module
    original_check = bridge_module.check_composite_zone_coverage_geometry

    def expensive(*args, **kwargs):
        checked = original_check(*args, **kwargs)
        return replace(checked, additional_mass_kg=checked.additional_mass_kg + 100)

    monkeypatch.setattr(bridge_module, "check_composite_zone_coverage_geometry", expensive)
    result = bridge_composite_recombined(problem, baseline, max_pair_evaluations=1)
    assert result.points == baseline.points
    assert result.telemetry["bridge"]["evaluated_pairs"] == 1
    assert result.telemetry["bridge"]["rejections"]["nonpositive_mass_gain"] >= 1


def test_touching_pairs_are_considered_and_old_budget_envelope_holds():
    problem, baseline = _four_islands(separation=0)
    result = bridge_composite_recombined(problem, baseline)
    assert result.telemetry["bridge"]["touching_overlap_pairs"] >= 1
    assert len(result.points) <= len(baseline.points)
    for old in baseline.points:
        assert any(len(new.zones) <= len(old.zones)
                   and new.coverage.additional_mass_kg <= old.coverage.additional_mass_kg + 1e-6
                   for new in result.points)


def test_invalid_limits_are_not_silently_treated_as_fallback():
    problem, baseline = _four_islands()
    with pytest.raises(ValueError):
        bridge_composite_recombined(problem, baseline, time_limit_s=0)
