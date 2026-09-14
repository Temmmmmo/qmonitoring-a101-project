"""Independent true-arc, cover and cut-quantity checks for sourced edge forms."""
from dataclasses import replace
import math

import pytest
from shapely.geometry import Polygon, box

from rebar.models import Axis, Direction, Layer
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.contracts.shaped_physical import (
    Arc3D, K09_U_RETURN_50D_PROFILE, Line3D,
)
from rebar.optimization.services.shaped_geometry import (
    build_g_edge_bar, build_u_edge_bar, certified_curve_chords,
    check_edge_anchor_geometry, check_shaped_host, curve_length_mm, curve_point,
    main_horizontal_interval_mm, shaped_batch_metrics, shaped_cut_length_mm,
    shaped_cutting_schedule, shaped_mass_kg, shaped_position_key, straight_bar_from_physical,
)
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection
from rebar.optimization.services.stock_cutting import check_stock_cutting


def _kwargs(**changes):
    values = dict(
        bar_id="edge-1", direction=Direction(Layer.TOP, Axis.X), steel_class="A500",
        diameter_mm=10, transverse_axis_mm=1500., edge_coordinate_mm=0., inward_sign=1,
        main_axis_z_mm=155., return_axis_z_mm=45., slab_thickness_mm=200., side_cover_mm=25.,
        cut_length_mm=2340., source_bar_ids=("source-1",), placement_profile_id="explicit-X-outer-v1",
    )
    values.update(changes)
    return values


def _bar(**changes):
    result = build_u_edge_bar(**_kwargs(**changes))
    assert result.status == "geometry_conditions_met", result.report
    assert result.bar is not None
    return result.bar


def _host(footprint=None, *, sections=None, top=40., bottom=40., side=25.):
    if sections is None:
        sections = (SolidHostSection(0., 200., box(0, 0, 5000, 5000)
                                     if footprint is None else footprint),)
    return OrthogonalSolidHost(sections, top, bottom, side, 5e9, 6)


@pytest.mark.parametrize("diameter,bridge,tangent", [(10, 50, 60), (12, 36, 67), (16, 8, 81)])
@pytest.mark.parametrize("axis", list(Axis))
@pytest.mark.parametrize("layer", list(Layer))
@pytest.mark.parametrize("inward", [-1, 1])
def test_u_exact_geometry_chirality_and_host(diameter, bridge, tangent, axis, layer, inward):
    ztop, zbottom = 160-diameter/2, 40+diameter/2
    main, returning = (ztop, zbottom) if layer is Layer.TOP else (zbottom, ztop)
    result = build_u_edge_bar(**_kwargs(
        diameter_mm=diameter, direction=Direction(layer, axis), inward_sign=inward,
        edge_coordinate_mm=0 if inward == 1 else 5000,
        main_axis_z_mm=main, return_axis_z_mm=returning,
    ))
    bar = result.bar
    assert result.status == "geometry_conditions_met"
    assert bar is not None and len(bar.segments) == 5
    assert bar.demand_segment_indexes == (0,)
    assert result.report["external_height_mm"] == 120
    assert result.report["vertical_straight_mm"] == bridge
    assert result.report["main_tangent_distance_from_edge_mm"] == tangent
    assert result.report["return_straight_mm"] == 46.5*diameter
    assert result.report["anchor_geometry"]["geometry_conditions_met"]
    assert not result.report["anchor_geometry"]["normative_anchorage_pass"]
    assert shaped_cut_length_mm(bar) == pytest.approx(2340, abs=1e-10)
    assert shaped_mass_kg(bar) == pytest.approx(0.006165*diameter**2*2.34)
    for a, b in zip(bar.segments, bar.segments[1:]):
        assert curve_point(a, 1) == b.start_mm
    host = check_shaped_host(bar, _host())
    assert host["whole_body_with_cover_contained"]
    assert host["status"] == "pass"
    assert host["maximum_actual_sagitta_mm"] <= 0.05
    assert not host["placement_eligible"] and not host["anchorage_capacity_checked"]


