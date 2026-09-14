"""Cross-face/axis/arc collision proof and explicit contact uncertainty."""
from dataclasses import replace
from fractions import Fraction
import math
import random

import pytest

from rebar.models import Axis, Direction, Layer
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.contracts.shaped_physical import Line3D
from rebar.optimization.services.shaped_collisions import (
    ShapedCollisionLimitError, _closest_chords, check_shaped_collisions,
)
from rebar.optimization.services.shaped_geometry import (
    build_u_edge_bar, curve_point, straight_bar_from_physical,
)


def _straight(id="a", *, axis=Axis.X, layer=Layer.TOP, q=50., z=155., lo=0., hi=100., diameter=10):
    source = PhysicalBar(id, Direction(layer, axis), "A500", diameter, q, (lo, hi), (f"source-{id}",))
    return straight_bar_from_physical(source, axis_z_mm=z, placement_profile_id="explicit-test-profile")


def _u(id="u", **changes):
    values = dict(
        bar_id=id, direction=Direction(Layer.TOP, Axis.X), steel_class="A500",
        diameter_mm=10, transverse_axis_mm=1500., edge_coordinate_mm=0., inward_sign=1,
        main_axis_z_mm=155., return_axis_z_mm=45., slab_thickness_mm=200., side_cover_mm=25.,
        cut_length_mm=2340., source_bar_ids=(f"source-{id}",), placement_profile_id="explicit-test-profile",
    )
    values.update(changes)
    result = build_u_edge_bar(**values)
    assert result.bar is not None
    return result.bar


def test_parallel_positive_body_overlap_uses_height_not_just_plan():
    a = _straight()
    b = _straight("b", q=50., z=164., lo=25, hi=75)
    report = check_shaped_collisions((a, b))
    assert report["proven_collision_pair_count"] == 1
    assert report["uncertain_pair_count"] == 0
    assert report["proven_collision_pairs"][0]["method"] == "exact_parallel_cylinder_positive_overlap"
    assert report["status"] == "fail" and not report["placement_eligible"]
    assert check_shaped_collisions((a, _straight("b", z=165)))["status"] == "pass"
    assert check_shaped_collisions((a, _straight("b", z=166)))["status"] == "pass"


def test_parallel_diagonal_transverse_distance_is_round_not_rectangle():
    report = check_shaped_collisions((_straight(), _straight("b", q=58, z=163)))
    assert report["status"] == "pass"  # sqrt(8²+8²)>10, despite overlap of body AABBs.


@pytest.mark.parametrize("gap", [0, 0.001, 1, 4])
def test_flat_end_to_end_contact_is_not_fake_capsule_collision(gap):
    report = check_shaped_collisions((_straight(), _straight("b", lo=100+gap, hi=200)))
    assert report["status"] == "pass"
    assert report["proven_collision_pair_count"] == 0


@pytest.mark.parametrize("z,expected", [(155, "fail"), (164, "fail"), (165, "pass"), (166, "pass")])
def test_cross_direction_and_face_are_checked_with_exact_tangency(z, expected):
    a = _straight()
    b = _straight("b", axis=Axis.Y, layer=Layer.BOTTOM, z=z)
    report = check_shaped_collisions((a, b))
    assert report["status"] == expected
    assert report["uncertain_pair_count"] == 0
    assert report["modeled_cross_direction_and_face_3d_checked"]


def test_opposite_face_return_leg_not_lost_when_main_is_far_away():
    u = _u()
    lower = _straight("lower", axis=Axis.Y, layer=Layer.BOTTOM, q=300, z=45, lo=1400, hi=1600)
    report = check_shaped_collisions((u, lower))
    assert report["proven_collision_pair_count"] == 1
    pair = report["proven_collision_pairs"][0]
    assert pair["method"] == "actual_interior_normal_sections_witness"
    assert pair["witness"]["first_segment_index"] == 4
    assert min(pair["witness"]["section_radial_interior_margins_mm"]) > 0


def test_actual_arc_collision_not_replaced_by_chord_or_main_projection():
    u = _u()
    p = curve_point(u.segments[1], 0.5)
    crossing = _straight("arc-cross", axis=Axis.Y, q=p[0], z=p[2], lo=1450, hi=1550)
    report = check_shaped_collisions((u, crossing), maximum_chord_error_mm=1)
    assert report["proven_collision_pair_count"] == 1
    assert report["proven_collision_pairs"][0]["witness"]["first_segment_index"] == 1


def test_different_planes_of_U_bars_prove_clearance_with_sagitta():
    report = check_shaped_collisions((_u("a"), _u("b", transverse_axis_mm=1511)))
    assert report["status"] == "pass"
    assert report["proven_collision_pair_count"] == 0


def test_tangent_U_contact_is_explicitly_uncertain_not_silently_green():
    report = check_shaped_collisions((_u("a"), _u("b", transverse_axis_mm=1510)),
                                     maximum_refinements=1)
    assert report["proven_collision_pair_count"] == 0
    assert report["uncertain_pair_count"] == 1
    assert report["status"] == "not_proven"
    assert not report["complete_no_body_collision_proof"]


