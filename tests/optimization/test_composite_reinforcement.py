"""Составные схемы сохраняются целиком; неизвестные фазы не становятся нулевыми."""

from dataclasses import replace
import json
import random

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar, ReinforcementRecipe
from rebar.application.composite_revit_export import build_composite_zone_revit_export
from rebar.dxf_ingest import read_mosaic
from rebar.legend import build_legend, parse_label, parse_recipe
from rebar.models import UnsupportedReinforcementRecipeError
from rebar.optimization import (
    AxisPlacement, LayoutConstraints, LayoutProblem, PeriodicAxisPattern, RecipePlacement,
    a101_247_slab_recipe_placement, build_composite_zone, build_demand_map, build_layout_problem,
    build_zone_from_bbox, evaluate_composite_zone, pattern_coordinates, pattern_runs,
)
from rebar.optimization.services.bar_geometry import axis_envelope_to_body_bbox
from rebar.optimization.services.detailing import rebar_mass_kg

TRIPLE = "s300d18+s150d18+s300d25"


def recipe_mosaic(axis=Axis.X, label=TRIPLE):
    recipe = parse_recipe(label)
    single = recipe.additions[0] if len(recipe.additions) == 1 else None
    band = Band(0, 2, label, 41.8, recipe.background, single, recipe=recipe)
    cells = []
    for y in (0, 400):
        points = [(0, y), (3900, y), (3900, y + 400), (0, y + 400)]
        centroid = (1950, y + 200)
        if axis is Axis.Y:
            points = [(y, x) for x, y in points]
            centroid = centroid[::-1]
        cells.append(Cell(points, centroid, 2, band))
    return Mosaic(Direction(Layer.TOP, axis), cells, [band],
                  (0, 0, 3900, 800) if axis is Axis.X else (0, 0, 800, 3900))


def resolved_synthetic(axis=Axis.X):
    demand = build_demand_map(recipe_mosaic(axis))
    placement = a101_247_slab_recipe_placement(demand.level(0).recipe, background_origin_mm=0)
    # Только синтетический тест: 50 мм НЕ назначается реальной Revit-плите.
    placement = replace(placement, additions=(placement.additions[0], replace(placement.additions[1], origin_mm=50)),
                        source="synthetic test only; explicit 25 mm bar phase = 50 mm")
    zone = build_composite_zone(demand, demand.bbox, 0, "test", placement)
    return demand, zone


@pytest.mark.parametrize("label, additions", [
    ("s300d18", ()), (" s300d18 + s150d20 ", (Rebar(150, 20),)),
    (TRIPLE, (Rebar(150, 18), Rebar(300, 25))),
    (TRIPLE + "+s300d25", (Rebar(150, 18), Rebar(300, 25), Rebar(300, 25))),
])
def test_lossless_recipe_parser(label, additions):
    recipe = parse_recipe(label)
    assert recipe == ReinforcementRecipe(Rebar(300, 18), additions)
    if len(additions) <= 1:
        assert parse_label(label) == (recipe.background, additions[0] if additions else None)
    else:
        with pytest.raises(UnsupportedReinforcementRecipeError, match="parse_recipe"):
            parse_label(label)


@pytest.mark.parametrize("label", ("", " ", "s300d18+", "+s300d18", "s300d18++s150d18",
                                    "s300d18+oops+s300d25", "s0d18", "s300d0"))
def test_invalid_recipe_does_not_silently_skip_a_component(label):
    with pytest.raises(ValueError):
        parse_recipe(label)


def test_legend_and_demand_keep_both_additions_and_block_legacy_solver(monkeypatch):
    monkeypatch.setattr("rebar.legend.parse_shk", lambda path: [(8.5, "s300d18"), (41.8, TRIPLE)])
    bands = build_legend("synthetic.shk", [181, 2])
    assert bands[1].additional is None
    assert bands[1].reinforcement_recipe.additions == (Rebar(150, 18), Rebar(300, 25))
    mosaic = recipe_mosaic()
    assert all(c.needs_extra for c in mosaic.cells)
    demand = build_demand_map(mosaic)
    assert demand.level(0).requires_extra is True
    assert demand.level(0).recipe == parse_recipe(TRIPLE)
    assert demand.level(0).additional is None
    for action in (lambda: build_layout_problem(mosaic), lambda: LayoutProblem(demand)):
        with pytest.raises(UnsupportedReinforcementRecipeError, match="несколько дополнительных"):
            action()
    with pytest.raises(UnsupportedReinforcementRecipeError, match="Band.recipe"):
        Band(0, 2, TRIPLE, 41.8, Rebar(300, 18), Rebar(300, 25))
    with pytest.raises(ValueError, match="не согласованы"):
        replace(bands[1], additional=Rebar(300, 25))
    with pytest.raises(ValueError, match="DemandLevel"):
        replace(demand.level(0), requires_extra=False)
    with pytest.raises(UnsupportedReinforcementRecipeError, match="DemandLevel.recipe"):
        replace(demand.level(0), recipe=None, additional=Rebar(300, 25))


