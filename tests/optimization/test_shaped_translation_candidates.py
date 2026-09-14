from dataclasses import replace

import pytest
from shapely.geometry import box

from rebar.models import Axis, Direction, Layer
from rebar.optimization.algorithms.shaped_translation_candidates import straight_translation_candidates
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


def case(axis=Axis.X):
    material = box(0, 0, 5000, 5000)
    host = OrthogonalSolidHost((SolidHostSection(0, 200, material),), 40, 40, 25, material.area*200, 6)
    bar = PhysicalBar("one", Direction(Layer.TOP, axis), "A500", 10, 1000,
                      (-300, 2625), ("zone/0/0",))
    return bar, host


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_exact_run_translation_preserves_stock_and_full40d(axis):
    bar, host = case(axis)
    shape = box(500, 900, 1500, 1100) if axis is Axis.X else box(900, 500, 1100, 1500)
    candidates, truncated = straight_translation_candidates(bar, host, ((10, 300, shape),),
                                                           maximum_longitudinal_shift_mm=11700)
    assert candidates and not truncated
    for candidate in candidates:
        assert candidate.installed_length_mm == bar.installed_length_mm
        assert candidate.source_bar_ids == bar.source_bar_ids
        assert candidate.transverse_axis_mm == bar.transverse_axis_mm
        assert 25 <= candidate.installed_interval_mm[0] <= 100
        assert candidate.installed_interval_mm[1] <= 4975
    assert candidates[0].installed_interval_mm == (25, 2950)


def test_no_candidate_when_required_core_still_reaches_beyond_material():
    bar, host = case()
    candidates, truncated = straight_translation_candidates(bar, host, ((10, 300, box(100, 900, 1500, 1100)),),
                                                           maximum_longitudinal_shift_mm=11700)
    assert candidates == () and not truncated


def test_hole_runs_are_never_bridged_by_a_straight_candidate():
    bar, host = case()
    material = host.sections[0].footprint.difference(box(3100, 950, 3200, 1050))
    host = replace(host, sections=(SolidHostSection(0, 200, material),), volume_mm3=material.area*200)
    candidates, _ = straight_translation_candidates(bar, host, (), maximum_longitudinal_shift_mm=11700)
    assert candidates
    assert all(c.installed_interval_mm[1] <= 3075 for c in candidates)


def test_zero_opt_in_and_choice_budget_are_explicit():
    bar, host = case()
    assert straight_translation_candidates(bar, host, (), maximum_longitudinal_shift_mm=0) == ((), False)
    candidates, cut = straight_translation_candidates(bar, host, (),
        maximum_longitudinal_shift_mm=11700, maximum_positions=1)
    assert len(candidates) == 1 and cut


@pytest.mark.parametrize("limit", (True, -1, 11701, float("nan"), float("inf")))
def test_invalid_limit_rejected(limit):
    with pytest.raises(ValueError, match="bounded"):
        straight_translation_candidates(*case(), (), maximum_longitudinal_shift_mm=limit)
