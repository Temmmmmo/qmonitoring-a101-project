"""Exact periodic axes, complete bar locators and no coplanar-to-3D promotion."""

from copy import deepcopy
from dataclasses import replace
from itertools import combinations
import math
import random

import pytest

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.placement import (
    AxisPlacement, CompositeLayoutZone, PatternedRebarSet, PeriodicAxisPattern, RecipePlacement,
)
from rebar.optimization.services.axis_patterns import pattern_coordinates
from rebar.optimization.services.patterned_conflicts import (
    PatternedConflictLimitError, check_patterned_same_plane_conflicts,
)


def _zone(name="zone", *, origins=(0.0,), diameters=(20,), window=(0.0, 300.0),
          interval=(0.0, 1000.0), pattern=None, step=300,
          direction=Direction(Layer.BOTTOM, Axis.X), depths=None):
    pattern = pattern or PeriodicAxisPattern.uniform(300)
    depths = depths if depths is not None else (None,) * len(origins)
    specs = tuple(Rebar(step, diameter) for diameter in diameters)
    axes = tuple(AxisPlacement(pattern, origin, depth) for origin, depth in zip(origins, depths, strict=True))
    recipe = ReinforcementRecipe(Rebar(300, 10), specs)
    placement = RecipePlacement(AxisPlacement(PeriodicAxisPattern.uniform(300), 0), axes, "explicit test")
    length = interval[1] - interval[0]
    components = tuple(PatternedRebarSet(index, spec, axis, window, interval,
        length, length, length, len(pattern_coordinates(axis, window)), 1.0)
        for index, (spec, axis) in enumerate(zip(specs, axes, strict=True)))
    return CompositeLayoutZone(name, direction, 1, (0, 0, 1000, 300), recipe, placement, components)


def _check(zones, **kwargs):
    return check_patterned_same_plane_conflicts(zones, assume_same_depth_per_direction=True, **kwargs)


def _pairs(report):
    return {(pair.first.direction, pair.first.zone_id, pair.first.component_index, pair.first.bar_index,
             pair.second.zone_id, pair.second.component_index, pair.second.bar_index)
            for pair in report.body_intersection_pairs}


def test_coincident_different_diameters_are_not_deduplicated_and_have_full_locators():
    first = _zone("first", diameters=(18,), interval=(0, 1000))
    second = _zone("second", diameters=(25,), interval=(800, 1800))
    original = deepcopy((first, second))
    report = _check((first, second))
    assert report.body_intersection_count == 2
    assert report.body_intersection_zone_pair_count == 1
    assert report.affected_bar_count == 4
    assert report.affected_zone_count == 2
    assert report.bars_checked == 4 and report.components_checked == 2
    for index, pair in enumerate(report.body_intersection_pairs):
        assert pair.first.zone_id == "first" and pair.second.zone_id == "second"
        assert pair.first.component_index == pair.second.component_index == 0
        assert pair.first.bar_index == pair.second.bar_index == index
        assert pair.first.transverse_coordinate_mm == pair.second.transverse_coordinate_mm == index * 300
        assert pair.longitudinal_overlap_interval_mm == (800, 1000)
        assert (pair.first.diameter_mm, pair.second.diameter_mm) == (18, 25)
        assert pair.transverse_axis_distance_mm == 0
        assert pair.transverse_surface_gap_mm == -21.5
    assert (first, second) == original
    assert not report.actual_3d_checked
    assert not report.placement_eligible


def test_periodic_100_200_axes_are_used_instead_of_nominal_uniform_150():
    periodic = _zone("periodic", window=(0, 600),
                     pattern=PeriodicAxisPattern(300, (100, 200)), step=150)
    regular = _zone("regular", origins=(100,), window=(0, 600))
    report = _check((periodic, regular))
    assert report.bars_checked == 6
    assert report.body_intersection_count == 2
    assert [pair.first.transverse_coordinate_mm for pair in report.body_intersection_pairs] == [100, 400]
    assert [pair.first.bar_index for pair in report.body_intersection_pairs] == [0, 2]


