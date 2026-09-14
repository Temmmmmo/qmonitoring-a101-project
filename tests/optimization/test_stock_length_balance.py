from dataclasses import replace
from itertools import product
from types import SimpleNamespace

import pytest

from rebar.optimization.algorithms.stock_length_balance import balance_stock_lengths
from rebar.optimization.services.bar_schedule import BarScheduleGroup, build_bar_schedule
from rebar.optimization.services.composite_detailing import build_composite_zone, evaluate_composite_zone
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.contracts.problem import LayoutConstraints
from rebar.optimization.services.stock_cutting import check_stock_cutting

from test_composite_reinforcement import resolved_synthetic


def groups(*pairs):
    return tuple(BarScheduleGroup(str(i), 18, length, count, "A500") for i, (length, count) in enumerate(pairs))


def test_balance_lengths_minimizes_actual_mass_and_never_invents_bars():
    source = groups((4875, 1), (5850, 1))
    result = balance_stock_lengths(source, maximum_mass_increase_pct=10, time_limit_s=5)
    assert result.status == "balanced"
    lengths = dict(result.installed_lengths_mm)
    assert sum(lengths.values()) == 11700
    assert all(lengths[g.source_id] >= g.installed_length_mm for g in source)
    assert result.telemetry["stock_cutting"]["status"] == "pass"
    assert result.telemetry["stock_cutting"]["physical_bar_count"] == 2
    assert result.balanced_mass_kg / result.original_mass_kg == pytest.approx(11700 / 10725)
    oracle = min(a + b for a, b in product(PLATE_11700_CUT_LENGTHS_MM, repeat=2)
                 if a >= 4875 and b >= 5850 and check_stock_cutting(build_bar_schedule(groups((a, 1), (b, 1))))["status"] == "pass")
    assert sum(lengths.values()) == oracle


def test_growth_cap_is_hard_not_a_reason_to_double_mass():
    result = balance_stock_lengths(groups((3900, 4)), maximum_mass_increase_pct=5, time_limit_s=5)
    assert result.status == "infeasible" and not result.installed_lengths_mm
    assert result.balanced_mass_kg is None
    allowed = balance_stock_lengths(groups((3900, 4)), maximum_mass_increase_pct=50, time_limit_s=5)
    assert allowed.status == "balanced" and dict(allowed.installed_lengths_mm)["0"] == 5850


def test_existing_exact_party_not_modified_and_unknown_class_not_assumed():
    source = groups((3900, 3))
    result = balance_stock_lengths(source, maximum_mass_increase_pct=0)
    assert result.status == "balanced" and result.balanced_mass_kg == result.original_mass_kg
    result = balance_stock_lengths((replace(source[0], steel_class=""),))
    assert result.status == "not_checked" and result.telemetry["reason"] == "steel_class_not_declared"


def test_new_position_limit_is_respected_after_length_changes():
    result = balance_stock_lengths(groups((4875, 1), (5850, 1)), maximum_positions=1,
                                  maximum_mass_increase_pct=10)
    assert result.status == "balanced"
    assert len(set(dict(result.installed_lengths_mm).values())) == 1
    assert list(dict(result.installed_lengths_mm).values()) == [5850, 5850]


def test_catalog_length_choice_requires_explicit_batch_policy_and_revalidates_geometry():
    demand, zone = resolved_synthetic()
    constraints = LayoutConstraints(allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM, cutting_profile="plate-11700-batch")
    changed = build_composite_zone(demand, zone.demand_bbox, zone.level_index, zone.id, zone.placement,
                                   constraints=constraints, installed_lengths_mm=(5850, 7800))
    check = evaluate_composite_zone(demand, changed, constraints=constraints)
    assert check.geometry_valid and check.physical_bar_count == 9
    assert changed.demand_bbox == zone.demand_bbox and changed.placement == zone.placement
    assert changed.components[1].longitudinal_interval_mm == (-1950, 5850)
    old_policy = replace(constraints, cutting_profile="plate-11700")
    assert not evaluate_composite_zone(demand, changed, constraints=old_policy).geometry_valid
    with pytest.raises(ValueError, match="профиль"):
        build_composite_zone(demand, zone.demand_bbox, 0, zone.id, zone.placement,
                             constraints=old_policy, installed_lengths_mm=(5850, 7800))
    for bad in ((5850,), (3900, 7800), (float("nan"), 7800), (5850, 8000)):
        with pytest.raises(ValueError):
            build_composite_zone(demand, zone.demand_bbox, 0, zone.id, zone.placement,
                                 constraints=constraints, installed_lengths_mm=bad)


@pytest.mark.parametrize("field,value", [("maximum_mass_increase_pct", -1), ("maximum_mass_increase_pct", True),
                                         ("maximum_positions", 0), ("time_limit_s", float("nan"))])
def test_invalid_balance_request_rejected(field, value):
    with pytest.raises(ValueError):
        balance_stock_lengths(groups((3900, 3)), **{field: value})


def test_incomplete_solver_is_not_given_a_cutting_certificate(monkeypatch):
    import scipy.optimize
    monkeypatch.setattr(scipy.optimize, "milp", lambda *a, **kw: SimpleNamespace(status=1, x=None))
    result = balance_stock_lengths(groups((3900, 4)))
    assert result.status == "not_checked" and result.balanced_mass_kg is None
