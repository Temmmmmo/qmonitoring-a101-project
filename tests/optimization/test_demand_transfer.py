"""Source conservation is independent from physical supply and placement."""
from copy import deepcopy
from dataclasses import replace
import math

import pytest
from shapely.affinity import affine_transform
from shapely.geometry import LineString, MultiPolygon, Polygon, box

from rebar.models import Axis, Direction, Layer, Rebar, ReinforcementRecipe
from rebar.optimization.contracts.demand_transfer import (
    DemandTransferPolygon, DemandTransferProfile, DemandTransferSupplyOffer,
    TransverseDemandTransferPiece,
)
from rebar.optimization.contracts.problem import DemandCell, DemandLevel, DemandMap, LayoutProblem
from rebar.optimization.services.demand_transfer import (
    _outside_parts, _shape, check_source_demand_transfer, check_transferred_target_supply,
    conserved_strip_intensity, construct_local_transverse_transfer, construct_refined_transverse_transfer,
    make_source_demand_transfer, polygon_record,
)
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection
from rebar.optimization.services.demand_transfer_diagnostics import (
    retained_transfer_geometry_diagnostics, target_density_summary,
)

SOURCE_SHA, HOST_SHA = "a"*64, "b"*64


def fixture(axis=Axis.X, *, polygons=None, background=False, material=None):
    direction = Direction(Layer.TOP, axis)
    recipe = ReinforcementRecipe(Rebar(300, 10), (Rebar(300, 10),))
    levels = (DemandLevel(0, 1, 0, 2.6, "background", None, False, replace(recipe, additions=())),
        DemandLevel(1, 2, 2.6, 5.2, "additional", recipe.additions[0], True, recipe))
    polygons = (box(100, 400, 300, 600),) if polygons is None else polygons
    material = box(0, 0, 2000, 2000).difference(box(100, 400, 300, 600)) if material is None else material
    if axis is Axis.Y:
        polygons = tuple(affine_transform(p, (0, 1, 1, 0, 0, 0)) for p in polygons)
        material = affine_transform(material, (0, 1, 1, 0, 0, 0))
    cells = tuple(DemandCell(i, tuple(p.exterior.coords)[:-1], (p.centroid.x, p.centroid.y),
        1 if background else 2, 0 if background else 1) for i, p in enumerate(polygons))
    problem = LayoutProblem(DemandMap(direction, levels, cells, (0, 0, 2000, 2000)), case_id="synthetic")
    host = OrthogonalSolidHost((SolidHostSection(0, 200, material),), 25, 25, 25, material.area*200, 10)
    piece = TransverseDemandTransferPiece("p", 0, polygon_record(polygons[0]), 1, 250)
    return problem, host, piece


def checked(problem, host, pieces, profile=DemandTransferProfile()):
    cert = make_source_demand_transfer(problem, host, pieces, source_snapshot_sha256=SOURCE_SHA,
        host_report_sha256=HOST_SHA, profile=profile)
    return check_source_demand_transfer(problem, host, cert,
        expected_source_snapshot_sha256=SOURCE_SHA, expected_host_report_sha256=HOST_SHA)


def supplied(problem, host, checked, offers):
    return check_transferred_target_supply(problem, host, checked.certificate, offers,
        expected_source_snapshot_sha256=SOURCE_SHA, expected_host_report_sha256=HOST_SHA)