def test_true_arc_length_not_external_dimension_sum_and_main_credit_only():
    bar = _bar()
    d, radius = 10., 30.
    main, bridge, returning = bar.segments[0], bar.segments[2], bar.segments[-1]
    external_a = curve_length_mm(main)+d/2+radius
    external_b = curve_length_mm(bridge)+2*radius+d
    external_v = curve_length_mm(returning)+d/2+radius
    external_sum = external_a+external_b+external_v
    assert external_v == 500
    assert external_b == 120
    assert external_sum-2340 == pytest.approx(2*d+(4-math.pi)*radius)
    assert curve_length_mm(main) == pytest.approx(2340-465-50-math.pi*30)
    lo, hi = main_horizontal_interval_mm(bar)
    assert lo == 60
    assert hi-lo == pytest.approx(curve_length_mm(main))
    assert hi-lo < shaped_cut_length_mm(bar)-500
    with pytest.raises(ValueError, match="only the main"):
        shaped_cut_length_mm(replace(bar, demand_segment_indexes=(0, 4)))


def test_copied_130_external_height_fails_actual_40_40_cover():
    bar = _bar(main_axis_z_mm=160, return_axis_z_mm=40)
    assert check_shaped_host(bar, _host())["status"] == "not_proven"
    assert check_shaped_host(bar, _host(top=30, bottom=30))["status"] == "pass"


def test_inner_layer_d16_blocked_without_reducing_mandrel():
    result = build_u_edge_bar(**_kwargs(diameter_mm=16, main_axis_z_mm=136, return_axis_z_mm=64))
    assert result.status == "blocked_geometry" and result.bar is None
    assert result.report["centerline_bend_radius_mm"] == 48
    assert result.report["reason"] == "insufficient_vertical_space_for_sourced_bend_radius"


def test_exact_two_arc_contact_supported_without_degenerate_bridge_line():
    bar = _bar(main_axis_z_mm=130, return_axis_z_mm=70)
    assert len(bar.segments) == 4
    assert tuple(type(s) for s in bar.segments) == (Line3D, Arc3D, Arc3D, Line3D)
    assert check_shaped_host(bar, _host())["status"] == "pass"


def test_g_diagnostic_never_gets_supported_anchor_pass():
    result = build_g_edge_bar(**_kwargs())
    assert result.status == "blocked_anchor_geometry"
    assert result.bar is not None
    assert result.report["tip_chain_after_main_tangent_mm"] == pytest.approx(80+math.pi*15)
    assert result.report["tip_chain_after_main_tangent_mm"] < 400
    assert not result.report["anchor_geometry"]["geometry_conditions_met"]
    assert not result.report["placement_eligible"]


def test_arc_evaluation_checks_signed_sweep_and_true_extrema():
    arc = Arc3D((0., 0., 0.), (10., 0., 0.), (0., 0., 1.), math.pi)
    assert curve_point(arc, 0.5) == (0., 10., 0.)
    assert curve_point(arc, 1) == (-10., 0., 0.)
    assert curve_length_mm(arc) == pytest.approx(math.pi*10)
    assert curve_point(replace(arc, sweep_rad=-math.pi), 0.5) == (0., -10., 0.)


def test_chord_reserve_encloses_whole_arcs_not_only_samples():
    bar = _bar()
    chords = certified_curve_chords(bar, maximum_chord_error_mm=0.01)
    assert len(chords) > len(bar.segments)
    for segment_index in (1, 3):
        pieces = [c for c in chords if c.segment_index == segment_index]
        for index, chord in enumerate(pieces):
            assert 0 < chord.maximum_deviation_mm <= 0.01
            for sample in range(101):
                fraction = (index+sample/100)/len(pieces)
                p = curve_point(bar.segments[segment_index], fraction)
                lo, hi = chord.centerline_bounds_mm
                assert all(low-1e-10 <= v <= high+1e-10 for low, v, high in zip(lo, p, hi))