def test_flat_tip_capsule_overlap_does_not_prove_real_body_penetration():
    a = _straight(q=0, z=0)
    b = _straight("b", axis=Axis.Y, q=105, z=0, lo=0, hi=100)
    # A ends at x=100, B's cylinder touches x=100. Capsule overlap is positive,
    # but actual flat-cylinder bodies only touch the plane. No false collision.
    report = check_shaped_collisions((a, b), maximum_refinements=0)
    assert report["proven_collision_pair_count"] == 0
    assert report["uncertain_pair_count"] == 1


def test_background_is_never_inferred_and_explicit_geometry_participates():
    a, b = _straight(), _straight("background", q=50, z=155)
    absent = check_shaped_collisions((a,))
    assert absent["background_input_state"] == "not_provided"
    assert absent["provided_background_bar_count"] is None
    report = check_shaped_collisions((a,), background_bars=(b,))
    assert report["proven_collision_pair_count"] == 1
    assert report["proven_collision_pairs"][0]["second"]["role"] == "provided_background"
    assert not report["background_inventory_complete"]
    empty = check_shaped_collisions((a,), background_bars=())
    assert empty["background_input_state"] == "explicit_empty"
    assert not empty["background_inventory_complete"]


def test_direction_aware_ids_and_no_duplicate_skipping():
    a = _straight("same")
    b = _straight("same", axis=Axis.Y)
    assert check_shaped_collisions((a, b))["proven_collision_pair_count"] == 1
    with pytest.raises(ValueError, match="duplicate"):
        check_shaped_collisions((a, a))
    with pytest.raises(ValueError, match="duplicate"):
        check_shaped_collisions((a,), background_bars=(a,))


@pytest.mark.parametrize("kwargs", [
    dict(maximum_refinements=True), dict(maximum_refinements=-1), dict(maximum_refinements=7),
    dict(maximum_chord_error_mm=float("nan")), dict(maximum_chord_error_mm=True),
    dict(maximum_chord_error_mm=1e-320),
    dict(maximum_chord_error_mm=0), dict(maximum_bars=True), dict(maximum_candidate_pairs=0),
    dict(maximum_chord_pair_checks=False), dict(maximum_chords_per_bar=0),
])
def test_invalid_limits_fail_closed(kwargs):
    with pytest.raises(ValueError):
        check_shaped_collisions((_straight(),), **kwargs)


def test_bar_pair_and_chord_budgets_raise_not_partial_pass():
    bars = (_straight("a"), _straight("b"), _straight("c"))
    with pytest.raises(ShapedCollisionLimitError, match="bar budget"):
        check_shaped_collisions(bars, maximum_bars=2)
    with pytest.raises(ShapedCollisionLimitError, match="candidate-pair"):
        check_shaped_collisions(bars, maximum_candidate_pairs=1)
    with pytest.raises(ShapedCollisionLimitError, match="chord-pair"):
        check_shaped_collisions((_u("a"), _u("b", transverse_axis_mm=1510)),
                                maximum_chord_pair_checks=1)


def test_tampered_geometry_is_checked_before_broad_phase():
    a = _straight()
    corrupt = replace(a, segments=(Line3D((0., 50., float("nan")), (100., 50., 155.)),))
    with pytest.raises(ValueError):
        check_shaped_collisions((corrupt,))
    with pytest.raises(ValueError):
        check_shaped_collisions([a])
    assert check_shaped_collisions(())["status"] == "pass"


def _exact_segment_distance(a, b, c, d):
    a, b, c, d = (tuple(Fraction(v) for v in p) for p in (a, b, c, d))
    u, v, w = tuple(y-x for x, y in zip(a, b)), tuple(y-x for x, y in zip(c, d)), tuple(x-y for x, y in zip(a, c))
    aa, bb, cc, dd, ee = (sum(x*y for x, y in zip(p, q)) for p, q in ((u, u), (u, v), (v, v), (u, w), (v, w)))
    def clamp(value):
        return max(0, min(1, value))
    points = [(0, clamp(ee/cc)), (1, clamp((ee+bb)/cc)), (clamp(-dd/aa), 0), (clamp((bb-dd)/aa), 1)]
    determinant = aa*cc-bb*bb
    if determinant:
        s, t = (bb*ee-cc*dd)/determinant, (aa*ee-bb*dd)/determinant
        if 0 <= s <= 1 and 0 <= t <= 1:
            points.append((s, t))
    return math.sqrt(float(min(sum((x+s*y-t*z)**2 for x, y, z in zip(w, u, v)) for s, t in points)))


def test_chord_distance_matches_rational_oracle_including_nearly_parallel():
    randomizer = random.Random(4737)
    cases = []
    for _ in range(100):
        points = [tuple(float(randomizer.randint(-100, 100)) for _ in range(3)) for _ in range(4)]
        cases.append(points)
    for epsilon in (1e-2, 1e-5, 1e-8, 1e-11):
        cases.append(((0., 0., 0.), (100., 0., 0.), (0., epsilon, 0.), (100., -epsilon, 0.)))
    for points in cases:
        expected = _exact_segment_distance(*points)
        got = _closest_chords(*points)[0][0]
        assert got == pytest.approx(expected, abs=1e-10)
