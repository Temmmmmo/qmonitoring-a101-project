"""Проверка явно зафиксированной таблицы плиты нуля."""

from __future__ import annotations

import math

import pytest

from rebar import Rebar
from rebar.optimization import PLATE_ZERO_D12


def _area_cm2_per_m(rebar: Rebar) -> float:
    bar_area_mm2 = math.pi * rebar.diameter**2 / 4
    return bar_area_mm2 * (1000 / rebar.step) / 100


def test_plate_zero_mapping_assignments_are_explicit_and_ordered():
    assert PLATE_ZERO_D12.id == "plate-zero-d12-v1"
    assert PLATE_ZERO_D12.status == "mvp_assumption"
    assert PLATE_ZERO_D12.expected_scale_bounds_as == (
        1.9,
        3.8,
        7.5,
        11.0,
        17.0,
        24.0,
        35.0,
    )
    assert [band.label for band in PLATE_ZERO_D12.bands] == [
        "s300d12",
        "s300d12+s300d12",
        "s300d12+s150d12",
        "s300d12+s150d16",
        "s300d12+s100d16",
        "s300d12+s100d20",
    ]
    assert [band.additional for band in PLATE_ZERO_D12.bands] == [
        None,
        Rebar(step=300, diameter=12),
        Rebar(step=150, diameter=12),
        Rebar(step=150, diameter=16),
        Rebar(step=100, diameter=16),
        Rebar(step=100, diameter=20),
    ]


def test_plate_zero_thresholds_match_total_rebar_capacity():
    for band in PLATE_ZERO_D12.bands:
        capacity = _area_cm2_per_m(band.background)
        if band.additional is not None:
            capacity += _area_cm2_per_m(band.additional)
        assert band.threshold_as == pytest.approx(capacity, abs=0.05)
