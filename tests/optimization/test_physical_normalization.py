"""Focused bounds, stock failure, and individual normalization-operator regressions."""

from dataclasses import replace
import math

import pytest

from rebar.models import Axis, Direction, Layer
from rebar.optimization.algorithms import physical_normalization as service
from rebar.optimization.algorithms.stock_length_balance import StockLengthBalanceResult
from rebar.optimization.contracts.physical import (
    PhysicalNormalizationConfig, PhysicalSourceBar,
)
from rebar.optimization.services.stock_cutting import check_stock_cutting
from rebar.optimization.services.bar_schedule import build_bar_schedule

BX = Direction(Layer.BOTTOM, Axis.X)
TY = Direction(Layer.TOP, Axis.Y)


def _source(identifier, *, direction=BX, axis=100, diameter=10,
            required=(1000.0, 2000.0), interval=(-1000.0, 10700.0)):
    return PhysicalSourceBar(identifier, direction, "A500", diameter, axis, interval, required, 10, 0)


def _duplicates():
    return tuple(_source(f"owner-{axis}-{item}", axis=100 + axis * 300)
                 for axis in range(6) for item in range(2))


def test_empty_complete_source_does_not_become_placement_approval():
    result = service.normalize_physical_bars((), config=PhysicalNormalizationConfig())
    assert result.status == "unchanged"
    assert result.bars == () and result.metrics.physical_bar_count == 0
    assert result.stock_report["status"] == "pass"
    assert result.source_certificate["represented_source_bar_count"] == 0
    assert not result.placement_eligible and not result.actual_3d_checked


@pytest.mark.parametrize("options", [
    {"allow_diameter_increase": 1}, {"maximum_mass_kg": True}, {"maximum_mass_kg": math.nan},
    {"maximum_mass_kg": 0}, {"time_limit_s": 0}, {"time_limit_s": math.inf},
    {"stock_balance_time_limit_s": -1}, {"maximum_batch_mass_increase_pct": 5.01},
    {"maximum_batch_mass_increase_pct": -1}, {"maximum_bars": 0}, {"maximum_bars": 5001},
    {"maximum_pair_checks": False}, {"maximum_merge_operations": 1.5},
    {"maximum_exchange_attempts": 301},
])
def test_invalid_bounds_do_not_silently_relax_the_profile(options):
    with pytest.raises(ValueError):
        service.normalize_physical_bars(_duplicates(), config=PhysicalNormalizationConfig(**options))


def test_non_tuple_or_oversized_input_is_rejected_without_truncation():
    with pytest.raises(ValueError, match="bounded tuple"):
        service.normalize_physical_bars(list(_duplicates()), config=PhysicalNormalizationConfig())
    with pytest.raises(ValueError, match="bounded tuple"):
        service.normalize_physical_bars(_duplicates(), config=PhysicalNormalizationConfig(maximum_bars=2))


@pytest.mark.parametrize("change", [{"background_step_mm": 150}, {"background_step_mm": True},
                                   {"background_origin_mm": None}, {"required_interval_mm": (1, math.inf)},
                                   {"installed_interval_mm": [0, 11700]}])
def test_unknown_background_or_malformed_interval_is_explicitly_rejected(change):
    with pytest.raises(ValueError):
        service.normalize_physical_bars((replace(_source("bar"), **change),), config=PhysicalNormalizationConfig())


def test_wall_budget_retains_complete_verified_source_stock_without_search(monkeypatch):
    times = iter((0.0, 100.0))
    monkeypatch.setattr(service, "perf_counter", lambda: next(times, 100.0))
    source = _duplicates()
    result = service.normalize_physical_bars(source, config=PhysicalNormalizationConfig(time_limit_s=1))
    assert result.status == "budget_exhausted" and result.budget_exhausted
    assert result.metrics.physical_bar_count == len(source)
    assert result.metrics.body_intersection_pair_count == 6
    assert result.stock_report["status"] == "pass"
    assert {(b.direction, i) for b in result.bars for i in b.source_bar_ids} == {(b.direction, b.id) for b in source}
    assert all(len(b.source_bar_ids) == 1 for b in result.bars)


def test_unsolved_new_stock_branch_retains_full_checked_incumbent(monkeypatch):
    def unavailable(*args, **kwargs):
        return StockLengthBalanceResult("not_checked", (), 0, None, {"reason": "solver unavailable"})
    monkeypatch.setattr(service, "balance_stock_lengths", unavailable)
    source = _duplicates()
    result = service.normalize_physical_bars(source, config=PhysicalNormalizationConfig())
    assert result.status == "unchanged"
    assert result.stock_report["status"] == "pass"
    assert len(result.bars) == len(source)
    assert any(row["stage"] == "stock_balance" and row["status"] == "not_checked" for row in result.trace)