def test_sto132_reproduces_full_area_example_with_correct_units():
    result = conserved_strip_intensity((2.33, 1.66, .673), (200, 300, 300), 600)
    assert result == pytest.approx(1.9431666666666667)
    assert result*10 == pytest.approx(19.431666666666665)  # cm²/m
    assert result*600 == pytest.approx(1165.9)  # mm² in the station, NOT kg.
    previous = conserved_strip_intensity((2.33, 1.66, .673), (200, 300, 300), 800)
    assert result/previous == pytest.approx(4/3)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_complete_source_preserved_and_recipient_in_real_material(axis):
    problem, host, piece = fixture(axis)
    frozen = deepcopy(problem)
    result = checked(problem, host, (piece,))
    assert result.report["original_FE_ids"] == [0]
    assert result.report["transfer_piece_count"] == len(result.target_patches) == 1
    assert result.report["original_additional_demand_integral_mm3"] == pytest.approx(math.pi*100/1200*40000)
    assert result.report["retained_original_FE_outside_material"] == []
    assert not result.report["placement_eligible"]
    assert not result.report["prescribed_background_transferred_or_credited"]
    assert not result.report["source_band_values_reinterpreted"]
    assert problem == frozen


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_triangular_compression_preserves_every_station_not_only_global_area(axis):
    tri = Polygon(((100, 400), (300, 400), (300, 600)))
    problem, host, piece = fixture(axis, polygons=(tri,))
    piece = replace(piece, transverse_scale=.5, transverse_offset_mm=600)
    result = checked(problem, host, (piece,))
    target = _shape(result.target_patches[0].polygon)
    original = _shape(piece.source_polygon)
    intensity = math.pi*100/1200
    for s in (100.1, 111, 150, 237.9, 299.999):
        cut = LineString(((s, -1000), (s, 3000))) if axis is Axis.X else LineString(((-1000, s), (3000, s)))
        assert target.intersection(cut).length*(intensity/.5) == pytest.approx(original.intersection(cut).length*intensity)


def test_unresolved_original_pieces_are_retained_not_dropped():
    problem, host, _ = fixture()
    result = checked(problem, host, ())
    assert result.report["retained_original_FE_outside_material"] == [0]
    assert result.target_patches[0].origin == "retained_source"
    assert _shape(result.target_patches[0].polygon).equals(Polygon(problem.demand.cells[0].poly))
    assert supplied(problem, host, result, ())["status"] == "under_supplied"


def test_partial_transfer_keeps_every_unmoved_positive_piece():
    problem, host, piece = fixture()
    piece = replace(piece, source_polygon=polygon_record(box(100, 400, 200, 600)))
    result = checked(problem, host, (piece,))
    assert len(result.target_patches) == 2 and result.report["retained_original_FE_outside_material"] == [0]
    assert result.report["original_additional_demand_integral_mm3"] == pytest.approx(result.report["target_additional_demand_integral_mm3"])


def test_overlap_requirements_add_but_two_weak_supply_offers_do_not():
    problem, host, piece = fixture(polygons=(box(100, 400, 300, 600), box(100, 650, 300, 850)))
    result = checked(problem, host, (piece,))
    polygon = polygon_record(box(100, 650, 300, 850))
    a = math.pi*100/1200
    weak = tuple(DemandTransferSupplyOffer(str(i), polygon, a) for i in range(2))
    insufficient = supplied(problem, host, result, weak)
    assert insufficient["status"] == "under_supplied" and not insufficient["weak_supply_As_summed"]
    good = supplied(problem, host, result, (DemandTransferSupplyOffer("one", polygon, 2*a),))
    assert good["status"] == "pass" and not good["physical_offer_derivation_checked"]


def test_background_is_neither_transferred_nor_used_as_spare_capacity():
    problem, host, piece = fixture(background=True)
    result = checked(problem, host, ())
    assert result.target_patches == () and result.report["original_FE_ids"] == [0]
    assert result.report["original_additional_demand_integral_mm3"] == 0
    with pytest.raises(ValueError, match="background"):
        checked(problem, host, (piece,))


def test_prescribed_background_and_band_are_in_source_fingerprint():
    problem, host, piece = fixture()
    cert = checked(problem, host, (piece,)).certificate
    level = replace(problem.demand.levels[1], upper_as=999)
    changed = replace(problem, demand=replace(problem.demand, levels=(problem.demand.levels[0], level)))
    with pytest.raises(ValueError, match="binding"):
        check_source_demand_transfer(changed, host, cert, expected_source_snapshot_sha256=SOURCE_SHA,
            expected_host_report_sha256=HOST_SHA)


@pytest.mark.parametrize("change", ({"transverse_scale": 0}, {"transverse_scale": -1},
    {"transverse_scale": True}, {"transverse_scale": float("nan")},
    {"transverse_offset_mm": float("inf")}, {"transverse_offset_mm": True},
    {"transverse_offset_mm": 900}, {"transverse_offset_mm": 0}, {"source_cell_id": -1}))
def test_invalid_or_outside_or_nonlocal_operations_fail_closed(change):
    problem, host, piece = fixture()
    with pytest.raises(ValueError):
        checked(problem, host, (replace(piece, **change),))


