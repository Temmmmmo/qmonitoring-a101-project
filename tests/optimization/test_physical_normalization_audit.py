"""Independent adversarial checks of physical normalization, without case artifacts.

The oracle below deliberately does not call the normalizer's certificate,
collision, mass, or stock helpers. Fixtures contain arbitrary IDs and directions.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import math

import pytest

from rebar.models import Axis, Direction, Layer
from rebar.optimization.algorithms.physical_normalization import normalize_physical_bars
from rebar.optimization.contracts.physical import (
    PhysicalNormalizationConfig,
    PhysicalNormalizationLimitError,
    PhysicalSourceBar,
)

TOLERANCE = 1e-6
BX = Direction(Layer.BOTTOM, Axis.X)
BY = Direction(Layer.BOTTOM, Axis.Y)
TX = Direction(Layer.TOP, Axis.X)
TY = Direction(Layer.TOP, Axis.Y)


def _source(identifier, *, direction=BX, diameter=10, coordinate=100,
            required=(1000.0, 2000.0), installed=(-1000.0, 10700.0),
            steel="A500", background_diameter=10, background_origin=0.0):
    return PhysicalSourceBar(identifier, direction, steel, diameter, float(coordinate),
        installed, required, background_diameter, background_origin)


def _duplicates(*, axes=6, diameters=(10, 10), directions=(BX,), contact=False):
    return tuple(_source(f"arbitrary-owner-{axis}-{owner}", direction=direction, diameter=diameter,
                         coordinate=(10 if contact else 100) + axis * 300)
                 for direction in directions for axis in range(axes)
                 for owner, diameter in enumerate(diameters))


def _config(**kwargs):
    return PhysicalNormalizationConfig(time_limit_s=15, stock_balance_time_limit_s=2,
        maximum_exchange_attempts=20, **kwargs)


def _independent_certificate(source, result):
    originals = {(bar.direction, bar.id): bar for bar in source}
    assert len(originals) == len(source)
    output_ids = {(bar.direction, bar.id) for bar in result.bars}
    assert len(output_ids) == len(result.bars)
    assigned = Counter()
    for bar in result.bars:
        lower, upper = bar.installed_interval_mm
        assert math.isfinite(lower) and math.isfinite(upper)
        assert 0 < upper - lower <= 11700 + TOLERANCE
        assert bar.source_bar_ids
        for identifier in bar.source_bar_ids:
            key = (bar.direction, identifier)
            assigned[key] += 1
            original = originals[key]
            assert bar.steel_class == original.steel_class
            assert bar.diameter_mm >= original.diameter_mm
            assert abs(bar.transverse_axis_mm-original.transverse_axis_mm) <= TOLERANCE
            assert lower <= original.required_interval_mm[0] - 40 * bar.diameter_mm + TOLERANCE
            assert upper >= original.required_interval_mm[1] + 40 * bar.diameter_mm - TOLERANCE
            origin, step = original.background_origin_mm, original.background_step_mm
            nearest = origin + math.floor((original.transverse_axis_mm-origin)/step + 0.5) * step
            old_gap = abs(original.transverse_axis_mm-nearest) - (original.diameter_mm+original.background_diameter_mm)/2
            gap = abs(bar.transverse_axis_mm-nearest) - (bar.diameter_mm+original.background_diameter_mm)/2
            assert gap >= -TOLERANCE
            if abs(old_gap) <= TOLERANCE:
                assert abs(gap) <= TOLERANCE
    assert assigned == Counter({key: 1 for key in originals})
    expected_pairs = set()
    for index, left in enumerate(result.bars):
        for right in result.bars[index + 1:]:
            if left.direction != right.direction:
                continue
            overlap = min(left.installed_interval_mm[1], right.installed_interval_mm[1]) - max(
                left.installed_interval_mm[0], right.installed_interval_mm[0])
            gap = abs(left.transverse_axis_mm-right.transverse_axis_mm) - (left.diameter_mm+right.diameter_mm)/2
            if overlap > TOLERANCE and gap < -TOLERANCE:
                expected_pairs.add((left.direction, *sorted((left.id, right.id))))
    actual_pairs = {(pair.direction, *sorted((pair.first_bar_id, pair.second_bar_id)))
                    for pair in result.unresolved_pairs}
    assert len(actual_pairs) == len(result.unresolved_pairs)
    assert expected_pairs == actual_pairs
    assert result.metrics.body_intersection_pair_count == len(expected_pairs)
    assert result.metrics.physical_bar_count == len(result.bars)
    expected_mass = math.fsum(0.000006165 * bar.diameter_mm ** 2 *
                             (bar.installed_interval_mm[1]-bar.installed_interval_mm[0]) for bar in result.bars)
    assert result.metrics.mass_kg == pytest.approx(expected_mass, abs=1e-6)
    types = {(bar.steel_class, bar.diameter_mm, round(bar.installed_interval_mm[1]-bar.installed_interval_mm[0], 6))
             for bar in result.bars}
    assert result.metrics.position_count == len(types)
    assert result.placement_eligible is False
    assert result.actual_3d_checked is False
    assert result.host_checked is False


def test_absorption_preserves_every_source_but_does_not_preserve_redundant_stock_tails():
    source = _duplicates()
    before = tuple(source)
    result = normalize_physical_bars(source, config=_config())
    _independent_certificate(source, result)
    assert source == before
    assert len(result.bars) == 6
    assert all(len(bar.source_bar_ids) == 2 for bar in result.bars)
    assert not result.unresolved_pairs
    assert result.stock_report["status"] == "pass"
    assert any(bar.installed_interval_mm[0] > -1000 + TOLERANCE for bar in result.bars)


def test_diameter_substitution_requires_opt_in_and_full_new_diameter_anchorage():
    source = _duplicates(axes=5, diameters=(10, 12))
    conservative = normalize_physical_bars(source, config=_config())
    stronger = normalize_physical_bars(source, config=_config(allow_diameter_increase=True))
    _independent_certificate(source, conservative)
    _independent_certificate(source, stronger)
    assert all(len(bar.source_bar_ids) == 1 for bar in conservative.bars)
    assert len(stronger.bars) == 5
    assert all(bar.diameter_mm == 12 for bar in stronger.bars)
    assert all(bar.installed_interval_mm[0] <= 520 + TOLERANCE
               and bar.installed_interval_mm[1] >= 2480 - TOLERANCE for bar in stronger.bars)
    assert stronger.stock_report["status"] == "pass"


def test_equal_ids_in_different_directions_keep_separate_complete_ownership():
    source = _duplicates(directions=(BX, BY, TX, TY))
    result = normalize_physical_bars(source, config=_config())
    _independent_certificate(source, result)
    assert len(result.bars) == 24
    assert {bar.direction for bar in result.bars} == {BX, BY, TX, TY}
    assert result.stock_report["status"] == "pass"


def test_arbitrary_names_direction_and_negative_origin_do_not_require_known_case_ids():
    source = tuple(replace(bar, id="совсем-другой/" + bar.id, direction=TY,
                           transverse_axis_mm=bar.transverse_axis_mm - 900,
                           background_origin_mm=-900) for bar in _duplicates())
    result = normalize_physical_bars(source, config=_config())
    _independent_certificate(source, result)
    assert len(result.bars) == 6 and not result.unresolved_pairs


def test_touching_source_background_is_not_turned_into_a_collision():
    source = _duplicates(contact=True)
    result = normalize_physical_bars(source, config=_config(allow_diameter_increase=True))
    _independent_certificate(source, result)
    assert result.source_certificate["background_touch_source_count"] == 12
    assert result.source_certificate["background_contact_broken_count"] == 0


def test_different_steel_classes_are_never_absorbed_together():
    source = tuple(replace(bar, steel_class="A400" if bar.id.endswith("-1") else "A500") for bar in _duplicates())
    result = normalize_physical_bars(source, config=_config(allow_diameter_increase=True))
    _independent_certificate(source, result)
    assert all(len(bar.source_bar_ids) == 1 for bar in result.bars)
    assert len(result.bars) == 12


def test_collision_report_includes_neighboring_axes_not_just_exact_axis_duplicates():
    source = _duplicates(diameters=(20, 20)) + tuple(
        _source(f"neighbor-{index}", coordinate=114 + index * 300) for index in range(6))
    result = normalize_physical_bars(source, config=_config(allow_diameter_increase=True))
    _independent_certificate(source, result)
    assert any(pair.transverse_axis_distance_mm == pytest.approx(14)
               for pair in result.unresolved_pairs)


def test_long_required_chain_is_not_clipped_or_hidden_by_an_oversized_single_bar():
    source = (_source("first", required=(0.0, 6000.0), installed=(-400.0, 11300.0)),
              _source("last", required=(5000.0, 11000.0), installed=(-300.0, 11400.0)))
    result = normalize_physical_bars(source, config=_config(allow_diameter_increase=True))
    _independent_certificate(source, result)
    assert len(result.bars) == 2
    assert len(result.unresolved_pairs) == 1
    task = result.unresolved_pairs[0]
    assert task.single_merged_full40d_minimum_length_mm == pytest.approx(11800)
    assert task.exceeds_11700 is True
    assert task.engineering_resolution_required is True


def test_disabled_search_operators_return_a_complete_explicitly_unresolved_candidate():
    source = _duplicates()
    config = replace(_config(), maximum_merge_operations=0, maximum_exchange_attempts=0)
    result = normalize_physical_bars(source, config=config)
    _independent_certificate(source, result)
    assert len(result.bars) == len(source)
    assert len(result.unresolved_pairs) == 6


def test_verification_budget_does_not_turn_partial_pair_enumeration_into_a_pass():
    with pytest.raises(PhysicalNormalizationLimitError):
        normalize_physical_bars(_duplicates(), config=_config(maximum_pair_checks=1))


def test_search_budget_exhaustion_returns_the_complete_already_verified_incumbent():
    source = _duplicates()
    # Six initial pairs fit the mandatory complete audit; search needs further checks.
    result = normalize_physical_bars(source, config=_config(maximum_pair_checks=6))
    _independent_certificate(source, result)
    assert result.budget_exhausted is True
    assert result.status == "budget_exhausted"
    assert len(result.bars) == len(source)
    assert len(result.unresolved_pairs) == 6
    assert result.stock_report["status"] == "pass"
    assert any(row["stage"] == "search_budget_exhausted" and row["complete_verified_incumbent_retained"]
               for row in result.trace)


@pytest.mark.parametrize("change", ["duplicate", "nan_axis", "infinite_interval", "bool_diameter",
    "short_source_40d", "negative_length", "overstock_length", "empty_class", "background_penetration", "invalid_direction"])
def test_invalid_source_geometry_is_rejected_instead_of_repaired_without_evidence(change):
    source = list(_duplicates())
    if change == "duplicate":
        source[1] = source[0]
    elif change == "nan_axis":
        source[0] = replace(source[0], transverse_axis_mm=float("nan"))
    elif change == "infinite_interval":
        source[0] = replace(source[0], installed_interval_mm=(-1000, float("inf")))
    elif change == "bool_diameter":
        source[0] = replace(source[0], diameter_mm=True)
    elif change == "short_source_40d":
        source[0] = replace(source[0], installed_interval_mm=(601, 2400))
    elif change == "negative_length":
        source[0] = replace(source[0], installed_interval_mm=(3000, 1000))
    elif change == "overstock_length":
        source[0] = replace(source[0], installed_interval_mm=(-1000, 11000))
    elif change == "empty_class":
        source[0] = replace(source[0], steel_class=" ")
    elif change == "background_penetration":
        source[0] = replace(source[0], transverse_axis_mm=0)
    else:
        source[0] = replace(source[0], direction="bottom-X")
    with pytest.raises(ValueError):
        normalize_physical_bars(tuple(source), config=_config())


def test_reordered_input_produces_equivalent_verified_physical_result():
    source = _duplicates()
    first = normalize_physical_bars(source, config=_config())
    second = normalize_physical_bars(tuple(reversed(source)), config=_config())
    _independent_certificate(source, first)
    _independent_certificate(source, second)

    def geometry(result):
        return sorted((str(bar.direction), bar.steel_class, bar.diameter_mm, bar.transverse_axis_mm,
                       tuple(round(v, 6) for v in bar.installed_interval_mm), tuple(sorted(bar.source_bar_ids)))
                      for bar in result.bars)

    assert geometry(first) == geometry(second)
    assert first.metrics == second.metrics
