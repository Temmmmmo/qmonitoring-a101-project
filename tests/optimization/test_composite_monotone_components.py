"""Guardrails for the opt-in ordered-component zone experiment."""
from dataclasses import replace

import pytest

from rebar.legend import parse_recipe
from rebar.models import Axis, Direction, Layer
from rebar.optimization.contracts.composite_coverage import (
    MONOTONE_COMPONENT_STO_COVERAGE_POLICY, STO_279_COVERAGE_POLICY,
)
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutConstraints
from rebar.optimization.services.axis_patterns import a101_247_slab_recipe_placement
from rebar.optimization.services.composite_coverage import (
    evaluate_composite_coverage, monotone_component_recipe_covers,
)
from rebar.optimization.services.composite_detailing import build_composite_zone, prepare_composite_detailing
from rebar.optimization.services.composite_windows import covering_composite_window


REQUIRED = "s300d18+s150d18+s300d25"
STRONGER = "s300d18+s150d25+s300d32"
CONSTRAINTS = LayoutConstraints(min_width_cells=1)


def _case(*labels, axis=Axis.X):
    recipes = tuple(parse_recipe(label) for label in ("s300d18", REQUIRED, *labels))
    levels = tuple(DemandLevel(i, i, None, None, str(i),
                   recipe.additions[0] if len(recipe.additions) == 1 else None,
                   bool(recipe.additions), recipe) for i, recipe in enumerate(recipes))
    points = ((0, 0), (3900, 0), (3900, 800), (0, 800))
    centroid = (1950, 400)
    bounds = (0, 0, 3900, 800)
    if axis is Axis.Y:
        points = tuple((y, x) for x, y in points)
        centroid = centroid[::-1]
        bounds = (0, 0, 800, 3900)
    cell = DemandCell(7, points, centroid, 1, 1)
    return DemandMap(Direction(Layer.TOP, axis), levels, (cell,), bounds)


def _zone(demand, index, identifier):
    recipe = demand.level(index).recipe
    placement = a101_247_slab_recipe_placement(recipe, background_origin_mm=0)
    placement = replace(placement, additions=tuple(
        replace(axes, origin_mm=0 if spec.step == 150 else 50)
        for spec, axes in zip(recipe.additions, placement.additions)))
    return build_composite_zone(demand, demand.bbox, index, identifier, placement, constraints=CONSTRAINTS)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_stronger_each_ordered_component_passes_opt_in_but_old_policy_rejects(axis):
    demand = _case(STRONGER, axis=axis)
    zone = _zone(demand, 2, "strong")
    old = evaluate_composite_coverage(demand, (zone,), policy_id=STO_279_COVERAGE_POLICY,
                                      constraints=CONSTRAINTS)
    new = evaluate_composite_coverage(demand, (zone,), policy_id=MONOTONE_COMPONENT_STO_COVERAGE_POLICY,
                                      constraints=CONSTRAINTS)
    assert old.status == "fail" and old.uncovered_cell_count == 1
    assert new.status == "pass" and new.covered_cell_count == 1
    assert new.additional_mass_kg > 0 and not new.placement_eligible
    assert "monotone-component-substitution-engineering-approval" in new.remaining_check_ids


@pytest.mark.parametrize("supplied", (
    "s300d18+s100d40",                 # A heavier single set cannot replace two sets.
    "s300d18+s300d25+s300d32",        # More diameter cannot compensate s300 for s150.
    "s300d18+s150d25+s300d18",        # Second component is too thin.
    "s300d20+s150d25+s300d32",        # Different background is outside this policy.
))
def test_no_component_drop_spacing_tradeoff_or_background_change(supplied):
    assert not monotone_component_recipe_covers(parse_recipe(REQUIRED), parse_recipe(supplied))


def test_two_individually_insufficient_zones_cannot_sum_their_components():
    demand = _case("s300d18+s150d18+s300d18", "s300d18+s300d12+s300d25")
    zones = (_zone(demand, 2, "weak-second"), _zone(demand, 3, "weak-first"))
    checked = evaluate_composite_coverage(demand, zones, policy_id=MONOTONE_COMPONENT_STO_COVERAGE_POLICY,
                                          constraints=CONSTRAINTS)
    assert checked.geometry_and_patterns_valid
    assert checked.status == "fail" and checked.uncovered_cell_count == 1
    assert checked.uncovered_area_mm2 == 3900 * 800
    assert checked.additional_mass_kg > 0  # Steel exists, but the full recipe is not supplied by either zone.


def test_window_rejects_context_from_a_different_demand_even_when_values_match():
    demand = _case(STRONGER)
    equivalent = _case(STRONGER)
    placement = _zone(demand, 2, "valid").placement
    with pytest.raises(ValueError, match="контекст детализации"):
        covering_composite_window(demand, demand.bbox, 2, placement,
            constraints=CONSTRAINTS, context=prepare_composite_detailing(equivalent))
