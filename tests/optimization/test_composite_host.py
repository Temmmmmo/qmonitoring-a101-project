from dataclasses import replace

import pytest

from rebar.models import Axis, Direction, Layer
from rebar.optimization.contracts.host import RectangularHostEnvelope
from rebar.optimization.contracts.problem import LayoutConstraints
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_host import evaluate_composite_host, evaluate_host_demand_feasibility

from test_composite_coverage import demand_sample, zone_sample


def host_sample(**kwargs):
    return RectangularHostEnvelope(**{"outer_mm": (-2000, -2000, 10000, 10000), "openings_mm": (),
        "bottom_z_mm": -300, "top_z_mm": 0, "top_cover_mm": 25, "bottom_cover_mm": 25,
        "side_cover_mm": 25, "source": "synthetic explicit prism", **kwargs})


def depth_zone(demand, *, depths=(34, 65), name="Z", phase=50):
    zone = zone_sample(demand, name=name, phase25=phase)
    placement = replace(zone.placement, additions=tuple(replace(p, axis_depth_from_face_mm=d)
                        for p, d in zip(zone.placement.additions, depths)))
    return build_composite_zone(demand, zone.demand_bbox, 2, name, placement)


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
@pytest.mark.parametrize("layer", [Layer.TOP, Layer.BOTTOM])
def test_all_axes_and_components_use_explicit_host_face_depths(axis, layer):
    demand = replace(demand_sample(axis), direction=Direction(layer, axis))
    result = evaluate_composite_host(demand, (depth_zone(demand),), host_sample())
    assert result["physical_bar_count"] == 9 and result["invalid_bar_count"] == 0
    assert result["checks"]["planar_host_and_openings"] == result["checks"]["top_bottom_cover"] == "pass"
    assert result["checks"]["additional_bar_collisions"] == "pass"
    assert result["status"] == "needs_external_checks" and not result["placement_eligible"]
    assert result["checks"]["live_host_geometry"] == "not_checked"


def test_unknown_depth_is_not_silently_placed_on_cover():
    demand = demand_sample()
    result = evaluate_composite_host(demand, (zone_sample(demand),), host_sample())
    assert result["checks"]["planar_host_and_openings"] == "pass"
    assert result["checks"]["top_bottom_cover"] == result["checks"]["additional_bar_collisions"] == "not_checked"
    assert len(result["unknown_depth_components"]) == 2


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_anchorage_that_exits_host_is_not_trimmed_or_counted_as_valid(axis):
    demand = demand_sample(axis)
    zone = depth_zone(demand)
    before = zone
    result = evaluate_composite_host(demand, (zone,), host_sample(outer_mm=demand.bbox))
    assert result["invalid_bar_count"] == 9 and result["status"] == "blocked"
    assert all("host-side-cover" in row["check_ids"] for row in result["invalid_bars"])
    assert zone == before and zone.components[1].installed_length_mm == 5900


@pytest.mark.parametrize("depths", [(25, 65), (34, 290), (290, 65)])
def test_both_covers_include_the_radius(depths):
    demand = demand_sample()
    result = evaluate_composite_host(demand, (depth_zone(demand, depths=depths),), host_sample())
    assert result["checks"]["top_bottom_cover"] == "fail" and result["status"] == "blocked"


@pytest.mark.parametrize("hole,blocked", [((0, 80, 100, 110), True), ((0, 125, 100, 135), True),
                                        ((0, 235, 100, 240), False), ((8000, 100, 9000, 1000), False)])
def test_opening_clearance_is_checked_on_physical_bars_not_zone_bbox(hole, blocked):
    demand = demand_sample()
    result = evaluate_composite_host(demand, (depth_zone(demand),), host_sample(openings_mm=(hole,)))
    assert (result["checks"]["planar_host_and_openings"] == "fail") is blocked
    if blocked:
        assert any("opening-cover" in r["check_ids"] for r in result["invalid_bars"])


