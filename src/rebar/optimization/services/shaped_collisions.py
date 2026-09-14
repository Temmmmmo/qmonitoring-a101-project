"""Bounded 3D full-centreline body checks with certified curve enclosures.

Separation uses chord-distance LOWER bounds minus both sagitta errors. A capsule
overlap alone is NOT labelled a physical collision: real bar ends are flat. A
positive collision additionally needs a witness inside normal circular sections
at interior stations of both bars. Unresolved near-contact/end-cap cases are
reported separately, never silently counted as separated. Background geometry
must be passed explicitly; inventory completeness is not inferred from a tuple.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
import math

from shapely.geometry import box
from shapely.strtree import STRtree

from rebar.models import Axis
from ..contracts.shaped_physical import CertifiedCurveChord, Line3D, ShapedPhysicalBar
from .shaped_geometry import certified_curve_chords, curve_point, shaped_cut_length_mm


class ShapedCollisionLimitError(ValueError):
    """No partial-success report when a complete check exceeds its explicit budget."""


@dataclass(frozen=True)
class _Piece:
    chord: CertifiedCurveChord
    start_fraction: float
    end_fraction: float


def _dot(a, b):
    return math.fsum(x*y for x, y in zip(a, b))


def _sub(a, b):
    return tuple(x-y for x, y in zip(a, b))


def _add_scaled(a, b, scale):
    return tuple(x+scale*y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _unit(v):
    norm = math.hypot(*v)
    return tuple(value/norm for value in v)


def _clamp(value):
    return max(0., min(1., value))


def _closest_chords(a, b, c, d):
    """Convex quadratic minimum: four boundary optima plus interior optimum.

    Nearly parallel chords use decimal arithmetic for the determinant, rather
    than deleting a small determinant and overestimating the true minimum.
    The caller subtracts an explicit coordinate-ULP roundoff reserve.
    """
    u, v, w = _sub(b, a), _sub(d, c), _sub(a, c)
    aa, bb, cc, dd, ee = _dot(u, u), _dot(u, v), _dot(v, v), _dot(u, w), _dot(v, w)
    pairs = [(0., _clamp(ee/cc)), (1., _clamp((ee+bb)/cc)),
             (_clamp(-dd/aa), 0.), (_clamp((bb-dd)/aa), 1.)]
    determinant = aa*cc-bb*bb
    if determinant > aa*cc*1e-10:
        s, t = (bb*ee-cc*dd)/determinant, (aa*ee-bb*dd)/determinant
        if 0 <= s <= 1 and 0 <= t <= 1:
            pairs.append((s, t))
    else:
        with localcontext() as context:
            context.prec = 70
            # Decimal.from_float preserves the actual finite geometry input.
            uu, vv, ww = ([Decimal.from_float(float(x)) for x in vector] for vector in (u, v, w))
            aaa = sum(x*x for x in uu)
            bbb = sum(x*y for x, y in zip(uu, vv))
            ccc = sum(x*x for x in vv)
            ddd = sum(x*y for x, y in zip(uu, ww))
            eee = sum(x*y for x, y in zip(vv, ww))
            det = aaa*ccc-bbb*bbb
            if det > 0:
                s, t = (bbb*eee-ccc*ddd)/det, (aaa*eee-bbb*ddd)/det
                if 0 <= s <= 1 and 0 <= t <= 1:
                    pairs.append((float(s), float(t)))
    # Additional candidates do not change the minimum; useful collision
    # witnesses when an equal-distance endpoint minimizer would use a flat end.
    pairs.extend(((0.5, _clamp((ee+0.5*bb)/cc)), (_clamp((0.5*bb-dd)/aa), 0.5)))
    candidates = [(math.dist(_add_scaled(a, u, s), _add_scaled(c, v, t)), s, t)
                  for s, t in pairs]
    return sorted(candidates, key=lambda value: (value[0], abs(value[1]-0.5)+abs(value[2]-0.5)))


def _tangent(segment, fraction):
    if isinstance(segment, Line3D):
        return _unit(_sub(segment.end_mm, segment.start_mm))
    radial = _sub(curve_point(segment, fraction), segment.center_mm)
    normal = segment.normal_unit
    tangent = _cross(normal, radial)
    return _unit(tangent if segment.sweep_rad > 0 else tuple(-x for x in tangent))


def _disc_witness(p, n, ra, q, m, rb, guard):
    """An interior point common to two actual circular normal sections, if found."""
    crossed = _cross(n, m)
    cross2 = _dot(crossed, crossed)
    delta = _sub(q, p)
    if all(v == 0 for v in crossed):
        # Exact parallel normals only; nearly parallel is not rounded to parallel.
        if _dot(delta, n) != 0 or math.dist(p, q) >= ra+rb-4*guard:
            return None
        witness = _add_scaled(p, delta, ra/(ra+rb))
    elif cross2 < 1e-20:
        return None
    else:
        unit_line = _unit(crossed)
        perpendicular = _sub(m, tuple(_dot(m, n)*x for x in n))
        origin = _add_scaled(p, perpendicular, _dot(m, delta)/cross2)
        intervals = []
        for center, radius in ((p, ra), (q, rb)):
            offset = _sub(center, origin)
            station = _dot(offset, unit_line)
            perpendicular2 = max(0., _dot(offset, offset)-station*station)
            if perpendicular2 >= (radius-4*guard)**2:
                return None
            half = math.sqrt(max(0., radius*radius-perpendicular2))
            intervals.append((station-half, station+half))
        lo, hi = max(i[0] for i in intervals), min(i[1] for i in intervals)
        if hi-lo <= 8*guard:
            return None
        witness = _add_scaled(origin, unit_line, (lo+hi)/2)
    margins = (ra-math.dist(witness, p), rb-math.dist(witness, q))
    residuals = (abs(_dot(_sub(witness, p), n)), abs(_dot(_sub(witness, q), m)))
    if min(margins) <= 4*guard or max(residuals) > guard:
        return None
    return {"point_mm": witness, "section_radial_interior_margins_mm": margins,
            "normal_plane_residuals_mm": residuals}


def _interior_station(bar, piece, chord_fraction, guard):
    fraction = piece.start_fraction + chord_fraction*(piece.end_fraction-piece.start_fraction)
    index = piece.chord.segment_index
    segment = bar.segments[index]
    point = curve_point(segment, fraction)
    # Flat endpoint planes must not become fake positive-volume collisions.
    if index == 0 and math.dist(point, bar.segments[0].start_mm) <= 4*guard:
        return None
    if index == len(bar.segments)-1 and math.dist(point, curve_point(bar.segments[-1], 1)) <= 4*guard:
        return None
    return fraction, point, _tangent(segment, fraction)


def _prepare(bar, error, maximum_chords):
    chords = certified_curve_chords(bar, maximum_chord_error_mm=error, maximum_chords=maximum_chords)
    counts = {}
    for chord in chords:
        counts[chord.segment_index] = counts.get(chord.segment_index, 0)+1
    offsets, pieces = {}, []
    for chord in chords:
        index = offsets.get(chord.segment_index, 0)
        count = counts[chord.segment_index]
        offsets[chord.segment_index] = index+1
        pieces.append(_Piece(chord, index/count, (index+1)/count))
    low = tuple(min(c.centerline_bounds_mm[0][i] for c in chords) for i in range(3))
    high = tuple(max(c.centerline_bounds_mm[1][i] for c in chords) for i in range(3))
    return tuple(pieces), (low, high)


def _box_distance(a, b):
    delta = [max(0., a[0][i]-b[1][i], b[0][i]-a[1][i]) for i in range(3)]
    return math.hypot(*delta)


def _straight_pair(a, b):
    """Exact rational tests for cardinal, finite, flat-ended circular cylinders."""
    if a.shape_kind != "straight" or b.shape_kind != "straight":
        return None
    if a.direction.axis is not b.direction.axis:
        xbar, ybar = (a, b) if a.direction.axis is Axis.X else (b, a)
        xline, yline = xbar.segments[0], ybar.segments[0]
        xlo, xhi = sorted(Fraction(v[0]) for v in (xline.start_mm, xline.end_mm))
        ylo, yhi = sorted(Fraction(v[1]) for v in (yline.start_mm, yline.end_mm))
        x, y = Fraction(yline.start_mm[0]), Fraction(xline.start_mm[1])
        dx, dy = max(0, xlo-x, x-xhi), max(0, ylo-y, y-yhi)
        dz = Fraction(xline.start_mm[2])-Fraction(yline.start_mm[2])
        distance2 = dx*dx+dy*dy+dz*dz
        radius_sum = Fraction(a.diameter_mm+b.diameter_mm, 2)
        if distance2 >= radius_sum*radius_sum:
            return {"status": "separated", "method": "exact_orthogonal_capsule_separation",
                    "exact_tangent": distance2 == radius_sum*radius_sum}
        if xlo < x < xhi and ylo < y < yhi:
            return {"status": "collision", "method": "exact_orthogonal_interior_sections",
                    "centerline_vertical_distance_mm": float(abs(dz)),
                    "radius_sum_mm": float(radius_sum)}
        return None
    along, transverse = (0, 1) if a.direction.axis is Axis.X else (1, 0)
    sa, sb = a.segments[0], b.segments[0]
    ia = sorted((sa.start_mm[along], sa.end_mm[along]))
    ib = sorted((sb.start_mm[along], sb.end_mm[along]))
    if max(ia[0], ib[0]) >= min(ia[1], ib[1]):
        return {"status": "separated", "method": "exact_parallel_flat_end_intervals"}
    q = Fraction(sa.start_mm[transverse])-Fraction(sb.start_mm[transverse])
    z = Fraction(sa.start_mm[2])-Fraction(sb.start_mm[2])
    radius_sum = Fraction(a.diameter_mm+b.diameter_mm, 2)
    distance2 = q*q+z*z
    return {"status": "collision" if distance2 < radius_sum*radius_sum else "separated",
            "method": "exact_parallel_cylinder_positive_overlap",
            "exact_tangent": distance2 == radius_sum*radius_sum,
            "transverse_centerline_distance_mm": math.sqrt(float(distance2)),
            "radius_sum_mm": float(radius_sum)}


def _check_pair(a, b, pieces_a, pieces_b, guard, counter, maximum_checks):
    exact = _straight_pair(a, b)
    if exact is not None:
        return exact
    threshold = (a.diameter_mm+b.diameter_mm)/2
    lower, upper = math.inf, math.inf
    ambiguous = False
    for first in pieces_a:
        for second in pieces_b:
            counter[0] += 1
            if counter[0] > maximum_checks:
                raise ShapedCollisionLimitError("chord-pair budget exceeded; no partial pass")
            box_bound = _box_distance(first.chord.centerline_bounds_mm, second.chord.centerline_bounds_mm)-guard
            if box_bound >= threshold:
                lower = min(lower, box_bound)
                continue
            candidates = _closest_chords(first.chord.start_mm, first.chord.end_mm,
                                         second.chord.start_mm, second.chord.end_mm)
            distance = candidates[0][0]
            error = first.chord.maximum_deviation_mm+second.chord.maximum_deviation_mm
            low, high = max(0., distance-error-guard), distance+error+guard
            lower, upper = min(lower, low), min(upper, high)
            if low >= threshold:
                continue
            ambiguous = True
            # Finite evaluations are only positive witnesses. Absence of a
            # witness NEVER proves separation of the full curves.
            for candidate_distance, s, t in candidates:
                if candidate_distance-error >= threshold+guard:
                    continue
                pa = _interior_station(a, first, s, guard)
                pb = _interior_station(b, second, t, guard)
                if pa is None or pb is None:
                    continue
                fa, p, n = pa
                fb, q, m = pb
                witness = _disc_witness(p, n, a.diameter_mm/2, q, m, b.diameter_mm/2, guard)
                if witness is not None:
                    return {"status": "collision", "method": "actual_interior_normal_sections_witness",
                            "witness": {**witness, "first_segment_index": first.chord.segment_index,
                                        "second_segment_index": second.chord.segment_index,
                                        "first_curve_fraction": fa, "second_curve_fraction": fb},
                            "examined_centerline_distance_lower_bound_mm": lower,
                            "examined_centerline_distance_upper_bound_mm": upper}
    return {"status": "uncertain" if ambiguous else "separated",
            "method": "certified_chord_distance_and_sagitta_bounds",
            "centerline_distance_lower_bound_mm": lower,
            "centerline_distance_upper_bound_mm": None if math.isinf(upper) else upper,
            "radius_sum_mm": threshold}


def check_shaped_collisions(
    bars: tuple[ShapedPhysicalBar, ...], *,
    background_bars: tuple[ShapedPhysicalBar, ...] | None = None,
    maximum_chord_error_mm: float = 0.05, maximum_refinements: int = 3,
    maximum_bars: int = 10000, maximum_candidate_pairs: int = 200000,
    maximum_chord_pair_checks: int = 2000000, maximum_chords_per_bar: int = 10000,
) -> dict:
    """Check every provided bar pair across directions, faces and curved returns.

    Broad phase uses full-body XY bounding boxes and Z separation; the narrow
    phase never substitutes a zone bbox for physical line/arc geometry. Contact
    without a separation proof remains uncertainty, not a collision-free pass.
    """
    integer_limits = ((maximum_bars, 20000), (maximum_candidate_pairs, 2000000),
                      (maximum_chord_pair_checks, 20000000), (maximum_chords_per_bar, 100000))
    if any(type(value) is not int or not 1 <= value <= cap for value, cap in integer_limits):
        raise ValueError("collision resource limits must be bounded positive integers")
    if type(maximum_refinements) is not int or not 0 <= maximum_refinements <= 6:
        raise ValueError("maximum_refinements must be an integer in [0,6]")
    if (isinstance(maximum_chord_error_mm, bool) or not isinstance(maximum_chord_error_mm, (int, float))
            or not math.isfinite(maximum_chord_error_mm) or not 1e-9 <= maximum_chord_error_mm <= 1):
        raise ValueError("maximum chord error must be finite and in [1e-9,1]")
    if not isinstance(bars, tuple) or background_bars is not None and not isinstance(background_bars, tuple):
        raise ValueError("complete immutable bar tuples are required")
    combined = bars + (background_bars or ())
    if len(combined) > maximum_bars:
        raise ShapedCollisionLimitError("bar budget exceeded")
    identities, prepared = set(), []
    for bar in combined:
        shaped_cut_length_mm(bar)  # Full independent type/curve/continuity validation.
        key = (bar.direction, bar.id)
        if key in identities:
            raise ValueError("duplicate physical bar ID within direction across input roles")
        identities.add(key)
        prepared.append(_prepare(bar, maximum_chord_error_mm, maximum_chords_per_bar))
    coordinate_scale = max((abs(value) for _, bounds in prepared for point in bounds for value in point), default=1.)
    guard = max(1e-10, 128*math.ulp(coordinate_scale))
    rectangles, body_bounds = [], []
    for bar, (_, bounds) in zip(combined, prepared):
        radius = bar.diameter_mm/2
        low = tuple(value-radius-guard for value in bounds[0])
        high = tuple(value+radius+guard for value in bounds[1])
        body_bounds.append((low, high))
        rectangles.append(box(low[0], low[1], high[0], high[1]))
    tree = STRtree(rectangles)
    collisions, uncertain, checked, separated = [], [], 0, 0
    counter = [0]
    refined_cache = {}
    level_counts = {}

    def locator(index):
        bar = combined[index]
        return {"direction": str(bar.direction), "bar_id": bar.id, "shape_kind": bar.shape_kind,
                "role": "additional" if index < len(bars) else "provided_background"}

    for index, rectangle in enumerate(rectangles):
        for other in sorted(int(i) for i in tree.query(rectangle) if int(i) > index):
            if (body_bounds[index][0][2] > body_bounds[other][1][2]
                    or body_bounds[other][0][2] > body_bounds[index][1][2]):
                continue
            checked += 1
            if checked > maximum_candidate_pairs:
                raise ShapedCollisionLimitError("candidate-pair budget exceeded; no partial pass")
            a, b = combined[index], combined[other]
            result = None
            for level in range(maximum_refinements+1):
                parts = []
                for which in (index, other):
                    if level == 0:
                        parts.append(prepared[which][0])
                    else:
                        key = which, level
                        if key not in refined_cache:
                            refined_cache[key] = _prepare(combined[which], maximum_chord_error_mm/(4**level),
                                                           maximum_chords_per_bar)[0]
                        parts.append(refined_cache[key])
                result = _check_pair(a, b, parts[0], parts[1], guard, counter, maximum_chord_pair_checks)
                if result["status"] != "uncertain":
                    break
            level_counts[level] = level_counts.get(level, 0)+1
            record = {"first": locator(index), "second": locator(other),
                      "refinement_level": level, **result}
            if result["status"] == "collision":
                collisions.append(record)
            elif result["status"] == "uncertain":
                uncertain.append(record)
            else:
                separated += 1
    return {
        "schema_version": "shaped-collision-check/research-v1",
        "status": "fail" if collisions else "not_proven" if uncertain else "pass",
        "physical_bar_count": len(combined), "additional_bar_count": len(bars),
        "provided_background_bar_count": len(background_bars) if background_bars is not None else None,
        "background_input_state": "not_provided" if background_bars is None
        else "explicit_empty" if not background_bars else "provided_geometry_only",
        "background_inventory_complete": False,
        "proven_collision_pair_count": len(collisions), "uncertain_pair_count": len(uncertain),
        "proven_collision_pairs": collisions, "uncertain_pairs": uncertain,
        "complete_no_body_collision_proof": not collisions and not uncertain,
        "candidate_pairs_checked": checked, "certified_separated_candidate_pairs": separated,
        "chord_pair_checks": counter[0], "refinement_level_pair_counts": level_counts,
        "maximum_chord_error_mm": maximum_chord_error_mm,
        "maximum_refinements": maximum_refinements, "numerical_roundoff_reserve_mm": guard,
        "straight_flat_ends_checked": True, "positive_collision_requires_interior_body_witness": True,
        "modeled_cross_direction_and_face_3d_checked": True,
        "collision_scope": "every_distinct_pair_of_provided_physical_bars",
        "individual_bar_self_intersection_checked": False,
        "host_checked": False, "source_demand_checked": False,
        "anchorage_capacity_checked": False, "revit_readback_checked": False,
        "placement_eligible": False, "production_ready": False,
    }