def test_double_credit_overlap_and_duplicate_ids_fail_closed():
    problem, host, piece = fixture()
    for second in (piece, replace(piece, id="another")):
        with pytest.raises(ValueError):
            checked(problem, host, (piece, second))


def test_transfer_source_cannot_be_invented_outside_original_FE():
    problem, host, piece = fixture()
    with pytest.raises(ValueError, match="original FE"):
        checked(problem, host, (replace(piece, source_polygon=polygon_record(box(99, 400, 300, 600))),))


def test_lost_tiny_source_fragment_is_not_ignored():
    problem, host, piece = fixture()
    piece = replace(piece, source_polygon=polygon_record(box(100, 400, 300-1e-9, 600)))
    result = checked(problem, host, (piece,))
    retained = [p for p in result.target_patches if p.origin == "retained_source"]
    assert len(retained) == 1 and _shape(retained[0].polygon).area > 0


def test_another_height_slice_blocks_recipient():
    problem, host, piece = fixture()
    narrow = host.sections[0].footprint.difference(box(100, 650, 300, 850))
    host = replace(host, sections=(SolidHostSection(0, 100, host.sections[0].footprint),
        SolidHostSection(100, 200, narrow)), volume_mm3=(narrow.area+host.sections[0].footprint.area)*100)
    with pytest.raises(ValueError, match="recipient"):
        checked(problem, host, (piece,))


@pytest.mark.parametrize("profile", (DemandTransferProfile(maximum_transverse_span_mm=601),
    DemandTransferProfile(recipient_clearance_mm=29), DemandTransferProfile(maximum_vertices=3),
    DemandTransferProfile(maximum_source_cells=True), DemandTransferProfile(id="normative")))
def test_resource_and_geometry_profile_guards(profile):
    problem, host, piece = fixture()
    with pytest.raises(ValueError):
        checked(problem, host, (piece,), profile)


def test_exact_polygon_holes_survive_serialization():
    shape = box(0, 0, 200, 200).difference(box(50, 50, 100, 100))
    assert _shape(polygon_record(shape)).equals(shape)
    with pytest.raises(ValueError):
        _shape(DemandTransferPolygon(((0, 0), (1, 1), (2, 2))))


@pytest.mark.parametrize("field,value", (("source_snapshot_sha256", "c"*64),
    ("host_report_sha256", "d"*64), ("host_geometry_sha256", "e"*64),
    ("source_demand_sha256", "f"*64), ("direction", Direction(Layer.BOTTOM, Axis.X))))
def test_wrong_chain_or_direction_rejected(field, value):
    problem, host, piece = fixture()
    certificate = checked(problem, host, (piece,)).certificate
    with pytest.raises(ValueError, match="binding"):
        check_source_demand_transfer(problem, host, replace(certificate, **{field: value}),
            expected_source_snapshot_sha256=SOURCE_SHA, expected_host_report_sha256=HOST_SHA)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_finite_constructor_clears_hole_without_deleting_a_source_piece(axis):
    problem, host, _ = fixture(axis)
    result, search = construct_local_transverse_transfer(problem, host,
        source_snapshot_sha256=SOURCE_SHA, host_report_sha256=HOST_SHA)
    assert result.report["transfer_piece_count"] > 0
    assert not result.report["retained_original_FE_outside_material"]
    assert not search["unmoved_source_removed"] and search["candidate_geometry_checks"] > 0


def test_constructor_cannot_hide_longitudinal_edge_or_budget_exhaustion():
    problem, host, _ = fixture(polygons=(box(-100, 400, 100, 600),), material=box(0, 0, 2000, 2000))
    result, search = construct_local_transverse_transfer(problem, host,
        source_snapshot_sha256=SOURCE_SHA, host_report_sha256=HOST_SHA, maximum_candidate_checks=1)
    assert result.report["retained_original_FE_outside_material"] == [0]
    assert search["candidate_budget_exhausted"] and search["unmoved_fragments"]
    assert result.report["original_additional_demand_integral_mm3"] == pytest.approx(result.report["target_additional_demand_integral_mm3"])


