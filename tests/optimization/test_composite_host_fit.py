from dataclasses import replace

import pytest

from rebar.models import Axis, Direction, Layer
from rebar.optimization.contracts.problem import LayoutConstraints
from rebar.optimization.services.composite_detailing import build_composite_zone, evaluate_composite_zone
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.contracts.composite_coverage import COMPOSITE_COVERAGE_POLICY
from rebar.optimization.services.composite_host import evaluate_composite_host
from rebar.optimization.services.composite_host_fit import fit_composite_zone_to_host
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM

from test_composite_coverage import demand_sample, zone_sample
from test_composite_host import host_sample


def fitting_case(axis=Axis.X, layer=Layer.TOP):
    demand = replace(demand_sample(axis, required_level=1), direction=Direction(layer, axis))
    constraints = LayoutConstraints(allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM, cutting_profile="plate-11700")
    placement = zone_sample(demand, level=1).placement
    zone = build_composite_zone(demand, demand.bbox, 1, "fit", placement, constraints=constraints)
    outer = (-800, -200, 6000, 1200) if axis is Axis.X else (-200, -800, 1200, 6000)
    return demand, zone, constraints, host_sample(outer_mm=outer)


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
@pytest.mark.parametrize("layer", [Layer.TOP, Layer.BOTTOM])
def test_stock_surplus_moves_inward_without_weakening_anchorage_or_changing_metrics(axis, layer):
    demand, zone, constraints, host = fitting_case(axis, layer)
    assert evaluate_composite_host(demand, (zone,), host, constraints=constraints)["checks"]["planar_host_and_openings"] == "fail"
    fitted = fit_composite_zone_to_host(demand, zone, host, constraints=constraints)
    a, b = zone.components[0], fitted.components[0]
    assert a.longitudinal_interval_mm == (-975, 4875)
    assert b.longitudinal_interval_mm == (-775, 5075)
    assert b.installed_length_mm == a.installed_length_mm == 5850
    assert b.mass_kg == a.mass_kg and b.bar_count == a.bar_count
    assert b.placement == a.placement and fitted.demand_bbox == zone.demand_bbox
    assert b.longitudinal_interval_mm[0] <= -40 * b.rebar.diameter
    assert b.longitudinal_interval_mm[1] >= 3900 + 40 * b.rebar.diameter
    assert evaluate_composite_zone(demand, fitted, constraints=constraints).geometry_valid
    assert evaluate_composite_host(demand, (fitted,), host, constraints=constraints)["checks"]["planar_host_and_openings"] == "pass"
    before = evaluate_composite_coverage(demand, (zone,), constraints=constraints, policy_id=COMPOSITE_COVERAGE_POLICY)
    after = evaluate_composite_coverage(demand, (fitted,), constraints=constraints, policy_id=COMPOSITE_COVERAGE_POLICY)
    assert before == after


def test_opening_is_avoided_by_whole_set_translation_not_by_cutting():
    demand, zone, constraints, _ = fitting_case()
    host = host_sample(outer_mm=(-3000, -2000, 8000, 3000), openings_mm=((-1100, -100, -950, 1000),))
    fitted = fit_composite_zone_to_host(demand, zone, host, constraints=constraints)
    assert fitted.components[0].longitudinal_interval_mm == (-925, 4925)
    assert evaluate_composite_host(demand, (fitted,), host, constraints=constraints)["checks"]["planar_host_and_openings"] == "pass"


@pytest.mark.parametrize("obstacle", ["edge", "hole", "too_short"])
def test_fit_never_erases_demand_or_shortens_bars_to_force_success(obstacle):
    demand, zone, constraints, host = fitting_case()
    if obstacle == "edge":
        host = replace(host, outer_mm=(-700, -200, 6000, 1200))
    elif obstacle == "hole":
        host = replace(host, openings_mm=((500, 0, 800, 700),))
    else:
        zone = replace(zone, components=(replace(zone.components[0], longitudinal_interval_mm=(-700, 5150)),))
    with pytest.raises(ValueError):
        fit_composite_zone_to_host(demand, zone, host, constraints=constraints)
