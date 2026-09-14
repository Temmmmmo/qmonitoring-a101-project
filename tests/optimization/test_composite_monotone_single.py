"""Opt-in diameter substitution is monotone in BOTH diameter and nominal density."""
from copy import deepcopy
from dataclasses import replace

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic
from rebar.legend import parse_recipe
from rebar.optimization import LayoutConstraints, PeriodicAxisPattern, build_demand_map
from rebar.optimization.contracts.composite_coverage import (
    MONOTONE_SINGLE_STO_COVERAGE_POLICY, STO_279_COVERAGE_POLICY,
)
from rebar.optimization.services.axis_patterns import a101_sto_279_slab_recipe_placement
from rebar.optimization.services.composite_coverage import (
    evaluate_composite_coverage, monotone_single_recipe_covers,
)
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_windows import covering_composite_window


def _demand(required="s300d18+s150d18", supplied="s300d18+s150d25", axis=Axis.X):
    recipes = [parse_recipe(label) for label in ("s300d18", required, supplied)]
    bands = [Band(i, i + 1, label, i * 20.0, recipe.background,
                  recipe.additions[0] if len(recipe.additions) == 1 else None, recipe)
             for i, (label, recipe) in enumerate(zip(("s300d18", required, supplied), recipes))]
    cells = []
    for y in (0, 400):
        points = [(0, y), (3900, y), (3900, y + 400), (0, y + 400)]
        if axis is Axis.Y:
            points = [(b, a) for a, b in points]
        cells.append(Cell(points, points[0], 2, bands[1]))
    bbox = (0, 0, 3900, 800) if axis is Axis.X else (0, 0, 800, 3900)
    return build_demand_map(Mosaic(Direction(Layer.TOP, axis), cells, bands, bbox))


def _zone(demand, *, level=2, bounds=None, identifier="strong", placement=None):
    recipe = demand.level(level).recipe
    axes = placement or a101_sto_279_slab_recipe_placement(recipe, background_origin_mm=0, contact_side="left")
    axes = replace(axes, additions=tuple(replace(a, origin_mm=100) if s.step == 300 else a
                                        for s, a in zip(recipe.additions, axes.additions)))
    selected = bounds or covering_composite_window(
        demand, demand.bbox, level, axes, constraints=LayoutConstraints(),
    )
    return build_composite_zone(demand, selected, level, identifier, axes)


def _evaluate(demand, zones):
    return evaluate_composite_coverage(demand, zones, policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY)


@pytest.mark.parametrize("required,supplied,expected", [
    ("s300d18+s150d18", "s300d18+s150d25", True),
    ("s300d18+s150d18", "s300d18+s100d18", True),
    ("s300d18+s150d18", "s300d18+s100d25", True),
    ("s300d18+s150d18", "s300d18+s150d18", True),
    ("s300d18+s100d18", "s300d18+s150d40", False),  # More As still cannot compensate coarser axes.
    ("s300d18+s300d25", "s300d18+s100d18", False),  # Denser does not compensate thinner bars.
    ("s300d18+s150d18", "s300d20+s150d25", False),
    ("s300d18+s150d18", "s200d18+s150d25", False),
    ("s300d18+s150d18", "s300d18+s150d25+s300d25", False),
    ("s300d18+s150d18+s300d25", "s300d18+s100d40", False),
    ("s300d18", "s300d18+s150d25", False),
])
def test_monotonicity_has_no_as_tradeoff_or_multicomponent_fallback(required, supplied, expected):
    assert monotone_single_recipe_covers(parse_recipe(required), parse_recipe(supplied)) is expected


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
@pytest.mark.parametrize("step", [100, 150])
def test_explicit_new_policy_accepts_no_thinner_no_sparser_while_old_policy_stays_strict(axis, step):
    demand = _demand(supplied=f"s300d18+s{step}d25", axis=axis)
    zone = _zone(demand)
    before = deepcopy((demand, zone))
    old = evaluate_composite_coverage(demand, (zone,), policy_id=STO_279_COVERAGE_POLICY)
    new = _evaluate(demand, (zone,))
    assert old.status == "fail" and old.uncovered_cell_count == 2
    assert new.status == "pass" and new.uncovered_cell_count == 0
    assert new.geometry_and_patterns_valid
    assert not new.placement_eligible
    assert "monotone-diameter-substitution-engineering-approval" in new.remaining_check_ids
    assert any("engineering approval is absent" in item for item in new.diagnostics)
    assert any("18, 25" in item for item in new.diagnostics)
    assert (demand, zone) == before


@pytest.mark.parametrize("required,supplied", [
    ("s300d18+s100d18", "s300d18+s150d40"),
    ("s300d18+s300d25", "s300d18+s100d18"),
])
def test_two_weak_sets_do_not_sum_into_coverage(required, supplied):
    demand = _demand(required, supplied)
    zones = (_zone(demand, identifier="one"), _zone(demand, identifier="two"))
    check = _evaluate(demand, zones)
    assert check.geometry_and_patterns_valid
    assert check.status == "fail" and check.uncovered_cell_count == 2
    assert check.uncovered_area_mm2 == 3900 * 800


@pytest.mark.parametrize("same_half", [False, True])
def test_geometric_union_requires_both_halves_without_duplicate_credit(same_half):
    demand = _demand()
    first = _zone(demand, bounds=(0, 0, 1950, 800), identifier="left")
    other = _zone(demand, bounds=(0, 0, 1950, 800) if same_half else (1950, 0, 3900, 800),
                  identifier="other")
    check = _evaluate(demand, (first, other))
    assert check.coverage_passed is not same_half
    assert check.uncovered_area_mm2 == (3900 * 400 if same_half else 0)


@pytest.mark.parametrize("step", [100, 150])
def test_uniform_pattern_forgery_is_rejected_for_both_nominal_steps(step):
    demand = _demand(supplied=f"s300d18+s{step}d25")
    placement = a101_sto_279_slab_recipe_placement(demand.level(2).recipe, background_origin_mm=0, contact_side="left")
    placement = replace(placement, additions=(replace(placement.additions[0], pattern=PeriodicAxisPattern.uniform(step)),))
    check = _evaluate(demand, (_zone(demand, placement=placement),))
    assert not check.geometry_and_patterns_valid
    assert check.uncovered_cell_count == 2


def test_nominally_higher_level_cannot_override_weaker_diameter():
    demand = _demand("s300d18+s150d25", "s300d18+s100d18")
    assert _zone(demand).level_index > demand.cells[0].level_index
    check = _evaluate(demand, (_zone(demand),))
    assert check.status == "fail" and check.uncovered_cell_count == 2


def test_multicomponent_source_rejected_even_if_current_cells_use_a_single_recipe():
    demand = _demand(supplied="s300d18+s150d25+s300d25")
    with pytest.raises(ValueError, match="exactly one"):
        _evaluate(demand, ())


def test_different_background_map_is_not_reinterpreted_by_new_policy():
    demand = _demand(supplied="s300d20+s150d25")
    with pytest.raises(ValueError, match="фоновые"):
        _evaluate(demand, ())