def test_supply_always_revalidates_original_certificate():
    problem, host, piece = fixture()
    result = checked(problem, host, (piece,))
    forged = replace(result.certificate, source_demand_sha256="e"*64)
    with pytest.raises(ValueError, match="binding"):
        check_transferred_target_supply(problem, host, forged, (), expected_source_snapshot_sha256=SOURCE_SHA,
            expected_host_report_sha256=HOST_SHA)


def test_constructor_generation_resource_limit_is_not_only_geometry_checks():
    problem, host, _ = fixture()
    with pytest.raises(ValueError, match="generation budget"):
        construct_local_transverse_transfer(problem, host, source_snapshot_sha256=SOURCE_SHA,
            host_report_sha256=HOST_SHA, maximum_generated_candidates=1)


def test_source_partition_interactions_are_bounded_before_many_unions():
    problem, host, piece = fixture()
    pieces = tuple(replace(piece, id=str(i), source_polygon=polygon_record(box(100+i*50, 400, 150+i*50, 600)))
        for i in range(3))
    with pytest.raises(ValueError, match="partition interaction"):
        checked(problem, host, pieces, DemandTransferProfile(maximum_overlay_pairs=1))


@pytest.mark.parametrize("bad", (True, float("nan"), -1))
def test_supply_negative_or_nonfinite_intensity_is_not_accepted(bad):
    problem, host, piece = fixture()
    result = checked(problem, host, (piece,))
    with pytest.raises(ValueError):
        supplied(problem, host, result, (DemandTransferSupplyOffer("bad", result.target_patches[0].polygon, bad),))


def test_background_recipe_cannot_claim_additional_demand_from_global_band_width():
    problem, host, _ = fixture(background=True)
    problem = replace(problem, demand=replace(problem.demand,
        levels=(replace(problem.demand.levels[0], upper_as=1000), problem.demand.levels[1])))
    result = checked(problem, host, ())
    assert result.report["original_additional_demand_integral_mm3"] == 0
    assert result.report["source_band_values_reinterpreted"] is False


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_minimum_peak_selects_other_side_without_weakening_source_conservation(axis):
    problem, host, _ = fixture(axis, polygons=(box(100, 400, 300, 600), box(100, 150, 300, 367)))
    common = dict(source_snapshot_sha256=SOURCE_SHA, host_report_sha256=HOST_SHA)
    near, _ = construct_local_transverse_transfer(problem, host, **common)
    low, telemetry = construct_local_transverse_transfer(problem, host, recipient_selection="minimum-peak", **common)
    assert target_density_summary(low)["maximum_additional_as_mm2_per_mm"] < target_density_summary(near)["maximum_additional_as_mm2_per_mm"]
    assert low.report["original_additional_demand_integral_mm3"] == pytest.approx(low.report["target_additional_demand_integral_mm3"])
    assert telemetry["recipient_pressure"]["evaluations"] > 1
    assert not low.report["retained_FE_outside_material_above_0_001mm2"]


def test_unknown_recipient_selection_or_pressure_budget_rejected():
    problem, host, _ = fixture(polygons=(box(100, 400, 300, 600), box(100, 150, 300, 367)))
    for options in ({"recipient_selection": "magic"}, {"recipient_selection": "minimum-peak", "maximum_pressure_interactions": 1}):
        with pytest.raises(ValueError):
            construct_local_transverse_transfer(problem, host, source_snapshot_sha256=SOURCE_SHA,
                host_report_sha256=HOST_SHA, **options)


def test_retained_hole_has_explicit_local_q_corridors_not_a_false_impossibility():
    problem, host, _ = fixture()
    result = retained_transfer_geometry_diagnostics(problem, host, checked(problem, host, ()))
    assert result["remaining_direction_FE_count_above_0_001mm2"] == 1
    assert result["categories"] == {"opening": 1}
    part = result["cells"][0]["fragments"][0]
    assert part["obstruction_status"] == "rectangle_corridor_candidates_exist"
    assert part["permitted_recipient_q_envelope_mm"] == [0, 1000]
    assert part["rectangular_envelope_affine_candidates"]


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_longitudinal_global_edge_cannot_be_hidden_as_a_hole(axis):
    problem, host, _ = fixture(axis, polygons=(box(-100, 400, 100, 600),), material=box(0, 0, 2000, 2000))
    result = retained_transfer_geometry_diagnostics(problem, host, checked(problem, host, ()))
    assert result["categories"] == {"outer_edge": 1}
    part = result["cells"][0]["fragments"][0]
    assert part["obstruction_status"] == "outside_global_longitudinal_host_bound"
    assert part["outside_global_along_lower_mm"] == 100
    assert part["empty_bare_material_source_station_intervals_mm"] == [(-100, 0)]


