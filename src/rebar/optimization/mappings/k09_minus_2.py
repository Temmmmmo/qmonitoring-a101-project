"""Явная таблица плиты корпуса 2.9 над минус вторым этажом."""

from rebar.models import Rebar
from rebar.standards import A101_242_PARKING_SLAB_T200_220

from .contracts import RebarBandMapping, RebarMapping

_BACKGROUND_D12_300 = Rebar(step=300, diameter=12)


K09_MINUS_2_D12 = RebarMapping(
    id="k09-minus-2-d12-v1",
    source=(
        "легенды PNG задания 2025.02.04_плита над минус 2 этажом; "
        "одинаковый порядок интервалов проверен в четырёх DXF"
    ),
    status="mvp_assumption",
    a101_profile_id=A101_242_PARKING_SLAB_T200_220.id,
    expected_scale_bounds_as=(1.6, 3.8, 7.5, 11.0, 17.0, 24.0, 35.0),
    bands=(
        RebarBandMapping(
            label="s300d12",
            threshold_as=3.77,
            background=_BACKGROUND_D12_300,
            additional=None,
        ),
        RebarBandMapping(
            label="s300d12+s300d12",
            threshold_as=7.54,
            background=_BACKGROUND_D12_300,
            additional=Rebar(step=300, diameter=12),
        ),
        RebarBandMapping(
            label="s300d12+s150d12",
            threshold_as=11.3,
            background=_BACKGROUND_D12_300,
            additional=Rebar(step=150, diameter=12),
        ),
        RebarBandMapping(
            label="s300d12+s150d16",
            threshold_as=17.2,
            background=_BACKGROUND_D12_300,
            additional=Rebar(step=150, diameter=16),
        ),
        RebarBandMapping(
            label="s300d12+s100d16",
            threshold_as=23.9,
            background=_BACKGROUND_D12_300,
            additional=Rebar(step=100, diameter=16),
        ),
        RebarBandMapping(
            label="s300d12+s100d20",
            threshold_as=35.2,
            background=_BACKGROUND_D12_300,
            additional=Rebar(step=100, diameter=20),
        ),
    ),
)
