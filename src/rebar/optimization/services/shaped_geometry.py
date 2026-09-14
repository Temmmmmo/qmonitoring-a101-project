"""True line/arc geometry and conservative host checks for opt-in edge forms.

No demand transfer, strength calculation, model mutation, or production export.
Tube containment is a sufficient proof: each complete arc piece is enclosed by
its chord plus a certified sagitta reserve. Boxes are intersected with analytic
arc extrema (so cover-tangent endpoints are not falsely enlarged). The resulting
boxes, expanded by body radius AND host cover, must lie in every crossed solid
section. A failed conservative box is NOT proof the actual round tube collides.
Straight segments are cardinal, flat-ended cylinders: radius is perpendicular
to their axis, while cover is still applied in all three coordinates.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
import math

from shapely.geometry import MultiPolygon, Polygon, box

from rebar.models import Axis, Direction, Layer
from ..contracts.physical import PhysicalBar
from ..contracts.shaped_physical import (
    Arc3D, CertifiedCurveChord, Curve3D, EdgeShapeProfile, K09_U_RETURN_50D_PROFILE,
    Line3D, Point3D, ShapedBarBuildResult, ShapedPhysicalBar,
)
from .bar_schedule import BarScheduleGroup, build_bar_schedule
from .detailing import rebar_mass_kg
from .solid_host import OrthogonalSolidHost

_COORDINATE_LIMIT_MM = 1e9
_LENGTH_EPS_MM = 1e-7
_CARDINALS = ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1))


def _number(value, *, positive=False, maximum=_COORDINATE_LIMIT_MM):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or abs(value) > maximum
            or (positive and value <= 0)):
        raise ValueError("shape geometry requires bounded finite numbers")
    return float(value)


def _point(value):
    if not isinstance(value, tuple) or len(value) != 3:
        raise ValueError("shape point must be an immutable XYZ tuple")
    return tuple(_number(v) for v in value)


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError("shape identifiers and declared steel class must be nonempty strings")
    return value


def _dot(a, b):
    return math.fsum(x*y for x, y in zip(a, b))


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _difference(a, b):
    return tuple(x-y for x, y in zip(a, b))


def _trig(angle):
    # Quarter-turn templates have exactly shared endpoints. This removes only
    # libm residuals at multiples of pi/2, not arbitrary geometric coordinates.
    quadrant = round(angle / (math.pi / 2))
    if angle == quadrant * (math.pi / 2):
        return ((1., 0.), (0., 1.), (-1., 0.), (0., -1.))[quadrant % 4]
    return math.cos(angle), math.sin(angle)


def _validate_segment(segment):
    if isinstance(segment, Line3D):
        a, b = _point(segment.start_mm), _point(segment.end_mm)
        if math.dist(a, b) <= 0:
            raise ValueError("zero-length line is not a physical segment")
    elif isinstance(segment, Arc3D):
        c, a, n = _point(segment.center_mm), _point(segment.start_mm), _point(segment.normal_unit)
        if n not in _CARDINALS:
            raise ValueError("initial shape contract requires a cardinal unit arc normal")
        radial = _difference(a, c)
        if _dot(radial, n) != 0 or math.dist(a, c) <= 0:
            raise ValueError("arc start must define a positive radius in its normal plane")
        angle = _number(segment.sweep_rad, maximum=math.pi)
        if angle == 0:
            raise ValueError("arc sweep must be nonzero and at most pi")
    else:
        raise ValueError("unsupported physical curve; no implicit tessellation")


def curve_point(segment: Curve3D, fraction: float) -> Point3D:
    """Evaluate the canonical curve, including its true endpoint and midpoint."""
    _validate_segment(segment)
    f = _number(fraction, maximum=1)
    if f < 0:
        raise ValueError("curve fraction must be in [0,1]")
    if isinstance(segment, Line3D):
        return tuple(a + f*(b-a) for a, b in zip(segment.start_mm, segment.end_mm))
    radial = _difference(segment.start_mm, segment.center_mm)
    other = _cross(segment.normal_unit, radial)
    cosine, sine = _trig(f * segment.sweep_rad)
    return tuple(c + a*cosine + b*sine for c, a, b in zip(segment.center_mm, radial, other))


def curve_length_mm(segment: Curve3D) -> float:
    _validate_segment(segment)
    if isinstance(segment, Line3D):
        return math.dist(segment.start_mm, segment.end_mm)
    return math.dist(segment.start_mm, segment.center_mm) * abs(segment.sweep_rad)


def _tangent(segment, fraction):
    if isinstance(segment, Line3D):
        vector = _difference(segment.end_mm, segment.start_mm)
    else:
        radial = _difference(curve_point(segment, fraction), segment.center_mm)
        vector = _cross(segment.normal_unit, radial)
        if segment.sweep_rad < 0:
            vector = tuple(-v for v in vector)
    norm = math.hypot(*vector)
    return tuple(v/norm for v in vector)


def _validate_bar(bar):
    if not isinstance(bar, ShapedPhysicalBar):
        raise ValueError("a typed ShapedPhysicalBar is required")
    for value in (bar.id, bar.steel_class, bar.geometry_profile_id, bar.placement_profile_id):
        _text(value)
    if (not isinstance(bar.direction, Direction) or not isinstance(bar.direction.axis, Axis)
            or not isinstance(bar.direction.layer, Layer)):
        raise ValueError("typed face and axis are required")
    if (isinstance(bar.diameter_mm, bool) or not isinstance(bar.diameter_mm, int)
            or not 1 <= bar.diameter_mm <= 100):
        raise ValueError("diameter must be a positive bounded integer")
    if (not isinstance(bar.source_bar_ids, tuple) or not bar.source_bar_ids
            or len(bar.source_bar_ids) > 10000
            or any(not isinstance(v, str) or not v.strip() for v in bar.source_bar_ids)
            or len(set(bar.source_bar_ids)) != len(bar.source_bar_ids)):
        raise ValueError("source ownership must be explicit and unique within the bar")
    if not isinstance(bar.segments, tuple) or not 1 <= len(bar.segments) <= 32:
        raise ValueError("1..32 immutable physical segments are required")
    for segment in bar.segments:
        _validate_segment(segment)
        if isinstance(segment, Line3D) and sum(a != b for a, b in zip(segment.start_mm, segment.end_mm)) != 1:
            raise ValueError("scoped edge forms require cardinal straight segments")
    if bar.shape_kind not in ("straight", "G", "U"):
        raise ValueError("unsupported shape kind")
    expected_types = {"straight": (Line3D,), "G": (Line3D, Arc3D, Line3D)}
    if bar.shape_kind in expected_types:
        if tuple(type(s) for s in bar.segments) != expected_types[bar.shape_kind]:
            raise ValueError("shape kind does not match its canonical segments")
    elif tuple(type(s) for s in bar.segments) not in (
            (Line3D, Arc3D, Line3D, Arc3D, Line3D),
            (Line3D, Arc3D, Arc3D, Line3D)):
        raise ValueError("U requires two quarter arcs and two horizontal legs")
    if bar.demand_segment_indexes != (0,) or any(
            isinstance(i, bool) or not isinstance(i, int) for i in bar.demand_segment_indexes):
        raise ValueError("only the main horizontal leg may receive demand credit")
    along, transverse = (0, 1) if bar.direction.axis is Axis.X else (1, 0)
    first = bar.segments[0]
    if (first.start_mm[2] != first.end_mm[2]
            or first.start_mm[transverse] != first.end_mm[transverse]
            or first.start_mm[along] == first.end_mm[along]):
        raise ValueError("the demand leg must be horizontal along the declared axis")
    q = first.start_mm[transverse]
    for segment in bar.segments:
        if (segment.start_mm[transverse] != q or curve_point(segment, 1)[transverse] != q
                or (isinstance(segment, Arc3D) and (segment.center_mm[transverse] != q
                    or abs(segment.normal_unit[transverse]) != 1))):
            raise ValueError("edge forms must stay in their single vertical axis plane")
        if isinstance(segment, Arc3D) and abs(segment.sweep_rad) != math.pi / 2:
            raise ValueError("G/U templates require quarter-circle bends")
    for previous, following in zip(bar.segments, bar.segments[1:]):
        if math.dist(curve_point(previous, 1), following.start_mm) > _LENGTH_EPS_MM:
            raise ValueError("physical centre-line has a gap between segments")
        if math.dist(_tangent(previous, 1), _tangent(following, 0)) > 1e-10:
            raise ValueError("physical centre-line is not tangent continuous")
    if bar.shape_kind == "U":
        last = bar.segments[-1]
        if (last.start_mm[2] != last.end_mm[2]
                or _dot(_tangent(first, 0), _tangent(last, 0)) > -1 + 1e-10):
            raise ValueError("U return must be parallel and opposite to the main leg")
    selected = _number(bar.selected_cut_length_mm, positive=True)
    actual = math.fsum(curve_length_mm(s) for s in bar.segments)
    if abs(actual - selected) > _LENGTH_EPS_MM:
        raise ValueError("selected stock length is not the actual arc-inclusive cut length")


def shaped_cut_length_mm(bar: ShapedPhysicalBar) -> float:
    _validate_bar(bar)
    return math.fsum(curve_length_mm(s) for s in bar.segments)


def shaped_mass_kg(bar: ShapedPhysicalBar) -> float:
    """Use the same density convention as the existing straight-bar metric."""
    return rebar_mass_kg(bar.diameter_mm, shaped_cut_length_mm(bar), 1)


def main_horizontal_interval_mm(bar: ShapedPhysicalBar) -> tuple[float, float]:
    """Geometry only: callers still prove coverage and end anchorage separately."""
    _validate_bar(bar)
    axis = 0 if bar.direction.axis is Axis.X else 1
    return tuple(sorted((bar.segments[0].start_mm[axis], bar.segments[0].end_mm[axis])))


def _profile(profile):
    # This first sourced profile cannot be weakened by changing one dataclass
    # number while keeping the same certificate ID. A new policy needs new code.
    if not isinstance(profile, EdgeShapeProfile) or profile != K09_U_RETURN_50D_PROFILE:
        raise ValueError("only the exact, explicitly sourced K09 research profile is supported")
    return profile


def check_edge_anchor_geometry(bar: ShapedPhysicalBar, *, slab_thickness_mm: float,
                               profile: EdgeShapeProfile = K09_U_RETURN_50D_PROFILE) -> dict:
    """Recompute sourced geometric conditions; never certify anchorage capacity.

    In particular, a hand-edited arc radius cannot borrow a builder's old report.
    No part of this result changes original FE coverage or grants return-leg As.
    """
    _validate_bar(bar)
    profile = _profile(profile)
    h = _number(slab_thickness_mm, positive=True)
    reasons = []
    d = bar.diameter_mm
    arcs = [s for s in bar.segments if isinstance(s, Arc3D)]
    radii = [math.dist(s.start_mm, s.center_mm) for s in arcs]
    required = max(profile.control_anchor_diameters*d, profile.minimum_return_thicknesses*h)
    return_length = curve_length_mm(bar.segments[-1]) if bar.shape_kind == "U" else 0.
    external_return = return_length + d/2 + radii[-1] if bar.shape_kind == "U" else 0.
    if bar.geometry_profile_id != profile.id or d not in profile.allowed_diameters_mm:
        reasons.append("bar_is_not_in_the_sourced_profile")
    if bar.shape_kind != "U":
        reasons.append("no_supported_U_return_anchor")
    if any(2*r-d < profile.minimum_mandrel_diameters*d for r in radii):
        reasons.append("mandrel_below_sourced_minimum")
    if return_length < required:
        reasons.append("straight_return_shorter_than_control_40d_or_2h")
    if external_return < profile.return_external_diameters*d:
        reasons.append("external_return_shorter_than_engineer_50d")
    if bar.shape_kind == "U":
        main_z, return_z = bar.segments[0].start_mm[2], bar.segments[-1].start_mm[2]
        if (bar.direction.layer is Layer.TOP and main_z <= return_z
                or bar.direction.layer is Layer.BOTTOM and main_z >= return_z):
            reasons.append("return_is_not_towards_opposite_face")
        if abs(main_z-return_z)+d > h:
            reasons.append("external_height_exceeds_host_thickness")
    return {
        "schema_version": "edge-anchor-geometry/research-v1", "bar_id": bar.id,
        "geometry_conditions_met": not reasons, "reasons": reasons,
        "centerline_radii_mm": radii, "return_straight_mm": return_length,
        "return_external_mm": external_return, "required_return_straight_mm": required,
        "profile_id": profile.id, "engineering_assumption_required": True,
        "anchorage_capacity_checked": False, "normative_anchorage_pass": False,
        "source_demand_checked": False, "placement_eligible": False,
        "production_ready": False,
    }


def _build_edge(*, shape_kind, bar_id, direction, steel_class, diameter_mm,
                transverse_axis_mm, edge_coordinate_mm, inward_sign,
                main_axis_z_mm, return_axis_z_mm, slab_thickness_mm, side_cover_mm,
                cut_length_mm, source_bar_ids, placement_profile_id, profile):
    profile = _profile(profile)
    if (not isinstance(direction, Direction) or not isinstance(direction.axis, Axis)
            or not isinstance(direction.layer, Layer)):
        raise ValueError("explicit typed direction is required")
    for value in (bar_id, steel_class, placement_profile_id):
        _text(value)
    if (isinstance(diameter_mm, bool) or not isinstance(diameter_mm, int)
            or diameter_mm not in profile.allowed_diameters_mm):
        raise ValueError("sourced U profile supports only diameters 10,12,16")
    if (isinstance(inward_sign, bool) or not isinstance(inward_sign, int)
            or inward_sign not in (-1, 1)):
        raise ValueError("inward sign must explicitly be -1 or +1")
    q, edge, zm, zr = map(_number, (
        transverse_axis_mm, edge_coordinate_mm, main_axis_z_mm, return_axis_z_mm))
    h, length = (_number(slab_thickness_mm, positive=True), _number(cut_length_mm, positive=True))
    cover = _number(side_cover_mm)
    if cover < 0 or length > profile.maximum_cut_length_mm:
        raise ValueError("nonnegative side cover and cut length <=11700 are required")
    if (direction.layer is Layer.TOP and zm <= zr
            or direction.layer is Layer.BOTTOM and zm >= zr):
        raise ValueError("main/return Z must match the declared top/bottom face")
    d = diameter_mm
    radius = (profile.minimum_mandrel_diameters + 1) * d / 2
    separation = abs(zr-zm)
    report = {
        "schema_version": "edge-shape-build/research-v1", "shape_kind": shape_kind,
        "profile": asdict(profile), "placement_profile_id": placement_profile_id,
        "diameter_mm": d, "centerline_bend_radius_mm": radius,
        "inner_mandrel_diameter_mm": 2*radius-d, "slab_thickness_mm": h,
        "main_axis_z_mm": zm, "return_axis_z_mm": zr,
        "centerline_vertical_separation_mm": separation,
        "external_height_mm": separation+d, "selected_cut_length_mm": length,
        "required_return_straight_mm": max(profile.control_anchor_diameters*d,
                                             profile.minimum_return_thicknesses*h),
        "geometry_conditions_met": False, "anchorage_capacity_checked": False,
        "engineering_assumption_required": True, "placement_eligible": False,
        "production_ready": False, "host_checked": False, "source_demand_checked": False,
        "actual_3d_collisions_checked": False,
    }
    if separation + d > h or separation < (2*radius if shape_kind == "U" else radius):
        return ShapedBarBuildResult("blocked_geometry", None, {
            **report, "reason": "insufficient_vertical_space_for_sourced_bend_radius"})
    sign_z = 1 if zr > zm else -1
    vertical_s = edge + inward_sign*(cover+d/2)
    tangent_s = vertical_s + inward_sign*radius
    normal = ((0., float(inward_sign*sign_z), 0.) if direction.axis is Axis.X
              else (float(-inward_sign*sign_z), 0., 0.))

    def xyz(s, z):
        return (s, q, z) if direction.axis is Axis.X else (q, s, z)

    first_arc = Arc3D(xyz(tangent_s, zm+sign_z*radius), xyz(tangent_s, zm), normal, math.pi/2)
    if shape_kind == "U":
        return_straight = profile.return_external_diameters*d - d/2 - radius
        vertical_length = separation-2*radius
        main_length = length - return_straight - vertical_length - math.pi*radius
        report.update(return_external_mm=profile.return_external_diameters*d,
                      return_straight_mm=return_straight,
                      vertical_straight_mm=vertical_length)
        if return_straight < report["required_return_straight_mm"]:
            return ShapedBarBuildResult("blocked_anchor_geometry", None, {
                **report, "reason": "return_shorter_than_control_40d_or_2h"})
    else:
        vertical_length = separation-radius
        main_length = length - vertical_length - math.pi*radius/2
        report.update(return_straight_mm=0, vertical_straight_mm=vertical_length,
                      tip_chain_after_main_tangent_mm=vertical_length+math.pi*radius/2)
    if main_length <= 0:
        return ShapedBarBuildResult("blocked_geometry", None, {
            **report, "reason": "cut_length_leaves_no_positive_main_horizontal_leg"})
    curves = [Line3D(xyz(tangent_s+inward_sign*main_length, zm), xyz(tangent_s, zm)), first_arc]
    if shape_kind == "U":
        second_start = xyz(vertical_s, zr-sign_z*radius)
        if vertical_length > 0:
            curves.append(Line3D(curve_point(first_arc, 1), second_start))
        second_arc = Arc3D(xyz(tangent_s, zr-sign_z*radius), second_start, normal, math.pi/2)
        curves.extend((second_arc, Line3D(curve_point(second_arc, 1),
                                         xyz(tangent_s+inward_sign*return_straight, zr))))
    else:
        if vertical_length <= 0:
            return ShapedBarBuildResult("blocked_anchor_geometry", None, {
                **report, "reason": "G_has_no_straight_anchoring_tail"})
        curves.append(Line3D(curve_point(first_arc, 1), xyz(vertical_s, zr)))
    bar = ShapedPhysicalBar(
        bar_id, direction, steel_class, d, source_bar_ids, tuple(curves), shape_kind, (0,),
        profile.id, placement_profile_id, length,
    )
    _validate_bar(bar)
    report.update(geometry_conditions_met=True, true_cut_length_mm=shaped_cut_length_mm(bar),
                  main_horizontal_length_mm=main_length,
                  main_horizontal_interval_mm=main_horizontal_interval_mm(bar),
                  main_tangent_distance_from_edge_mm=abs(tangent_s-edge),
                  physical_bar_count=1, mass_kg=shaped_mass_kg(bar),
                  reason="geometry_only_anchorage_is_an_explicit_engineering_assumption")
    report["anchor_geometry"] = check_edge_anchor_geometry(bar, slab_thickness_mm=h, profile=profile)
    if shape_kind == "G":
        report.update(reason="G_without_return_has_no_supported_edge_anchorage_certificate")
        return ShapedBarBuildResult("blocked_anchor_geometry", bar, report)
    return ShapedBarBuildResult("geometry_conditions_met", bar, report)


def build_u_edge_bar(*, bar_id: str, direction: Direction, steel_class: str,
                     diameter_mm: int, transverse_axis_mm: float, edge_coordinate_mm: float,
                     inward_sign: int, main_axis_z_mm: float, return_axis_z_mm: float,
                     slab_thickness_mm: float, side_cover_mm: float, cut_length_mm: float,
                     source_bar_ids: tuple[str, ...], placement_profile_id: str,
                     profile: EdgeShapeProfile = K09_U_RETURN_50D_PROFILE) -> ShapedBarBuildResult:
    """Build ONE U from true stock length; never change host Z/order/cover implicitly.

    ``edge_coordinate_mm`` is supplied by the planner, not inferred from a bbox.
    The planner must independently identify an OUTER edge; this function cannot
    certify that a chosen coordinate belongs to an exterior edge rather than a hole.
    """
    return _build_edge(shape_kind="U", **locals())


def build_g_edge_bar(*, bar_id: str, direction: Direction, steel_class: str,
                     diameter_mm: int, transverse_axis_mm: float, edge_coordinate_mm: float,
                     inward_sign: int, main_axis_z_mm: float, return_axis_z_mm: float,
                     slab_thickness_mm: float, side_cover_mm: float, cut_length_mm: float,
                     source_bar_ids: tuple[str, ...], placement_profile_id: str,
                     profile: EdgeShapeProfile = K09_U_RETURN_50D_PROFILE) -> ShapedBarBuildResult:
    """Diagnostic geometry ONLY: G without a return never gets a valid anchor status."""
    return _build_edge(shape_kind="G", **locals())


def straight_bar_from_physical(bar: PhysicalBar, *, axis_z_mm: float,
                               placement_profile_id: str) -> ShapedPhysicalBar:
    if not isinstance(bar, PhysicalBar):
        raise ValueError("typed straight physical source required")
    z = _number(axis_z_mm)
    if not isinstance(bar.direction, Direction) or not isinstance(bar.direction.axis, Axis):
        raise ValueError("typed direction required")
    q = _number(bar.transverse_axis_mm)
    lo, hi = tuple(_number(v) for v in bar.installed_interval_mm)
    if hi <= lo:
        raise ValueError("straight installed interval must be positive")
    a, b = ((lo, q, z), (hi, q, z)) if bar.direction.axis is Axis.X else ((q, lo, z), (q, hi, z))
    result = ShapedPhysicalBar(
        bar.id, bar.direction, bar.steel_class, bar.diameter_mm, bar.source_bar_ids,
        (Line3D(a, b),), "straight", (0,), "straight-centreline/research-v1",
        placement_profile_id, hi-lo,
    )
    _validate_bar(result)
    return result


def _analytic_bounds(segment):
    points = [curve_point(segment, 0), curve_point(segment, 1)]
    if isinstance(segment, Arc3D):
        radial = _difference(segment.start_mm, segment.center_mm)
        other = _cross(segment.normal_unit, radial)
        lo, hi = sorted((0., segment.sweep_rad))
        for a, b in zip(radial, other):
            if a == b == 0:
                continue
            angle = math.atan2(b, a)
            for k in range(-2, 3):
                value = angle + k*math.pi
                if lo < value < hi:
                    points.append(curve_point(segment, value/segment.sweep_rad))
    return (tuple(min(p[i] for p in points) for i in range(3)),
            tuple(max(p[i] for p in points) for i in range(3)))


def certified_curve_chords(bar: ShapedPhysicalBar, *, maximum_chord_error_mm: float = 0.05,
                           maximum_chords: int = 10000) -> tuple[CertifiedCurveChord, ...]:
    _validate_bar(bar)
    error = _number(maximum_chord_error_mm, positive=True, maximum=1)
    if isinstance(maximum_chords, bool) or not isinstance(maximum_chords, int) or not 1 <= maximum_chords <= 100000:
        raise ValueError("bounded positive chord budget required")
    result = []
    for index, segment in enumerate(bar.segments):
        if isinstance(segment, Line3D):
            count, sagitta = 1, 0.
        else:
            radius = math.dist(segment.start_mm, segment.center_mm)
            # R*(1-cos(theta/2)) = 2R*sin(theta/4)^2; stable for small angles.
            maximum_angle = 4*math.asin(min(1., math.sqrt(error/(2*radius))))
            count = max(1, math.ceil(abs(segment.sweep_rad)/maximum_angle))
            sagitta = math.nextafter(2*radius*math.sin(abs(segment.sweep_rad)/(4*count))**2,
                                    math.inf)
        if len(result)+count > maximum_chords:
            raise ValueError("certified curve subdivision exceeds explicit chord budget")
        exact_lo, exact_hi = _analytic_bounds(segment)
        for piece in range(count):
            a, b = curve_point(segment, piece/count), curve_point(segment, (piece+1)/count)
            lo = tuple(max(exact_lo[i], min(a[i], b[i])-sagitta) for i in range(3))
            hi = tuple(min(exact_hi[i], max(a[i], b[i])+sagitta) for i in range(3))
            result.append(CertifiedCurveChord(index, a, b, sagitta, (lo, hi)))
    return tuple(result)


def _validate_host(host):
    if not isinstance(host, OrthogonalSolidHost) or not isinstance(host.sections, tuple) or not host.sections:
        raise ValueError("a validated orthogonal Solid host is required")
    if len(host.sections) > 128:
        raise ValueError("host section budget exceeded")
    previous_top = None
    for section in host.sections:
        low, high = _number(section.bottom_z_mm), _number(section.top_z_mm)
        shape = section.footprint
        if (low >= high or (previous_top is not None and low != previous_top)
                or not isinstance(shape, (Polygon, MultiPolygon)) or not shape.is_valid
                or shape.is_empty or shape.area <= 0):
            raise ValueError("solid sections must be finite, valid, contiguous and ordered")
        polygons = [shape] if isinstance(shape, Polygon) else list(shape.geoms)
        for polygon in polygons:
            for ring in (polygon.exterior, *polygon.interiors):
                coords = list(ring.coords)
                if any(not math.isfinite(v) or abs(v) > _COORDINATE_LIMIT_MM for p in coords for v in p):
                    raise ValueError("nonfinite host ring")
                if any(sum(a != b for a, b in zip(p, q)) != 1 for p, q in zip(coords, coords[1:])):
                    raise ValueError("host footprint must be orthogonal")
        previous_top = high
    if any(_number(v) < 0 for v in (host.top_cover_mm, host.bottom_cover_mm, host.side_cover_mm)):
        raise ValueError("host covers must be nonnegative")


def _host_contains_box(host, lo, hi):
    if lo[2] < host.sections[0].bottom_z_mm or hi[2] > host.sections[-1].top_z_mm:
        return False
    footprint = box(lo[0], lo[1], hi[0], hi[1])
    return all(section.footprint.covers(footprint) for section in host.sections
               if max(lo[2], section.bottom_z_mm) < min(hi[2], section.top_z_mm))


def check_shaped_host(bar: ShapedPhysicalBar, host: OrthogonalSolidHost, *,
                      maximum_chord_error_mm: float = 0.05,
                      maximum_chords: int = 10000) -> dict:
    """A sufficient 3D whole-tube+cover proof, NOT an anchorage/FE/collision approval."""
    _validate_host(host)
    chords = certified_curve_chords(bar, maximum_chord_error_mm=maximum_chord_error_mm,
                                    maximum_chords=maximum_chords)
    radius = bar.diameter_mm/2
    failed = []
    for index, chord in enumerate(chords):
        low, high = chord.centerline_bounds_mm
        segment = bar.segments[chord.segment_index]
        # Do not append a fictitious hemisphere to a flat-ended straight bar.
        # This is also valid for a vertical bridge; adjacent arcs have their own
        # complete conservative body enclosure, so no material is omitted.
        body_radii = tuple(0. if isinstance(segment, Line3D) and segment.start_mm[i] != segment.end_mm[i]
                           else radius for i in range(3))
        low_reserve = tuple(r+c for r, c in zip(body_radii, (
            host.side_cover_mm, host.side_cover_mm, host.bottom_cover_mm)))
        high_reserve = tuple(r+c for r, c in zip(body_radii, (
            host.side_cover_mm, host.side_cover_mm, host.top_cover_mm)))
        lo = tuple(v-r for v, r in zip(low, low_reserve))
        hi = tuple(v+r for v, r in zip(high, high_reserve))
        if not _host_contains_box(host, lo, hi):
            failed.append({"chord_index": index, "segment_index": chord.segment_index,
                           "covered_bounds_mm": [lo, hi]})
    return {
        "schema_version": "shaped-host-check/research-v1", "bar_id": bar.id,
        "status": "pass" if not failed else "not_proven",
        "whole_body_with_cover_contained": not failed,
        "proof_method": "flat-cardinal-cylinders-and-arc-sagitta-enclosures-plus-host-cover",
        "straight_flat_ends": True, "cover_preserved_on_straight_end_planes": True,
        "maximum_chord_error_mm": maximum_chord_error_mm,
        "maximum_actual_sagitta_mm": max(c.maximum_deviation_mm for c in chords),
        "chord_count": len(chords), "failed_chord_count": len(failed), "failed_chords": failed,
        "true_cut_length_mm": shaped_cut_length_mm(bar), "mass_kg": shaped_mass_kg(bar),
        "top_cover_mm": host.top_cover_mm, "bottom_cover_mm": host.bottom_cover_mm,
        "side_cover_mm": host.side_cover_mm, "round_body_bbox_overestimate": True,
        "anchorage_capacity_checked": False, "source_demand_checked": False,
        "actual_3d_collisions_checked": False, "placement_eligible": False,
        "production_ready": False,
    }


def shaped_position_key(bar: ShapedPhysicalBar) -> tuple:
    """Physical shapes with equal cut length need not be the same position."""
    _validate_bar(bar)
    geometry = tuple(("line", round(curve_length_mm(s), 6)) if isinstance(s, Line3D)
                     else ("arc", round(math.dist(s.start_mm, s.center_mm), 6),
                           round(abs(s.sweep_rad), 12)) for s in bar.segments)
    return (bar.shape_kind, bar.steel_class, bar.diameter_mm, geometry)


def shaped_batch_metrics(bars: tuple[ShapedPhysicalBar, ...]) -> dict:
    if not isinstance(bars, tuple) or len(bars) > 10000:
        raise ValueError("bounded immutable shaped batch required")
    ids, lengths = set(), defaultdict(float)
    for bar in bars:
        _validate_bar(bar)
        key = (bar.direction, bar.id)
        if key in ids:
            raise ValueError("duplicate physical bar ID within direction")
        ids.add(key)
        lengths[str(bar.direction)] += shaped_cut_length_mm(bar)
    return {"physical_bar_count": len(bars),
            "mass_kg": math.fsum(shaped_mass_kg(bar) for bar in bars),
            "position_count": len({shaped_position_key(bar) for bar in bars}),
            "true_cut_length_mm": math.fsum(shaped_cut_length_mm(bar) for bar in bars),
            "cut_length_by_direction_mm": dict(sorted(lengths.items())),
            "placement_eligible": False, "production_ready": False}


def shaped_cutting_schedule(bars: tuple[ShapedPhysicalBar, ...]):
    """Raw straight BLANKS before bending, not a physical-shape/Revit schedule.

    This feeds the existing exact 11700 stock checker. Same-diameter true cut
    lengths can share a cutting pattern even when their final shapes differ;
    use ``shaped_batch_metrics`` for actual physical position counts instead.
    """
    shaped_batch_metrics(bars)
    return build_bar_schedule(tuple(BarScheduleGroup(
        f"{bar.direction}:{bar.id}", bar.diameter_mm, shaped_cut_length_mm(bar), 1, bar.steel_class,
    ) for bar in bars))