def test_components_within_same_zone_are_compared():
    zone = _zone(origins=(0, 0), diameters=(18, 25))
    report = _check((zone,))
    assert report.body_intersection_count == 2
    assert report.body_intersection_zone_pair_count == 1
    assert report.affected_zone_count == 1
    assert report.body_intersection_zone_pairs[0].first_zone_id == report.body_intersection_zone_pairs[0].second_zone_id
    assert all(pair.first.component_index == 0 and pair.second.component_index == 1
               for pair in report.body_intersection_pairs)


def test_intrinsic_too_thick_bars_in_one_component_are_also_reported():
    zone = _zone(diameters=(200,), pattern=PeriodicAxisPattern.uniform(100), step=100, window=(0, 200))
    report = _check((zone,))
    assert report.body_intersection_count == 2  # 0/100 and 100/200; 0/200 merely tangent.
    assert report.affected_bar_count == 3
    assert all(pair.first.component_index == pair.second.component_index == 0
               for pair in report.body_intersection_pairs)


@pytest.mark.parametrize("second_start,expected", [(1001, 0), (1000, 0), (1000 - 1e-12, 0), (1000 - 1e-4, 2), (999, 2)])
def test_flat_end_touch_or_separation_is_not_a_capsule_intersection(second_start, expected):
    first = _zone("first", interval=(0, 1000))
    second = _zone("second", interval=(second_start, 2000))
    report = _check((first, second), minimum_clear_spacing_mm=50)
    assert report.body_intersection_count == expected
    assert not report.longitudinal_end_clearance_checked


@pytest.mark.parametrize("delta,body", [(0, 0), (-1e-12, 0), (1e-12, 0), (-1e-4, 1), (1e-4, 0)])
def test_transverse_tangency_is_numerically_stable(delta, body):
    first = _zone("first", origins=(0.1,), diameters=(18,), window=(0, 100))
    second = _zone("second", origins=(0.1 + 21.5 + delta,), diameters=(25,), window=(0, 100))
    report = _check((first, second))
    assert report.body_intersection_count == body
    assert report.geometry_tolerance_mm == 1e-6


def test_clearance_only_count_is_separate_from_body_intersection_count():
    first = _zone("first", window=(0, 100))
    second = _zone("second", origins=(35,), window=(0, 100))
    plain = _check((first, second))
    clear = _check((first, second), minimum_clear_spacing_mm=20)
    assert plain.body_intersection_count == clear.body_intersection_count == 0
    assert plain.clearance_only_count == 0 and clear.clearance_only_count == 1
    assert clear.clearance_only_pairs[0].transverse_surface_gap_mm == 15
    assert clear.clearance_only_pairs[0].kind == "transverse_clearance_only"


def test_intended_background_contact_is_not_invented_as_an_additional_bar_conflict():
    # Background Ø10 at 0, additional Ø20 at 15: tangent but background is NOT input.
    zone = _zone(origins=(15,), window=(0, 100))
    report = _check((zone,))
    assert report.bars_checked == 1
    assert report.body_intersection_count == report.clearance_only_count == 0
    assert not report.background_checked


def test_different_directions_are_explicitly_outside_check_not_merged_by_zone_id():
    zones = tuple(_zone("same-id", direction=Direction(layer, axis))
                  for layer in Layer for axis in Axis)
    report = _check(zones)
    assert report.body_intersection_count == 0
    assert report.zones_checked == 4 and report.bars_checked == 8
    assert not report.cross_direction_checked
    assert not report.actual_3d_checked


def test_known_unequal_depths_cannot_be_silently_treated_as_coplanar():
    with pytest.raises(ValueError, match="unequal depths"):
        _check((_zone("first", depths=(30,)), _zone("second", depths=(50,))))
    report = _check((_zone("first", depths=(30,)), _zone("second", depths=(30,))))
    assert report.body_intersection_count == 2
    assert not report.actual_3d_checked


@pytest.mark.parametrize("assumption", [False, None, 1, "yes"])
def test_same_depth_assumption_must_be_explicit_true(assumption):
    with pytest.raises(ValueError, match="explicit"):
        check_patterned_same_plane_conflicts((_zone(),), assume_same_depth_per_direction=assumption)