def test_legacy_band_positional_constructor_still_describes_one_addition():
    band = Band(1, 2, "s300d18+s100d25", 58, Rebar(300, 18), Rebar(100, 25))
    assert band.recipe is None
    assert band.reinforcement_recipe == parse_recipe(band.label)
    background = replace(band, label="s300d18", additional=None)
    assert not background.reinforcement_recipe.additions


@pytest.mark.parametrize("period, offsets", [(0, (0,)), (float("nan"), (0,)), (300, ()),
                                            (300, (200, 100)), (300, (100, 100)),
                                            (300, (-1,)), (300, (300,)), (300, (float("inf"),))])
def test_invalid_periodic_pattern(period, offsets):
    with pytest.raises(ValueError):
        PeriodicAxisPattern(period, offsets)


def test_conditional_150_axes_and_regular_subsets():
    placement = AxisPlacement(PeriodicAxisPattern(300, (100, 200)), 0)
    axes = pattern_coordinates(placement, (0, 800))
    assert axes == (100, 200, 400, 500, 700, 800)
    assert tuple(b - a for a, b in zip(axes, axes[1:])) == (100, 200, 100, 200, 100)
    assert [(r.first_axis_mm, r.actual_step_mm, r.bar_count) for r in pattern_runs(placement, (0, 800))] == [
        (100, 300, 3), (200, 300, 3),
    ]
    background = pattern_coordinates(AxisPlacement(PeriodicAxisPattern.uniform(300), 0), (0, 800))
    assert sorted((*axes, *background)) == list(range(0, 801, 100))
    assert pattern_coordinates(placement, (150, 450)) == (200, 400)  # Фаза не сбрасывается от границы зоны.
    assert pattern_coordinates(placement, (-400, 200)) == (-400, -200, -100, 100, 200)


def test_pattern_ranges_match_independent_explicit_enumeration():
    rng = random.Random(42)
    for _ in range(200):
        origin = rng.uniform(-100, 100)
        lo = rng.uniform(-700, 700)
        hi = lo + rng.uniform(1, 700)
        placement = AxisPlacement(PeriodicAxisPattern(300, (100, 200)), origin)
        expected = sorted(origin + 300 * k + offset for k in range(-10, 11) for offset in (100, 200)
                          if lo - 1e-6 <= origin + 300 * k + offset <= hi + 1e-6)
        assert pattern_coordinates(placement, (lo, hi)) == pytest.approx(expected, abs=1e-9)
        shift = 1500
        moved = pattern_coordinates(replace(placement, origin_mm=origin + shift), (lo + shift, hi + shift))
        assert moved == pytest.approx([x + shift for x in expected], abs=1e-9)