def test_unrelated_solver_error_is_not_disguised_as_a_successful_fallback(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("not a feasibility failure")
    monkeypatch.setattr(service, "balance_stock_lengths", broken)
    with pytest.raises(RuntimeError, match="not a feasibility failure"):
        service.normalize_physical_bars(_duplicates(), config=PhysicalNormalizationConfig())


def test_whole_unions_use_cumulative_mass_cap_across_separate_axes():
    source = tuple(bar for axis in (100, 400) for bar in (
        _source(f"small-{axis}", axis=axis, required=(0.0, 1100.0), interval=(-425.0, 1525.0)),
        _source(f"large-{axis}", axis=axis, diameter=12, required=(1000.0, 2000.0), interval=(330.0, 2670.0))))
    config = PhysicalNormalizationConfig(allow_diameter_increase=True)
    originals, bars = service._source_input(source, config)
    one = service._make_bar(bars[:2], originals, "new", config)
    increase = service._mass((one,)) - service._mass(bars[:2])
    assert increase > 0
    cap = service._mass(bars) + 1.5 * increase
    result = service._whole_merge(bars, originals, config, service._Budget(config), cap, [])
    assert len(result) == 3  # one individually admissible enlargement; not both
    assert service._mass(result) <= cap + 1e-6
    service._certificate(result, originals)


def test_enlarged_union_cannot_worsen_two_neighboring_axis_conflicts():
    source = (
        _source("thick", diameter=20, required=(0.0, 1000.0), interval=(-962.5, 1962.5)),
        _source("longer-thin", required=(1000.0, 2500.0), interval=(580.0, 2920.0)),
        _source("neighbor-left", axis=86, required=(3000.0, 3100.0), interval=(2465.0, 3635.0)),
        _source("neighbor-right", axis=114, required=(3000.0, 3100.0), interval=(2465.0, 3635.0)),
    )
    config = PhysicalNormalizationConfig(allow_diameter_increase=True, maximum_mass_kg=100)
    originals, bars = service._source_input(source, config)
    assert len(service._conflicts(bars)) == 1
    result = service._whole_merge(bars, originals, config, service._Budget(config), 100, [])
    assert len(result) == 4
    assert len(service._conflicts(result)) == 1


def test_generic_length_exchange_has_no_case_direction_or_thirteen_pair_hardcoding():
    # Only ONE initial conflict. The same identifier in a different Direction is
    # a legitimate distinct donor, while exchanging a bar with itself is not.
    sources = (
        _source("unrelated-name", direction=BX, required=(3500.0, 6500.0), interval=(0.0, 9750.0)),
        _source("left", direction=BX, required=(1500.0, 1800.0), interval=(1000.0, 2300.0)),
        _source("right", direction=BX, required=(10500.0, 10800.0), interval=(10100.0, 11400.0)),
        _source("unrelated-name", direction=TY, required=(0.0, 1000.0), interval=(-3000.0, 4800.0)),
        _source("cut-fill-1950", direction=TY, axis=400, required=(1000.0, 2000.0), interval=(525.0, 2475.0)),
        _source("cut-fill-1300", direction=TY, axis=700, required=(1500.0, 1800.0), interval=(1000.0, 2300.0)),
    )
    config = PhysicalNormalizationConfig()
    originals, bars = service._source_input(sources, config)
    groups, _ = service._group_schedule(bars)
    stock = check_stock_cutting(build_bar_schedule(groups), time_limit_s=1)
    assert stock["status"] == "pass" and len(service._conflicts(bars)) == 1
    trace = []
    result, after_stock = service._exchange(bars, originals, config, service._Budget(config), stock, trace)
    assert len(service._conflicts(result)) == 0
    assert service._group_schedule(result)[0] == groups
    assert service._mass(result) == pytest.approx(service._mass(bars), abs=1e-6)
    assert after_stock["status"] == "pass"
    service._certificate(result, originals)
    move = next(row for row in trace if row["stage"] == "mass_neutral_length_exchange")
    assert move["target"][0] == str(BX)
    assert move["donor"][0] == str(TY)
    assert move["old_lengths_mm"] == [9750, 7800]
    assert move["new_lengths_mm"] == [7800, 9750]
