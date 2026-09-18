"""Явная таблица фундаментной плиты корпуса 2.9 (К09).

Шкала извлечена из PNG-легенды:
"Площадь_полной_арматуры_на_1пм_по_оси_X_у_верхней_грани.png"

Фон: Ø18@300
Добавки: @300, @150, @100 с диаметрами 18/20/25
"""

from rebar.models import Rebar
from rebar.standards import A101_242_PARKING_SLAB_T200_220

from .contracts import RebarBandMapping, RebarMapping

_BACKGROUND_D18_300 = Rebar(step=300, diameter=18)


K09_FOUNDATION_D18 = RebarMapping(
    id="k09-foundation-d18-v1",
    source=(
        "PNG-легенда задания 2024.12.17_фундаментная плита, "
        "ось X, верхняя грань; пороги 7.19/8.48/17/25.5/29.4/39.9"
    ),
    status="mvp_assumption",
    a101_profile_id=A101_242_PARKING_SLAB_T200_220.id,
    expected_scale_bounds_as=(7.19, 8.48, 17.0, 25.5, 29.4, 39.9, 57.6),
    bands=(
        RebarBandMapping(
            label="s300d18",
            threshold_as=7.19,
            background=_BACKGROUND_D18_300,
            additional=None,
        ),
        RebarBandMapping(
            label="s300d18+s300d18",
            threshold_as=8.48,
            background=_BACKGROUND_D18_300,
            additional=Rebar(step=300, diameter=18),
        ),
        RebarBandMapping(
            label="s300d18+s150d18",
            threshold_as=17.0,
            background=_BACKGROUND_D18_300,
            additional=Rebar(step=150, diameter=18),
        ),
        RebarBandMapping(
            label="s300d18+s100d20",
            threshold_as=25.5,
            background=_BACKGROUND_D18_300,
            additional=Rebar(step=100, diameter=20),
        ),
        RebarBandMapping(
            label="s300d18+s100d25",
            threshold_as=29.4,
            background=_BACKGROUND_D18_300,
            additional=Rebar(step=100, diameter=25),
        ),
        RebarBandMapping(
            label="s300d18+s100d25",
            threshold_as=39.9,
            background=_BACKGROUND_D18_300,
            additional=Rebar(step=100, diameter=25),
        ),
    ),
)
