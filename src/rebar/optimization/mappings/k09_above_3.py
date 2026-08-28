"""Явная таблица плиты корпуса 2.9 над третьим этажом."""

from rebar.models import Rebar

from .contracts import RebarBandMapping, RebarMapping

_BACKGROUND_D10_300 = Rebar(step=300, diameter=10)


K09_ABOVE_3_D10 = RebarMapping(
    id="k09-above-3-d10-v1",
    source=(
        "легенды PNG задания 2025.08.13_плиты перекрытия над 3-14 этажами, "
        "плита над 3 этажом; одинаковый порядок интервалов проверен в четырёх DXF"
    ),
    status="mvp_assumption",
    expected_scale_bounds_as=(1.6, 2.6, 5.2, 7.8, 10.0, 14.0, 16.0, 23.0),
    bands=(
        RebarBandMapping(
            label="s300d10",
            threshold_as=2.62,
            background=_BACKGROUND_D10_300,
            additional=None,
        ),
        RebarBandMapping(
            label="s300d10+s300d10",
            threshold_as=5.23,
            background=_BACKGROUND_D10_300,
            additional=Rebar(step=300, diameter=10),
        ),
        RebarBandMapping(
            label="s300d10+s150d10",
            threshold_as=7.85,
            background=_BACKGROUND_D10_300,
            additional=Rebar(step=150, diameter=10),
        ),
        RebarBandMapping(
            label="s300d10+s150d12",
            threshold_as=10.2,
            background=_BACKGROUND_D10_300,
            additional=Rebar(step=150, diameter=12),
        ),
        RebarBandMapping(
            label="s300d10+s100d12",
            threshold_as=13.9,
            background=_BACKGROUND_D10_300,
            additional=Rebar(step=100, diameter=12),
        ),
        RebarBandMapping(
            label="s300d10+s150d16",
            threshold_as=16.0,
            background=_BACKGROUND_D10_300,
            additional=Rebar(step=150, diameter=16),
        ),
        RebarBandMapping(
            label="s300d10+s100d16",
            threshold_as=22.7,
            background=_BACKGROUND_D10_300,
            additional=Rebar(step=100, diameter=16),
        ),
    ),
)