def test_local_transverse_window_can_be_the_obstruction_inside_global_bbox():
    material = box(0, 0, 2000, 2000).difference(box(100, 0, 300, 1100))
    problem, host, _ = fixture(material=material)
    result = retained_transfer_geometry_diagnostics(problem, host, checked(problem, host, ()))
    part = result["cells"][0]["fragments"][0]
    assert part["obstruction_status"] == "no_material_within_local_transverse_window"
    assert part["outside_global_along_lower_mm"] == part["outside_global_along_upper_mm"] == 0


def test_diagnostic_target_cannot_discard_its_original_remainder():
    problem, host, _ = fixture()
    result = checked(problem, host, ())
    with pytest.raises(ValueError, match="independently rebuilt"):
        retained_transfer_geometry_diagnostics(problem, host, replace(result, target_patches=()))


def test_bare_material_station_change_is_not_replaced_by_offset_safe_event():
    notch = box(100, 0, 150, 1100).union(box(150, 0, 200, 800))
    problem, host, _ = fixture(polygons=(box(100, 400, 200, 600),),
        material=box(0, 0, 2000, 2000).difference(notch))
    result = retained_transfer_geometry_diagnostics(problem, host, checked(problem, host, ()))
    intervals = result["cells"][0]["fragments"][0]["empty_bare_material_source_station_intervals_mm"]
    assert intervals and all(100 <= a < b <= 150 for a, b in intervals)
    assert sum(b-a for a, b in intervals) == 50


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_refining_wide_source_sends_disjoint_parts_to_separate_banks(axis):
    problem, host, _ = fixture(axis, polygons=(box(100, 400, 300, 1000),),
        material=box(0, 0, 2000, 2000).difference(box(100, 400, 300, 1000)))
    original = deepcopy(problem)
    common = dict(source_snapshot_sha256=SOURCE_SHA, host_report_sha256=HOST_SHA)
    initial, _ = construct_local_transverse_transfer(problem, host, **common)
    result, search = construct_refined_transverse_transfer(problem, host, **common)
    assert initial.report["retained_original_FE_outside_material"] == [0]
    assert result.report["retained_original_FE_outside_material"] == []
    assert result.report["retained_original_FE_outside_recipient_clearance"] == []
    assert search["refinement_passes"][0]["added_transfer_pieces"] >= 2
    assert search["refinement_passes"][-1]["added_transfer_pieces"] == 0
    assert search["every_pass_source_certificate_rechecked"]
    assert result.report["original_additional_demand_integral_mm3"] == pytest.approx(result.report["target_additional_demand_integral_mm3"])
    assert problem == original
    cuts = (150, 199, 299)
    source = Polygon(problem.demand.cells[0].poly)
    for station in cuts:
        line = LineString(((station, -1000), (station, 3000))) if axis is Axis.X else LineString(((-1000, station), (3000, station)))
        required = source.intersection(line).length*math.pi*100/1200
        delivered = sum(_shape(p.polygon).intersection(line).length*p.required_additional_as_mm2_per_mm
            for p in result.target_patches)
        assert delivered == pytest.approx(required)


def test_refinement_splits_at_bare_and_safe_station_changes():
    material = box(0, 0, 2000, 2000).difference(box(100, 400, 200, 1000))
    problem, host, _ = fixture(polygons=(box(100, 400, 300, 1000),), material=material)
    result, search = construct_refined_transverse_transfer(problem, host,
        source_snapshot_sha256=SOURCE_SHA, host_report_sha256=HOST_SHA)
    assert not result.report["retained_original_FE_outside_material"]
    assert search["refinement_fragment_count"] > 0
    assert not result.report["tiny_remainders_removed_or_snapped"]


