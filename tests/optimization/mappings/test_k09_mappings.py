"""Проверка явных таблиц двух дополнительных комплектов корпуса 2.9."""

from __future__ import annotations

import math

import pytest

from rebar import Rebar
from rebar.optimization import K09_ABOVE_3_D10, K09_MINUS_2_D12


def _area_cm2_per_m(rebar: Rebar) -> float:
    bar_area_mm2 = math.pi * rebar.diameter**2 / 4
    return bar_area_mm2 * (1000 / rebar.step) / 100


@pytest.mark.parametrize("mapping", [K09_ABOVE_3_D10, K09_MINUS_2_D12])
def test_k09_mapping_thresholds_match_total_rebar_capacity(mapping):
    for band in mapping.bands:
        capacity = _area_cm2_per_m(band.background)
        if band.additional is not None:
            capacity += _area_cm2_per_m(band.additional)
        assert band.threshold_as == pytest.approx(capacity, abs=0.05)


def test_minus_2_mapping_is_explicit_d12_profile():
    assert K09_MINUS_2_D12.expected_scale_bounds_as == (
        1.6,
        3.8,
        7.5,
        11.0,
        17.0,
        24.0,
        35.0,
    )
    assert [band.label for band in K09_MINUS_2_D12.bands] == [
        "s300d12",
        "s300d12+s300d12",
        "s300d12+s150d12",
        "s300d12+s150d16",
        "s300d12+s100d16",
        "s300d12+s100d20",
    ]


def test_above_3_mapping_is_explicit_d10_profile():
    assert K09_ABOVE_3_D10.expected_scale_bounds_as == (
        1.6,
        2.6,
        5.2,
        7.8,
        10.0,
        14.0,
        16.0,
        23.0,
    )
    assert [band.label for band in K09_ABOVE_3_D10.bands] == [
        "s300d10",
        "s300d10+s300d10",
        "s300d10+s150d10",
        "s300d10+s150d12",
        "s300d10+s100d12",
        "s300d10+s150d16",
        "s300d10+s100d16",
    ]
