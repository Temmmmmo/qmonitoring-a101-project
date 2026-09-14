from dataclasses import replace

import pytest

from rebar.legend import parse_recipe
from rebar.optimization.contracts.composite_coverage import COMPOSITE_COVERAGE_POLICY, STO_279_COVERAGE_POLICY
from rebar.optimization.contracts.placement import PeriodicAxisPattern
from rebar.optimization.services.axis_patterns import (
    a101_247_slab_recipe_placement, a101_sto_279_slab_recipe_placement, pattern_coordinates,
)
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone

from test_composite_coverage import demand_sample


@pytest.mark.parametrize("diameter", [20, 25, 28, 32, 36])
@pytest.mark.parametrize("side", ["left", "right"])
def test_sto_100_has_three_axes_per_period_and_real_diameter_contact(diameter, side):
    recipe = parse_recipe(f"s300d18+s100d{diameter}")
    placement = a101_sto_279_slab_recipe_placement(recipe, background_origin_mm=73, contact_side=side)
    values = pattern_coordinates(placement.additions[0], (73, 373 - 1e-6))
    assert len(values) == 3 and placement.additions[0].pattern.mean_spacing_mm == 100
    assert (173 in values) and (273 in values)
    background = (73, 373)
    assert min(abs(a - b) for a in values for b in background) == (18 + diameter) / 2
    gaps = [b - a for a, b in zip(values, (*values[1:], values[0] + 300))]
    assert sorted(gaps) == [100 - (18 + diameter) / 2, 100, 100 + (18 + diameter) / 2]
    assert placement.additions[0].axis_depth_from_face_mm is None
    with pytest.raises(ValueError):
        a101_247_slab_recipe_placement(recipe, background_origin_mm=73)


@pytest.mark.parametrize("recipe", ["s150d18+s100d25", "s300d18+s100d25+s300d18"])
def test_contact_profile_does_not_guess_other_backgrounds_or_combinations(recipe):
    with pytest.raises(ValueError):
        a101_sto_279_slab_recipe_placement(parse_recipe(recipe), background_origin_mm=0, contact_side="left")


def test_explicit_new_coverage_policy_rejects_uniform_100_and_old_policy_stays_closed():
    demand = demand_sample(required_level=1)
    recipe = parse_recipe("s300d18+s100d18")
    levels = tuple(replace(level, recipe=recipe, additional=recipe.additions[0]) if level.index == 1 else level for level in demand.levels)
    demand = replace(demand, levels=levels)
    placement = a101_sto_279_slab_recipe_placement(recipe, background_origin_mm=0, contact_side="left")
    zone = build_composite_zone(demand, (0, 0, 3900, 900), 1, "sto100", placement)
    check = evaluate_composite_coverage(demand, (zone,), policy_id=STO_279_COVERAGE_POLICY)
    assert check.geometry_and_patterns_valid and not check.placement_eligible
    assert not evaluate_composite_coverage(demand, (zone,), policy_id=COMPOSITE_COVERAGE_POLICY).geometry_and_patterns_valid
    uniform = replace(placement, additions=(replace(placement.additions[0], pattern=PeriodicAxisPattern.uniform(100)),))
    forged = build_composite_zone(demand, (0, 0, 3900, 900), 1, "not-sto100", uniform)
    assert not evaluate_composite_coverage(demand, (forged,), policy_id=STO_279_COVERAGE_POLICY).geometry_and_patterns_valid
