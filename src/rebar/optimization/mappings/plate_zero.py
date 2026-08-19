"""Явная MVP-таблица плиты нуля корпуса 2.9."""

from rebar.models import Rebar

from .contracts import RebarBandMapping, RebarMapping

_BACKGROUND_D12_300 = Rebar(step=300, diameter=12)


PLATE_ZERO_D12 = RebarMapping(
    id="plate-zero-d12-v1",
    source=(
        "легенды PNG задания 2025.02.19_плита нуля; одинаковый порядок интервалов "
        "проверен в четырёх DXF"
    ),
    status="mvp_assumption",
    # Числа ниже записаны в ATTRIB четырёх DXF. Они округлены сильнее, чем
    # подписи границ на PNG: например, 3.8 вместо 3.77 и 35.0 вместо 35.2.
    expected_scale_bounds_as=(1.9, 3.8, 7.5, 11.0, 17.0, 24.0, 35.0),
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
