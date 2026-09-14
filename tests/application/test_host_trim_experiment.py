"""Shorter stock pieces are allowed only beyond every original full40d requirement."""
from copy import deepcopy
from dataclasses import replace
import importlib.util
from pathlib import Path

import pytest
from shapely.geometry import box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.opening_relocation import SourceServiceLane
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection


@pytest.fixture
def trim(monkeypatch):
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("host_trim_experiment_test", scripts / "experiment_host_trim.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def bar_case(axis):
    direction = Direction(Layer.TOP, axis)
    source = PhysicalSourceBar("zone/0/0", direction, "A500", 10, 100,
        (25, 3925), (1000, 1500), 10, 0)
    bar = PhysicalBar("physical", direction, "A500", 10, 100,
        (25, 3925), (source.id,))
    return bar, (source,)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_shorter_catalogue_piece_fits_without_shortening_40d_or_moving_axis(trim, axis):
    bar, sources = bar_case(axis)
    before = deepcopy((bar, sources))
    options = trim.shorter_options(bar, sources, box(0, 0, 2600, 2600), 25, subset_only=True)
    assert min(b.installed_length_mm for b in options) == 1300
    assert all(b.installed_length_mm < bar.installed_length_mm for b in options)
    assert all(25 <= b.installed_interval_mm[0] <= 600 for b in options)
    assert all(1900 <= b.installed_interval_mm[1] <= 2575 for b in options)
    assert all(replace(b, installed_interval_mm=bar.installed_interval_mm) == bar for b in options)
    assert (bar, sources) == before


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_necessary_anchorage_outside_host_cannot_be_trimmed_away(trim, axis):
    bar, sources = bar_case(axis)
    sources = (replace(sources[0], required_interval_mm=(1000, 2300)),)
    assert trim.shorter_options(bar, sources, box(0, 0, 2600, 2600), 25, subset_only=True) == ()
    assert trim.shorter_options(bar, sources, box(0, 0, 2600, 2600), 25, subset_only=False) == ()


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_shortening_keeps_all_merged_source_owners(trim, axis):
    bar, sources = bar_case(axis)
    second = replace(sources[0], id="zone2/0/0", required_interval_mm=(1800, 2300))
    bar = replace(bar, source_bar_ids=(*bar.source_bar_ids, second.id))
    assert trim.shorter_options(bar, (*sources, second), box(0, 0, 2600, 2600), 25,
                                subset_only=True) == ()


def test_invalid_original_40d_is_rejected_not_repaired_by_trimming(trim):
    bar, sources = bar_case(Axis.X)
    with pytest.raises(ValueError, match="40d"):
        trim.shorter_options(replace(bar, installed_interval_mm=(700, 3925)), sources,
                              box(0, 0, 2600, 2600), 25, subset_only=True)


def test_wrong_source_axis_cannot_be_silently_accepted(trim):
    bar, sources = bar_case(Axis.X)
    with pytest.raises(ValueError):
        trim.shorter_options(replace(bar, transverse_axis_mm=101), sources,
                              box(0, 0, 2600, 2600), 25, subset_only=True)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_length_position_uses_exact_body_contact_event_not_only_window_endpoints(trim, axis):
    bar, parents = bar_case(axis)
    other = replace(bar, id="neighbour", installed_interval_mm=(-500, 500))
    options = trim._contained_length_positions(bar, 3900, parents,
                                               box(-1000, -1000, 6000, 6000), 25, (other,))
    assert options[0].installed_interval_mm == (500, 4400)
    assert all(not trim.collision(b, other) for b in options)
    assert all(trim.contained(b, box(-1000, -1000, 6000, 6000), 25) for b in options)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_existing_collision_pair_is_not_grandfathered_for_extended_donor(trim, axis):
    bar, parents = bar_case(axis)
    other = replace(bar, id="existing-neighbour", installed_interval_mm=(1000, 1500))
    assert trim.collision(bar, other)
    assert trim._contained_length_positions(bar, 3900, parents,
        box(-1000, -1000, 6000, 6000), 25, (other,)) == ()


def exchange_case(axis):
    direction = Direction(Layer.TOP, axis)
    specs = ((-100, 3800, 1000, 1500), (5000, 6950, 5400, 5900),
             (6000, 9900, 6400, 6900), (9000, 10950, 9400, 9900))
    bars, lanes, polygons = [], [], []
    for index, (lo, hi, req_lo, req_hi) in enumerate(specs):
        coordinate = 100 + index * 300
        source = PhysicalSourceBar(f"zone-{index}/0/0", direction, "A500", 10, coordinate,
            (lo, hi), (req_lo, req_hi), 10, 0)
        bars.append(PhysicalBar(f"bar-{index}", direction, "A500", 10, coordinate,
                                (lo, hi), (source.id,)))
        lanes.append(SourceServiceLane(source, f"zone-{index}", 0, 0, 300,
            (coordinate-150, coordinate+150), (150, 150)))
        polygons.append(box(req_lo, coordinate-100, req_hi, coordinate+100) if axis is Axis.X
                        else box(coordinate-100, req_lo, coordinate+100, req_hi))
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(300, 10),))
    levels = (DemandLevel(0, 1, 0, 1, "s300d10", None, False, replace(recipe, additions=())),
              DemandLevel(1, 2, 1, 2, "s300d10+s300d10", recipe.additions[0], True, recipe))
    footprint = box(0, -1000, 12000, 2000) if axis is Axis.X else box(-1000, 0, 2000, 12000)
    problems = tuple(LayoutProblem(DemandMap(current, levels,
        tuple(DemandCell(index+1, tuple(p.exterior.coords)[:-1], (p.centroid.x, p.centroid.y),
                        2 if current == direction else 1, int(current == direction))
              for index, p in enumerate(polygons)), footprint.bounds)) for current in PLATE_DIRECTIONS)
    host = OrthogonalSolidHost((SolidHostSection(0, 200, footprint),), 25, 25, 25,
                              footprint.area * 200, 6)
    shortened = replace(bars[0], installed_interval_mm=(25, 1975))
    alternatives = {(str(direction), bars[0].id): (shortened,)}
    return tuple(bars), tuple(lanes), PlateProblem(problems), host, alternatives


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_exchange_preserves_entire_stock_and_all_fresh_demand(trim, axis):
    data = exchange_case(axis)
    untouched = deepcopy(data)
    report = trim.inspect_stock_exchanges(*data)
    assert data == untouched
    assert report["status"] == "full_inventory_exchange_found"
    assert report["pairs_checked"] == 1
    result = report["accepted"][0]
    assert result["host_blocked_after"] == 0
    assert result["physical_bar_count"] == 4
    assert result["stock_cutting"]["status"] == "pass"
    assert result["source_coverage"]["status"] == "pass"
    assert result["source_coverage"]["uncovered_cell_count"] == 0
    assert result["same_direction_body_pairs"] == result["new_body_pairs"] == 0
    assert result["full_layout_status"] == "not_engineering_accepted"
    assert "actual_Z_and_cross_direction_3D_collisions" in result["not_checked"]
    assert "Revit_readback" in result["not_checked"]
    assert result["body_collision_scope"] == "same_direction_only_no_Z_or_cross_direction_check"
    assert not report["placement_eligible"]


def test_exchange_budget_exhaustion_is_not_reported_as_proven_infeasibility(trim):
    report = trim.inspect_stock_exchanges(*exchange_case(Axis.X), maximum_pairs=0)
    assert report["status"] == "not_checked_pair_limit"
    assert report["pairs_checked"] == 0
    assert report["accepted"] == []