def test_refinement_keeps_longitudinal_impossibility_and_stops_without_progress():
    problem, host, _ = fixture(polygons=(box(-100, 400, -50, 600),), material=box(0, 0, 2000, 2000))
    result, search = construct_refined_transverse_transfer(problem, host,
        source_snapshot_sha256=SOURCE_SHA, host_report_sha256=HOST_SHA)
    assert result.report["retained_original_FE_outside_material"] == [0]
    assert len(search["refinement_passes"]) == 1
    assert search["refinement_passes"][0]["added_transfer_pieces"] == 0
    assert search["whole_void_translation_candidate_checks"] > 0
    assert search["accepted_whole_void_translations"] == []


@pytest.mark.parametrize("options", ({"refinement_passes": True}, {"refinement_passes": 0},
    {"maximum_transverse_fragment_width_mm": 301}, {"maximum_transverse_fragment_width_mm": float("nan")},
    {"maximum_candidate_checks": 1}, {"maximum_longitudinal_slices": 1}))
def test_refinement_resource_or_invalid_geometry_controls_fail_closed(options):
    problem, host, _ = fixture(polygons=(box(100, 400, 300, 1000),),
        material=box(0, 0, 2000, 2000).difference(box(100, 400, 300, 1000)))
    with pytest.raises(ValueError):
        construct_refined_transverse_transfer(problem, host,
            source_snapshot_sha256=SOURCE_SHA, host_report_sha256=HOST_SHA, **options)


@pytest.mark.parametrize("axis", (Axis.X, Axis.Y))
def test_optional_mask_moves_safe_but_excluded_original_demand_and_is_only_restrictive(axis):
    problem, host, _ = fixture(axis, material=box(0, 0, 2000, 2000))
    region = box(0, 650, 2500, 2500)
    if axis is Axis.Y:
        region = affine_transform(region, (0, 1, 1, 0, 0, 0))
    common = dict(source_snapshot_sha256=SOURCE_SHA, host_report_sha256=HOST_SHA)
    initial, _ = construct_local_transverse_transfer(problem, host, **common)
    result, search = construct_refined_transverse_transfer(problem, host, recipient_region=region, **common)
    assert initial.certificate.pieces == ()
    assert result.certificate.pieces
    assert all(_shape(p.polygon).difference(region).area == 0 for p in result.target_patches)
    mask = search["recipient_search_mask"]
    assert mask["provided"] and mask["intersected_safe_area_mm2"] < mask["requested_area_mm2"]
    assert not mask["independent_recipient_clearance_relaxed"]
    assert not result.report["placement_eligible"]


def test_empty_intersected_mask_does_not_destroy_demand_or_override_host():
    problem, host, _ = fixture()
    result, search = construct_refined_transverse_transfer(problem, host,
        source_snapshot_sha256=SOURCE_SHA, host_report_sha256=HOST_SHA, recipient_region=box(-1000, -1000, -500, -500))
    assert not result.certificate.pieces
    assert result.report["retained_original_FE_outside_material"] == [0]
    assert search["recipient_search_mask"]["intersected_safe_area_mm2"] == 0


@pytest.mark.parametrize("region", (LineString(((0, 0), (1, 1))), Polygon(),
    Polygon(((0, 0), (1, 1), (1, 0), (0, 1))), "host bypass"))
def test_invalid_or_unbounded_recipient_mask_is_rejected(region):
    problem, host, _ = fixture()
    with pytest.raises(ValueError, match="mask"):
        construct_local_transverse_transfer(problem, host, source_snapshot_sha256=SOURCE_SHA,
            host_report_sha256=HOST_SHA, recipient_region=region)


def test_polygon_component_diagnostics_do_not_invent_adjacent_hole_from_tiny_sliver():
    ordinary = Polygon(((16994.736, 500), (16990.678876, 847), (17133, 847), (17133, 1000),
        (17267, 1000), (17267, 847), (17377.777, 847), (17381.255608, 847), (17389.145, 500)))
    sliver = Polygon(((16990.293040000004, 880), (16990.293040000004, 879.9999999998631),
        (16990.678876, 847.000000000051), (16988.890000000003, 1000)))
    remaining = MultiPolygon((ordinary, sliver))
    material = box(16000, 0, 18000, 2000).difference(box(16700, 880, 17100, 1000))
    original_wkb = remaining.wkb
    assert sliver.area > 0
    outside = _outside_parts(remaining, material)
    assert math.fsum(p.area for p in outside) <= sliver.area
    assert remaining.wkb == original_wkb  # No snapping, no tolerance-based source deletion.
