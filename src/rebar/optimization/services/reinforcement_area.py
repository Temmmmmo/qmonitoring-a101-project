"""Средняя площадь стали на метр; не проверка анкеровки или дискретных осей."""
import math

from rebar.models import Rebar, ReinforcementRecipe


def rebar_area_cm2_m(spec: Rebar) -> float:
    if (isinstance(spec.diameter, bool) or not isinstance(spec.diameter, int)
            or spec.diameter <= 0 or isinstance(spec.step, bool)
            or not isinstance(spec.step, int) or spec.step <= 0):
        raise ValueError("требуются положительные целые диаметр и шаг")
    return math.pi * spec.diameter**2 / 4 * (1000 / spec.step) / 100


def recipe_area_cm2_m(recipe: ReinforcementRecipe) -> float:
    return math.fsum(rebar_area_cm2_m(spec) for spec in (recipe.background, *recipe.additions))
