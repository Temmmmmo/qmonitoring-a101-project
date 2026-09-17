from shapely.geometry import Polygon
import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic
from rebar.legend import parse_recipe
from rebar.optimization import AxisPlacement, LayoutConstraints, PeriodicAxisPattern, RecipePlacement, build_composite_zone, build_demand_map
from rebar.optimization.contracts.composite_coverage import MONOTONE_SINGLE_STO_COVERAGE_POLICY
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.cutting import PLATE_11700_BATCH_PROFILE, PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.edge_envelope_placement import place_edge_envelope


def test_rectangle_places_both_axes_and_preserves_length():
    outer = Polygon([(0, 0), (2000, 0), (2000, 2000), (0, 2000)])
    x = place_edge_envelope(outer, (-100, 100, 1200, 200), Axis.X, (100, 200), 1300, 800)
    y = place_edge_envelope(outer, (100, -100, 200, 1200), Axis.Y, (100, 200), 1300, 800)
    assert x.status == y.status == "pass"
    assert x.bounds_mm[2]-x.bounds_mm[0] == y.bounds_mm[3]-y.bounds_mm[1] == 1300


def test_concavity_proves_core_envelope_outside_and_invalid_total_rejected():
    concave = Polygon([(0, 0), (1000, 0), (1000, 1000), (500, 1000), (500, 300), (0, 300)])
    result = place_edge_envelope(concave, (-400, 200, 1300, 400), Axis.X, (0, 900), 1700, 800)
    assert result.reason == "logical_core_envelope_outside"
    outer = Polygon([(0, 0), (2000, 0), (2000, 1000), (0, 1000)])
    assert place_edge_envelope(outer, (0, 0, 700, 100), Axis.X, (0, 100), 700, 800).reason == "total_extension_insufficient"


def test_translated_sloped_polygon_and_malformed_bounds():
    outer = Polygon([(1_000_000, 0), (1_002_000, 0), (1_001_800, 1000), (1_000_200, 1000)])
    assert place_edge_envelope(outer, (999_900, 200, 1_001_200, 300), Axis.X, (1_000_100, 1_000_300), 1300, 800).status == "pass"
    with pytest.raises(ValueError):
        place_edge_envelope(outer, (2, 0, 1, 1), Axis.X, (0, 1), 1, 0)


def test_exact_corridor_finds_narrow_window_and_rejects_short_corridor():
    narrow = Polygon([(399.2, 0), (1400.2, 0), (1400.2, 100), (399.2, 100)])
    fitted = place_edge_envelope(narrow, (0, 10, 1000, 20), Axis.X, (500, 600), 1000, 800)
    assert fitted.status == "pass" and 399 < fitted.shift_mm < 401
    short = Polygon([(0, 0), (1500, 0), (1500, 100), (0, 100)])
    assert place_edge_envelope(short, (0, 10, 2000, 20), Axis.X, (700, 800), 2000, 800).reason == "no_common_longitudinal_corridor"


def test_holes_are_not_a_generic_service_proof():
    holed = Polygon([(0, 0), (2000, 0), (2000, 1000), (0, 1000)], [[(800, 0), (1200, 0), (1200, 300), (800, 300)]])
    assert place_edge_envelope(holed, (0, 400, 1300, 500), Axis.X, (100, 200), 1300, 800).status == "not_checked"


def test_partition_before_edge_fit_can_preserve_coverage_on_an_l_shape():
    """A generic partition counterexample, independent of the saved K09 packet."""
    labels = ("s300d10", "s300d10+s150d10")
    bands = []
    for index, label in enumerate(labels):
        recipe = parse_recipe(label)
        bands.append(Band(index, index + 1, label, 0, recipe.background,
                          recipe.additions[0] if recipe.additions else None, recipe))
    cells = []
    for x in range(6):
        for y in range(6):
            if x >= 3 and y >= 3:
                continue
            extra = (x, y) in {(1, 1), (4, 1), (1, 4)}
            poly = [(x * 1000, y * 1000), ((x + 1) * 1000, y * 1000),
                    ((x + 1) * 1000, (y + 1) * 1000), (x * 1000, (y + 1) * 1000)]
            cells.append(Cell(poly, poly[0], 2 if extra else 1, bands[1 if extra else 0]))
    demand = build_demand_map(Mosaic(Direction(Layer.TOP, Axis.X), cells, bands, (0, 0, 6000, 6000)))
    placement = RecipePlacement(AxisPlacement(PeriodicAxisPattern(300, (0,)), 0),
        (AxisPlacement(PeriodicAxisPattern(300, (100, 200)), 0),), "synthetic-L")
    constraints = LayoutConstraints(min_width_cells=2, allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM,
        cutting_profile=PLATE_11700_BATCH_PROFILE)
    single = build_composite_zone(demand, (1000, 1000, 5000, 5100), 1, "single", placement,
        constraints=constraints, installed_lengths_mm=(4875,))
    split = (
        build_composite_zone(demand, (1000, 1000, 5000, 3000), 1, "lower", placement,
            constraints=constraints, installed_lengths_mm=(4875,)),
        build_composite_zone(demand, (1000, 3000, 2000, 5100), 1, "upper", placement,
            constraints=constraints, installed_lengths_mm=(1950,)),
    )
    assert evaluate_composite_coverage(demand, (single,), policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY,
        constraints=constraints).uncovered_cell_count == 0
    split_check = evaluate_composite_coverage(demand, split, policy_id=MONOTONE_SINGLE_STO_COVERAGE_POLICY,
        constraints=constraints)
    assert split_check.geometry_and_patterns_valid and split_check.uncovered_cell_count == 0
    outline = Polygon([(0, 0), (6000, 0), (6000, 3000), (3000, 3000), (3000, 6000), (0, 6000)])
    assert place_edge_envelope(outline, (600, 1000, 5475, 5000), Axis.X, (1000, 5000), 4875, 800).reason == "logical_core_envelope_outside"
    assert all(place_edge_envelope(outline, (zone.components[0].longitudinal_interval_mm[0], zone.demand_bbox[1],
        zone.components[0].longitudinal_interval_mm[1], zone.demand_bbox[3]), Axis.X,
        (zone.demand_bbox[0], zone.demand_bbox[2]), zone.components[0].installed_length_mm, 800).status == "pass" for zone in split)