def test_unknown_phase_and_resource_limit_are_not_silent_fallbacks():
    with pytest.raises(ValueError, match="не согласована"):
        pattern_runs(AxisPlacement(PeriodicAxisPattern.uniform(100)), (0, 800))
    placement = AxisPlacement(PeriodicAxisPattern.uniform(100), 0)
    with pytest.raises(ValueError, match="не обрезан"):
        pattern_runs(placement, (0, 1e9), maximum_bars=100)
    with pytest.raises(ValueError, match="интервал"):
        pattern_runs(placement, (float("nan"), 800))
    assert pattern_coordinates(placement, (5, 95)) == ()
    assert pattern_coordinates(placement, (100, 100)) == (100,)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_revit_reference_distinguishes_axes_and_physical_body(axis):
    measured = pattern_coordinates(AxisPlacement(PeriodicAxisPattern.uniform(96.875), 12.5), (12.5, 787.5))
    exact = pattern_coordinates(AxisPlacement(PeriodicAxisPattern.uniform(100), 0), (0, 800))
    assert len(measured) == len(exact) == 9
    assert measured[-1] - measured[0] == 775
    assert exact[-1] - exact[0] == 800
    def bbox(first, last):
        return (0, first, 3900, last) if axis is Axis.X else (first, 0, last, 3900)
    assert axis_envelope_to_body_bbox(axis, bbox(12.5, 787.5), 25) == bbox(0, 800)
    assert axis_envelope_to_body_bbox(axis, bbox(0, 800), 25) == bbox(-12.5, 812.5)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_composite_mass_uses_each_actual_count_diameter_and_40d(axis):
    demand, zone = resolved_synthetic(axis)
    first, second = zone.components
    assert first.rebar.step == 150 and first.placement.pattern.mean_spacing_mm == 150
    assert first.bar_count == 6 and second.bar_count == 3
    assert first.installed_length_mm == 3900 + 80 * 18
    assert second.installed_length_mm == 3900 + 80 * 25
    check = evaluate_composite_zone(demand, zone)
    expected = .006165 * (18**2 * 5.34 * 6 + 25**2 * 5.9 * 3)
    assert check.geometry_valid
    assert check.total_mass_kg == pytest.approx(expected)
    assert check.physical_bar_count == 9  # Фон НЕ посчитан второй раз.
    assert (check.zone_count, check.component_count, check.uniform_run_count) == (1, 2, 3)
    assert "NOT_CHECKED:" in check.diagnostics[-1]


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_existing_uniform_result_matches_new_single_component_geometry(axis):
    mosaic = recipe_mosaic(axis, "s300d18+s100d25")
    problem = build_layout_problem(mosaic)
    legacy = build_zone_from_bbox(problem, mosaic.bbox, 0, "old")
    placement = RecipePlacement(AxisPlacement(PeriodicAxisPattern.uniform(300), 0),
                                (AxisPlacement(PeriodicAxisPattern.uniform(100), 0),), "legacy uniform test")
    new = build_composite_zone(problem.demand, mosaic.bbox, 0, "new", placement)
    component = new.components[0]
    assert component.rebar == legacy.rebar
    assert component.bar_count == legacy.bar_count
    assert component.mass_kg == legacy.mass_kg
    assert component.required_length_mm == legacy.required_length_mm
    assert component.anchored_length_mm == legacy.anchored_length_mm
    assert component.installed_length_mm == legacy.installed_length_mm
    assert evaluate_composite_zone(problem.demand, new).geometry_valid


def test_recipe_factory_does_not_guess_25_phase_or_height():
    demand = build_demand_map(recipe_mosaic())
    placement = a101_247_slab_recipe_placement(demand.level(0).recipe, background_origin_mm=0)
    assert placement.background.axis_depth_from_face_mm is None
    assert placement.additions[1].origin_mm is None
    with pytest.raises(ValueError, match="добавка 2: не согласована"):
        build_composite_zone(demand, demand.bbox, 0, "test", placement)
    with pytest.raises(ValueError, match="фон: не согласована"):
        build_composite_zone(demand, demand.bbox, 0, "test",
                             a101_247_slab_recipe_placement(demand.level(0).recipe))
    with pytest.raises(ValueError, match="отдельная"):
        a101_247_slab_recipe_placement(parse_recipe("s300d18+s100d25"))
    with pytest.raises(ValueError, match="фон @300"):
        a101_247_slab_recipe_placement(parse_recipe("s200d18+s150d18"))
    last_band = a101_247_slab_recipe_placement(parse_recipe("s300d18+s150d18+s150d25"), background_origin_mm=0)
    assert last_band.additions[0].origin_mm == 0
    assert last_band.additions[1].origin_mm is None  # Не помещать вторую добавку на те же оси.


@pytest.mark.parametrize("field,value", [("bar_count", 5), ("mass_kg", 0), ("required_length_mm", 3000),
                                         ("anchored_length_mm", 3900), ("installed_length_mm", float("nan")),
                                         ("component_index", 1), ("axis_window_mm", (0, 900)),
                                         ("rebar", Rebar(150, 25)), ("longitudinal_interval_mm", (0, 3900))])
def test_independent_check_rejects_tampered_component(field, value):
    demand, zone = resolved_synthetic()
    bad = replace(zone, components=(replace(zone.components[0], **{field: value}), zone.components[1]))
    assert not evaluate_composite_zone(demand, bad).geometry_valid
    with pytest.raises(ValueError, match="не прошла"):
        build_composite_zone_revit_export(demand, bad)


