"""Rebar auto-layout core (Qmonitoring × A101).

Публичный контракт данных — в models. Стадии пайплайна реализуются отдельными модулями:
legend, dxf_ingest, png_ingest (стадия A), далее segmentation/rectangularization/... .
"""

from .models import (
    Axis,
    Band,
    Cell,
    Direction,
    Layer,
    Mosaic,
    Point,
    Rebar,
    ReinforcementRecipe,
    UnsupportedReinforcementRecipeError,
)

__all__ = [
    "Axis",
    "Band",
    "Cell",
    "Direction",
    "Layer",
    "Mosaic",
    "Point",
    "Rebar",
    "ReinforcementRecipe",
    "UnsupportedReinforcementRecipeError",
]