@pytest.mark.parametrize("budget", [{"maximum_bars": 2}, {"maximum_pairs": 1}, {"maximum_pair_checks": 1}])
def test_resource_limit_raises_instead_of_returning_truncated_result(budget):
    zones = tuple(_zone(str(i), window=(0, 100)) for i in range(3))
    with pytest.raises(PatternedConflictLimitError):
        _check(zones, **budget)


def test_pair_check_budget_also_applies_when_candidate_pairs_have_no_intersections():
    zones = tuple(_zone(str(i), window=(0, 100), interval=(1000 * i, 1000 * i + 500)) for i in range(3))
    with pytest.raises(PatternedConflictLimitError, match="pair-check"):
        _check(zones, maximum_pair_checks=1)


def test_stored_bar_count_cannot_hide_extra_pattern_axes():
    zone = _zone()
    false = replace(zone, components=(replace(zone.components[0], bar_count=1),))
    with pytest.raises(ValueError, match="bar_count differs"):
        _check((false,))
    with pytest.raises(PatternedConflictLimitError):
        _check((false,), maximum_bars=1)


@pytest.mark.parametrize("change", ["duplicate_id", "missing_component", "component_index", "different_length", "unknown_origin"])
def test_malformed_geometry_or_identity_is_rejected(change):
    zone = _zone()
    zones = (zone,)
    if change == "duplicate_id":
        zones = (zone, zone)
    elif change == "missing_component":
        zones = (replace(zone, components=()),)
    elif change == "component_index":
        zones = (replace(zone, components=(replace(zone.components[0], component_index=1),)),)
    elif change == "different_length":
        zones = (replace(zone, components=(replace(zone.components[0], installed_length_mm=1),)),)
    else:
        placement = replace(zone.placement, additions=(replace(zone.placement.additions[0], origin_mm=None),))
        zones = (replace(zone, placement=placement,
                         components=(replace(zone.components[0], placement=placement.additions[0]),)),)
    with pytest.raises(ValueError):
        _check(zones)


@pytest.mark.parametrize("options", [
    {"maximum_bars": False}, {"maximum_bars": 0}, {"maximum_bars": 100001},
    {"maximum_pairs": 0}, {"maximum_pair_checks": 1.5},
    {"minimum_clear_spacing_mm": -1}, {"minimum_clear_spacing_mm": math.nan},
    {"minimum_clear_spacing_mm": math.inf}, {"minimum_clear_spacing_mm": True},
])
def test_invalid_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        _check((_zone(),), **options)


def test_empty_input_is_complete_but_not_a_placement_approval():
    result = _check(())
    assert result.bars_checked == result.body_intersection_count == 0
    assert not result.placement_eligible


def test_sweep_matches_independent_all_pairs_on_reproducible_mixed_patterns():
    rng = random.Random(2309)
    for _trial in range(30):
        zones = []
        for index in range(12):
            start = rng.randrange(-2000, 2000, 50)
            pattern = PeriodicAxisPattern(300, (100, 200)) if rng.randrange(2) else PeriodicAxisPattern.uniform(300)
            zones.append(_zone(str(index), origins=(rng.uniform(-100, 100),),
                diameters=(rng.choice((10, 12, 18, 20, 25)),), window=(-500, 1000),
                interval=(start, start + rng.randrange(1, 20) * 100), pattern=pattern,
                direction=Direction(rng.choice(tuple(Layer)), rng.choice(tuple(Axis)))))
        bars = []
        for zone in zones:
            component = zone.components[0]
            for index, coordinate in enumerate(pattern_coordinates(component.placement, component.axis_window_mm)):
                bars.append((zone.direction, zone.id, 0, index, coordinate,
                             component.longitudinal_interval_mm, component.rebar.diameter))
        expected = set()
        for first, second in combinations(bars, 2):
            if first[0] != second[0]:
                continue
            overlap = min(first[5][1], second[5][1]) - max(first[5][0], second[5][0])
            if overlap > 1e-6 and abs(first[4] - second[4]) + 1e-6 < (first[6] + second[6]) / 2:
                a, b = sorted((first, second), key=lambda item: (str(item[0]), *item[1:4]))
                expected.add((*a[:4], *b[1:4]))
        report = _check(tuple(zones))
        assert _pairs(report) == expected
        assert _check(tuple(reversed(zones))) == report