def test_hole_between_line_endpoints_cannot_be_missed_by_sampling():
    bar = _bar()
    footprint = box(0, 0, 5000, 5000).difference(box(900, 1450, 910, 1550))
    assert check_shaped_host(bar, _host(footprint))["status"] == "not_proven"


def test_void_at_return_height_not_hidden_by_valid_main_height():
    footprint = box(0, 0, 5000, 5000)
    sections = (SolidHostSection(0, 100, footprint.difference(box(300, 1490, 310, 1510))),
                SolidHostSection(100, 200, footprint))
    assert check_shaped_host(_bar(), _host(sections=sections))["status"] == "not_proven"


def test_missing_solid_height_and_nonorthogonal_footprints_rejected():
    with pytest.raises(ValueError, match="contiguous"):
        check_shaped_host(_bar(), _host(sections=(
            SolidHostSection(0, 90, box(0, 0, 5000, 5000)),
            SolidHostSection(100, 200, box(0, 0, 5000, 5000)),
        )))
    with pytest.raises(ValueError, match="orthogonal"):
        check_shaped_host(_bar(), _host(box(0, 0, 5000, 5000).difference(
            Polygon([(100, 100), (200, 100), (200, 200)])
        )))


@pytest.mark.parametrize("key,value", [
    ("diameter_mm", True), ("diameter_mm", 10.0), ("diameter_mm", 18),
    ("inward_sign", 1.0), ("inward_sign", True), ("inward_sign", 0),
    ("cut_length_mm", float("nan")), ("cut_length_mm", float("inf")),
    ("cut_length_mm", 11701), ("cut_length_mm", True),
    ("main_axis_z_mm", float("nan")), ("slab_thickness_mm", 0),
    ("side_cover_mm", -1), ("placement_profile_id", ""), ("steel_class", ""),
])
def test_builder_invalid_inputs_fail_closed(key, value):
    with pytest.raises(ValueError):
        build_u_edge_bar(**_kwargs(**{key: value}))


def test_builder_profile_cannot_be_weakened_under_same_id():
    for change in (dict(minimum_mandrel_diameters=1.), dict(control_anchor_diameters=0.),
                   dict(return_external_diameters=10.), dict(engineering_assumptions=())):
        with pytest.raises(ValueError, match="exact"):
            build_u_edge_bar(**_kwargs(profile=replace(K09_U_RETURN_50D_PROFILE, **change)))


@pytest.mark.parametrize("change", [
    dict(selected_cut_length_mm=2340.1), dict(selected_cut_length_mm=float("nan")),
    dict(demand_segment_indexes=(False,)), dict(demand_segment_indexes=(0.,)),
    dict(source_bar_ids=("a", "a")), dict(source_bar_ids=[]), dict(segments=[]),
    dict(shape_kind="straight"), dict(placement_profile_id=""), dict(diameter_mm=10.5),
])
def test_tampered_bar_never_reuses_builder_certificate(change):
    with pytest.raises(ValueError):
        check_shaped_host(replace(_bar(), **change), _host())


def test_tampered_arc_normal_gap_and_cut_length_rejected():
    bar = _bar()
    curves = list(bar.segments)
    curves[1] = replace(curves[1], normal_unit=(0., 1., 0.))
    with pytest.raises(ValueError):
        shaped_cut_length_mm(replace(bar, segments=tuple(curves)))
    curves = list(bar.segments)
    curves[1] = replace(curves[1], normal_unit=(0., 0.9, 0.))
    with pytest.raises(ValueError, match="cardinal"):
        shaped_cut_length_mm(replace(bar, segments=tuple(curves)))
    curves = list(bar.segments)
    curves[0] = replace(curves[0], end_mm=(60.01, 1500., 155.))
    with pytest.raises(ValueError, match="gap"):
        shaped_cut_length_mm(replace(bar, segments=tuple(curves)))


def test_selected_length_must_leave_main_leg_and_return_requirement():
    result = build_u_edge_bar(**_kwargs(cut_length_mm=400))
    assert result.bar is None and result.status == "blocked_geometry"
    result = build_u_edge_bar(**_kwargs(slab_thickness_mm=300))
    assert result.bar is None and result.status == "blocked_anchor_geometry"
    assert result.report["required_return_straight_mm"] == 600


