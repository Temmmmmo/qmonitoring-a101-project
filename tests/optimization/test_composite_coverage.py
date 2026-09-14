"""Покрытие составной схемой: без сложения слабых зон и без инженерного разрешения."""
from dataclasses import replace

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic
from rebar.legend import parse_recipe
from rebar.optimization import (
    AxisPlacement, LayoutConstraints, PeriodicAxisPattern, a101_247_slab_recipe_placement,
    build_composite_zone, build_demand_map,
)
from rebar.optimization.contracts.composite_coverage import COMPOSITE_COVERAGE_POLICY
from rebar.optimization.services.composite_coverage import (
    _service_interval, evaluate_composite_coverage, recipe_covers,
)

LABELS = ("s300d18", "s300d18+s150d18", "s300d18+s150d18+s300d25", "s300d18+s150d18+s150d25")


def demand_sample(axis=Axis.X, *, required_level=2, triangle=False):
    bands = []
    for i, label in enumerate(LABELS):
        r = parse_recipe(label)
        bands.append(Band(i, i + 1, label, 50, r.background,
                          r.additions[0] if len(r.additions) == 1 else None, r))
    cells = []
    for y in (0, 400):
        poly = [(0, y), (3900, y), (3900, y + 400), (0, y + 400)]
        if triangle:
            poly.pop(2)
        if axis is Axis.Y:
            poly = [(y, x) for x, y in poly]
        cells.append(Cell(poly, poly[0], required_level + 1, bands[required_level]))
    return build_demand_map(Mosaic(Direction(Layer.TOP, axis), cells, bands,
                                  (0, 0, 3900, 800) if axis is Axis.X else (0, 0, 800, 3900)))


def zone_sample(demand, *, level=2, phase25=50, background_origin=0, box=None, name="zone", constraints=None):
    placement = a101_247_slab_recipe_placement(demand.level(level).recipe, background_origin_mm=background_origin)
    additions = tuple(replace(a, origin_mm=phase25) if i else a for i, a in enumerate(placement.additions))
    placement = replace(placement, additions=additions, source="synthetic explicit phases, NOT a project rule")
    return build_composite_zone(demand, box or demand.bbox, level, name, placement, constraints=constraints)


def evaluate(demand, zones, **kwargs):
    return evaluate_composite_coverage(demand, zones, policy_id=COMPOSITE_COVERAGE_POLICY, **kwargs)


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
@pytest.mark.parametrize("triangle", [False, True])
def test_known_patterns_cover_all_cells_without_counting_background(axis, triangle):
    demand = demand_sample(axis, triangle=triangle)
    zone = zone_sample(demand)
    result = evaluate(demand, [zone])
    assert result.status == "pass" and result.coverage_passed and result.geometry_and_patterns_valid
    assert result.covered_cell_count == result.demanded_cell_count == 2
    assert result.uncovered_cell_count == result.uncovered_area_mm2 == 0
    assert result.physical_bar_count == 9 and result.component_count == 2
    assert result.additional_mass_kg == pytest.approx(132.1989309)
    assert not result.placement_eligible and "composite-coverage-engineering-acceptance" in result.remaining_check_ids


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_lost_edge_coverage_is_found_from_actual_phase_not_the_demand_rectangle(axis):
    demand = demand_sample(axis)
    result = evaluate(demand, [zone_sample(demand, phase25=200)])
    assert result.status == "fail" and result.geometry_and_patterns_valid
    assert result.uncovered_area_mm2 == pytest.approx(3900 * 50)
    assert result.uncovered_cell_count == 1 and result.physical_bar_count == 9


@pytest.mark.parametrize("offset,window,expected", [
    (0, (0, 800), (0, 900)), (0, (100, 200), (0, 300)),
    (0, (-200, -100), (-300, 0)), (300, (0, 800), (0, 900)),
    (-600, (-800, -100), (-900, 0)),
])
def test_actual_boundary_uses_periodic_neighbours_not_half_nominal_step(offset, window, expected):
    placement = AxisPlacement(PeriodicAxisPattern(300, (100, 200)), offset)
    assert _service_interval(placement, window) == pytest.approx(expected)


def test_uniform_boundary_and_empty_window():
    placement = AxisPlacement(PeriodicAxisPattern.uniform(300), 50)
    assert _service_interval(placement, (0, 800)) == (-100, 800)
    with pytest.raises(ValueError, match="пустое"):
        _service_interval(placement, (60, 100))


@pytest.mark.parametrize("required,supplied,expected", [
    (LABELS[2], LABELS[3], True), (LABELS[3], LABELS[2], False),
    (LABELS[1], LABELS[2], True), (LABELS[2], LABELS[1], False),
    (LABELS[2], "s300d18+s300d25+s150d18", False),
    (LABELS[2], "s300d18+s100d40", False),
    ("s300d18+s300d25+s300d25", "s300d18+s300d25", False),
    (LABELS[2], "s300d20+s150d18+s300d25", False),
])
def test_recipe_dominance_preserves_each_ordered_component(required, supplied, expected):
    assert recipe_covers(parse_recipe(required), parse_recipe(supplied)) is expected


def test_two_weak_zones_never_add_up_to_the_required_composite_recipe():
    demand = demand_sample()
    result = evaluate(demand, [zone_sample(demand, level=1, name="first"),
                               zone_sample(demand, level=1, name="second")])
    assert result.uncovered_cell_count == 2 and result.uncovered_area_mm2 == 3900 * 800
    assert result.physical_bar_count == 12  # both physical sets cost steel, but do not fulfil the recipe