def test_missing_component_and_insufficient_density_are_rejected():
    demand, zone = resolved_synthetic()
    assert not evaluate_composite_zone(demand, replace(zone, components=zone.components[:1])).geometry_valid
    missing = replace(zone.placement, additions=zone.placement.additions[:1])
    with pytest.raises(ValueError, match="ВСЕ добавки"):
        build_composite_zone(demand, demand.bbox, 0, "test", missing)
    weak = replace(zone.placement, additions=(AxisPlacement(PeriodicAxisPattern.uniform(300), 100),
                                              zone.placement.additions[1]))
    with pytest.raises(ValueError, match="плотность"):
        build_composite_zone(demand, demand.bbox, 0, "test", weak)


def test_cutting_policy_and_minimum_width_are_shared():
    demand, zone = resolved_synthetic()
    constraints = LayoutConstraints(allowed_cut_lengths_mm=(5400, 6000))
    cut = build_composite_zone(demand, demand.bbox, 0, "cut", zone.placement, constraints=constraints)
    assert [c.installed_length_mm for c in cut.components] == [5400, 6000]
    assert evaluate_composite_zone(demand, cut, constraints=constraints).geometry_valid
    assert not evaluate_composite_zone(demand, cut).geometry_valid  # Другую политику нельзя подставить молча.
    with pytest.raises(ValueError, match="минимальной ширины"):
        build_composite_zone(demand, (0, 0, 3900, 200), 0, "narrow", zone.placement)
    assert rebar_mass_kg(25, 3900, 1) == pytest.approx(.006165 * 25**2 * 3.9)


def test_export_keeps_parametric_components_and_explicitly_blocks_engineering_release():
    demand, zone = resolved_synthetic()
    payload = build_composite_zone_revit_export(demand, zone)
    json.dumps(payload, allow_nan=False)
    assert payload["schema_version"] == "reinforcement-zone-revit/v2"
    assert payload["metrics"]["zone_count"] == 1
    assert payload["metrics"]["component_count"] == 2
    assert payload["metrics"]["uniform_run_count"] == 3
    first = payload["components"][0]
    assert first["nominal_step_mm"] == 150
    assert first["axis_coordinates_mm"] == [100, 200, 400, 500, 700, 800]
    assert all(r["actual_step_mm"] == 300 for r in first["uniform_runs"])
    assert payload["checks"]["geometry_and_schedule"] == "pass"
    assert payload["checks"]["export_eligible"] is False
    assert "xy-layer-order" in payload["checks"]["blocking_check_ids"]
    assert "composite-demand-coverage" in payload["checks"]["blocking_check_ids"]


def test_real_two_background_dxf_shk_preserves_all_sets_and_remains_blocked(two_background_top_x_sources):
    dxf, shk = two_background_top_x_sources
    mosaic = read_mosaic(str(dxf), str(shk))
    assert len(mosaic.cells) == 2132
    assert len(mosaic.legend) == 4
    band = next(b for b in mosaic.legend if b.label.replace(" ", "") == TRIPLE)
    assert band.additional is None
    assert band.reinforcement_recipe.additions == (Rebar(150, 18), Rebar(300, 25))
    assert all(c.needs_extra for c in mosaic.cells if c.band.index == band.index)
    demand = build_demand_map(mosaic)
    recipe = demand.level(band.index).recipe
    assert recipe == parse_recipe(TRIPLE)
    placements = a101_247_slab_recipe_placement(recipe)
    assert placements.background.origin_mm is None
    assert placements.additions[1].origin_mm is None
    with pytest.raises(UnsupportedReinforcementRecipeError):
        build_layout_problem(mosaic)


def test_fractional_actual_spacing_does_not_overwrite_nominal_specification():
    demand = build_demand_map(recipe_mosaic(label="s300d18+s100d25"))
    placement = RecipePlacement(AxisPlacement(PeriodicAxisPattern.uniform(300), 0),
                                (AxisPlacement(PeriodicAxisPattern.uniform(96.875), 12.5),),
                                "synthetic reconstruction of transverse Revit reference")
    zone = build_composite_zone(demand, demand.bbox, 0, "measured-spacing", placement)
    payload = build_composite_zone_revit_export(demand, zone)
    component = payload["components"][0]
    assert component["nominal_step_mm"] == 100
    assert component["uniform_runs"][0]["actual_step_mm"] == 96.875
    assert component["bar_count"] == 9
    assert component["bar_axis_bbox_mm"][3] - component["bar_axis_bbox_mm"][1] == 775
    assert component["straight_bar_body_bbox_mm"][3] - component["straight_bar_body_bbox_mm"][1] == 800