@pytest.mark.parametrize("error,budget", [(0, 100), (True, 100), (float("nan"), 100),
                                         (0.001, 2), (0.05, True), (0.05, 0)])
def test_subdivision_bounds_are_explicit(error, budget):
    with pytest.raises(ValueError):
        check_shaped_host(_bar(), _host(), maximum_chord_error_mm=error, maximum_chords=budget)


def test_straight_adapter_preserves_inventory_and_declares_z():
    original = PhysicalBar("straight", Direction(Layer.BOTTOM, Axis.Y), "A500", 12,
                           500., (100., 3025.), ("original-1", "original-2"))
    bar = straight_bar_from_physical(original, axis_z_mm=46, placement_profile_id="explicit-X-outer-v1")
    assert bar.source_bar_ids == original.source_bar_ids
    assert bar.segments == (Line3D((500., 100., 46.), (500., 3025., 46.)),)
    assert shaped_cut_length_mm(bar) == 2925
    assert check_shaped_host(bar, _host())["status"] == "pass"
    assert not check_edge_anchor_geometry(bar, slab_thickness_mm=200)["geometry_conditions_met"]


@pytest.mark.parametrize("axis", list(Axis))
def test_flat_straight_end_flush_with_actual_side_cover_passes(axis):
    original = PhysicalBar("flush", Direction(Layer.TOP, axis), "A500", 10,
                           500., (25., 4975.), ("source-flush",))
    bar = straight_bar_from_physical(original, axis_z_mm=155, placement_profile_id="explicit-X-outer-v1")
    report = check_shaped_host(bar, _host())
    assert report["status"] == "pass" and report["straight_flat_ends"]
    assert report["cover_preserved_on_straight_end_planes"]
    too_long = replace(original, installed_interval_mm=(24.99, 4975.))
    assert check_shaped_host(straight_bar_from_physical(
        too_long, axis_z_mm=155, placement_profile_id="explicit-X-outer-v1"), _host())["status"] == "not_proven"


def test_vertical_diagnostic_G_has_flat_end_but_keeps_bottom_cover():
    result = build_g_edge_bar(**_kwargs(return_axis_z_mm=40))
    assert result.bar is not None and result.status == "blocked_anchor_geometry"
    assert check_shaped_host(result.bar, _host())["status"] == "pass"
    deeper = build_g_edge_bar(**_kwargs(return_axis_z_mm=39.99))
    assert check_shaped_host(deeper.bar, _host())["status"] == "not_proven"


def test_true_cut_lengths_feed_stock_not_outside_sums_or_physical_shape_positions():
    a = _bar(bar_id="a", cut_length_mm=5850)
    b = _bar(bar_id="b", cut_length_mm=5850, main_axis_z_mm=145, return_axis_z_mm=55)
    metrics = shaped_batch_metrics((a, b))
    assert metrics["physical_bar_count"] == 2
    assert metrics["position_count"] == 2  # Different bridge/main lengths, equal cut lengths.
    assert shaped_position_key(a) != shaped_position_key(b)
    assert metrics["true_cut_length_mm"] == pytest.approx(11700)
    assert metrics["mass_kg"] == pytest.approx(0.006165*100*11.7)
    schedule = shaped_cutting_schedule((a, b))
    assert len(schedule) == 1 and schedule[0].physical_bar_count == 2
    assert schedule[0].length_mm == 5850
    stock = check_stock_cutting(schedule)
    assert stock["status"] == "pass" and stock["stock_bar_count"] == 1
    assert not metrics["placement_eligible"]


def test_duplicate_batch_ids_rejected_even_if_shapes_differ():
    a, b = _bar(), _bar(main_axis_z_mm=145, return_axis_z_mm=55)
    with pytest.raises(ValueError, match="duplicate"):
        shaped_cutting_schedule((a, b))