def test_same_direction_overlap_and_coincident_bars_are_reported_separately():
    demand = demand_sample()
    result = evaluate_composite_host(demand, (depth_zone(demand, name="A"), depth_zone(demand, name="B")), host_sample())
    assert result["checks"]["same_direction_zone_overlap"] == result["checks"]["additional_bar_collisions"] == "fail"
    assert len(result["additional_bar_conflicts"]) == 9


@pytest.mark.parametrize("separate", [False, True])
def test_height_separation_controls_two_component_collision(separate):
    demand = demand_sample()
    zone = depth_zone(demand, phase=100, depths=(34, 65 if separate else 37.5))
    result = evaluate_composite_host(demand, (zone,), host_sample())
    assert result["checks"]["additional_bar_collisions"] == ("pass" if separate else "fail")


def test_clear_spacing_and_pair_resource_limit_are_not_ignored(monkeypatch):
    demand = demand_sample()
    zone = depth_zone(demand, phase=100)
    result = evaluate_composite_host(demand, (zone,), host_sample(), constraints=LayoutConstraints(minimum_clear_spacing_mm=20))
    assert result["checks"]["additional_bar_collisions"] == "fail"
    monkeypatch.setattr("rebar.optimization.services.composite_host.MAX_PAIR_CHECKS", 1)
    result = evaluate_composite_host(demand, (zone,), host_sample())
    assert result["checks"]["additional_bar_collisions"] == "not_checked"


@pytest.mark.parametrize("bad", ["count", "direction", "duplicate", "bar_limit"])
def test_host_preflight_never_trusts_forged_detailing(bad, monkeypatch):
    demand = demand_sample()
    zone = depth_zone(demand)
    zones = (zone,)
    if bad == "count":
        zones = (replace(zone, components=(replace(zone.components[0], bar_count=0), zone.components[1])),)
    elif bad == "direction":
        zones = (replace(zone, direction=Direction(Layer.TOP, Axis.Y)),)
    elif bad == "duplicate":
        zones = (zone, zone)
    else:
        monkeypatch.setattr("rebar.optimization.services.composite_host.MAX_BARS", 1)
    with pytest.raises(ValueError):
        evaluate_composite_host(demand, zones, host_sample())


@pytest.mark.parametrize("kwargs", [{"top_z_mm": float("nan")}, {"top_cover_mm": -1},
                                    {"bottom_z_mm": 100}, {"outer_mm": (0, 0, 0, 100)},
                                    {"side_cover_mm": True}, {"openings_mm": ((-2000, 0, 100, 100),)},
                                    {"source": ""}])
def test_host_contract_rejects_unknown_or_impossible_geometry(kwargs):
    with pytest.raises(ValueError):
        host_sample(**kwargs)


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
@pytest.mark.parametrize("required,extension", [(1, 720), (2, 1000), (3, 1000)])
def test_boundary_incompatibility_is_independent_of_search_and_phase(axis, required, extension):
    demand = demand_sample(axis, required_level=required)
    result = evaluate_host_demand_feasibility(demand, host_sample(outer_mm=demand.bbox))
    assert result["requires_engineering_decision"] and result["cell_count"] == 2
    assert result["unavoidable_uncovered_area_mm2"] == pytest.approx((extension + 25) * 2 * 800)
    assert all(c["minimum_anchorage_each_end_mm"] == extension for c in result["cells"])


def test_no_boundary_contradiction_is_not_a_placement_permission():
    result = evaluate_host_demand_feasibility(demand_sample(), host_sample())
    assert result["status"] == "necessary_condition_passed" and not result["placement_eligible"]
    assert result["cell_count"] == 0


def test_too_short_host_does_not_produce_negative_coverage():
    demand = demand_sample()
    result = evaluate_host_demand_feasibility(demand, host_sample(outer_mm=(0, 0, 1000, 800)))
    assert result["unavoidable_uncovered_area_mm2"] == 3900 * 800