@pytest.mark.parametrize("same_half", [True, False])
def test_union_of_sufficient_fragments_does_not_double_count_overlap(same_half):
    demand = demand_sample()
    first = zone_sample(demand, box=(0, 0, 1950, 800), name="left")
    second = zone_sample(demand, box=(0 if same_half else 1950, 0, 1950 if same_half else 3900, 800), name="other")
    result = evaluate(demand, [first, second])
    assert result.coverage_passed is not same_half
    assert result.uncovered_area_mm2 == (1950 * 800 if same_half else 0)
    assert result.additional_mass_kg > 0 and not result.placement_eligible
    # Their 40d extensions intersect even when demand halves only touch; no credit from anchorage.


def test_background_only_has_zero_additional_demand_and_mass():
    result = evaluate(demand_sample(required_level=0), [])
    assert result.status == "pass" and result.demanded_cell_count == result.physical_bar_count == 0
    assert not result.placement_eligible  # absence of additional demand does not prove the background exists


def test_no_zones_cannot_cover_nonempty_demand():
    result = evaluate(demand_sample(), [])
    assert result.status == "fail" and result.uncovered_cell_count == 2


@pytest.mark.parametrize("broken", ["component", "count", "mass", "direction", "phase", "duplicate_id",
                                    "uniform150", "background_relation", "mixed_background"])
def test_invalid_detailing_or_patterns_cannot_contribute_coverage(broken):
    demand = demand_sample()
    zone = zone_sample(demand)
    zones = [zone]
    if broken == "component":
        zones = [replace(zone, components=zone.components[:1])]
    elif broken in ("count", "mass"):
        part = replace(zone.components[0], **({"bar_count": 1} if broken == "count" else {"mass_kg": float("nan")}))
        zones = [replace(zone, components=(part, zone.components[1]))]
    elif broken == "direction":
        zones = [replace(zone, direction=Direction(Layer.BOTTOM, Axis.X))]
    elif broken == "phase":
        zones = [replace(zone, placement=replace(zone.placement,
                 additions=(zone.placement.additions[0], replace(zone.placement.additions[1], origin_mm=None))))]
    elif broken == "duplicate_id":
        zones = [zone, zone]
    elif broken in ("uniform150", "background_relation"):
        first = zone.placement.additions[0]
        first = (replace(first, pattern=PeriodicAxisPattern.uniform(150)) if broken == "uniform150"
                 else replace(first, origin_mm=50))
        placement = replace(zone.placement, additions=(first, zone.placement.additions[1]))
        zones = [build_composite_zone(demand, demand.bbox, 2, "bad-pattern", placement)]
    else:
        zones += [zone_sample(demand, name="other", background_origin=50)]
    result = evaluate(demand, zones)
    assert result.status == "fail" and not result.geometry_and_patterns_valid
    assert result.physical_bar_count is None and result.additional_mass_kg is None
    assert result.uncovered_cell_count == 2


def test_equivalent_origins_one_period_apart_share_the_same_background():
    demand = demand_sample()
    result = evaluate(demand, [zone_sample(demand, name="first"), zone_sample(demand, name="second", background_origin=300)])
    assert result.status == "pass" and result.physical_bar_count == 18


def test_conservative_no_overlap_profile_is_not_ignored():
    demand = demand_sample()
    result = evaluate(demand, [zone_sample(demand, name="first"), zone_sample(demand, name="second")],
                      constraints=LayoutConstraints(allow_overlaps=False))
    assert result.coverage_passed and result.status == "fail"


@pytest.mark.parametrize("bad", ["unknown_recipe", "duplicate_cell", "nonfinite", "concave", "empty_polygon", "policy", "limit"])
def test_invalid_or_unsupported_problem_does_not_return_a_successful_check(bad, monkeypatch):
    demand = demand_sample()
    if bad == "unknown_recipe":
        level = replace(demand.level(0), recipe=None, label=None, requires_extra=None)
        demand = replace(demand, levels=(level, *demand.levels[1:]))
    elif bad == "duplicate_cell":
        demand = replace(demand, cells=(demand.cells[0], demand.cells[0]))
    elif bad in ("nonfinite", "concave", "empty_polygon"):
        poly = {"nonfinite": ((0, 0), (100, 0), (float("nan"), 100)),
                "concave": ((0, 0), (100, 0), (20, 20), (0, 100)), "empty_polygon": ()}[bad]
        demand = replace(demand, cells=(replace(demand.cells[0], poly=poly),))
    elif bad == "limit":
        monkeypatch.setattr("rebar.optimization.services.composite_coverage.MAX_CELLS", 1)
    with pytest.raises((ValueError, KeyError)):
        evaluate_composite_coverage(demand, [], policy_id="unknown" if bad == "policy" else COMPOSITE_COVERAGE_POLICY)


def test_resource_limit_cannot_pass_on_a_partially_checked_layout(monkeypatch):
    demand = demand_sample()
    monkeypatch.setattr("rebar.optimization.services.composite_coverage.MAX_CELL_ZONE_PAIRS", 1)
    with pytest.raises(ValueError, match="лимит"):
        evaluate(demand, [zone_sample(demand)])
