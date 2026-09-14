"""Opt-in, canonical 3D centre-lines; these records never authorise placement.

Units are millimetres/radians. A single record is ONE physical bar, even when it
has two horizontal legs. Demand credit is explicitly assigned to the main leg,
not inferred from projected length, total cut length, or the opposite-face leg.
The public legacy ``models.py`` and straight-bar contracts remain unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass

from rebar.models import Direction

Point3D = tuple[float, float, float]


@dataclass(frozen=True)
class Line3D:
    start_mm: Point3D
    end_mm: Point3D


@dataclass(frozen=True)
class Arc3D:
    center_mm: Point3D
    start_mm: Point3D
    # This initial contract supports cardinal unit normals only. The scoped
    # edge forms lie in an XZ/YZ plane; no arbitrary 3D spline approximation.
    normal_unit: Point3D
    sweep_rad: float


Curve3D = Line3D | Arc3D


@dataclass(frozen=True)
class ShapedPhysicalBar:
    id: str
    direction: Direction
    steel_class: str
    diameter_mm: int
    source_bar_ids: tuple[str, ...]
    segments: tuple[Curve3D, ...]
    shape_kind: str  # straight / G / U
    demand_segment_indexes: tuple[int, ...]
    geometry_profile_id: str
    placement_profile_id: str
    selected_cut_length_mm: float


@dataclass(frozen=True)
class ShapeRuleSource:
    document: str
    location: str
    rule: str


@dataclass(frozen=True)
class EdgeShapeProfile:
    id: str
    allowed_diameters_mm: tuple[int, ...]
    minimum_mandrel_diameters: float
    return_external_diameters: float
    control_anchor_diameters: float
    minimum_return_thicknesses: float
    maximum_cut_length_mm: float
    sources: tuple[ShapeRuleSource, ...]
    engineering_assumptions: tuple[str, ...]


K09_U_RETURN_50D_PROFILE = EdgeShapeProfile(
    id="k09-u-return-50d-sp63-2018/research-v1",
    allowed_diameters_mm=(10, 12, 16),
    minimum_mandrel_diameters=5.0,
    return_external_diameters=50.0,
    control_anchor_diameters=40.0,
    minimum_return_thicknesses=2.0,
    maximum_cut_length_mm=11700.0,
    sources=(
        ShapeRuleSource(
            "ППТ8-1-Д2-Р-9-КЖ2.3-14.pdf", "PDF pages 9–10, drawing sheets 7–8",
            "U details Ø10/12/16: external return 500/600/800 mm. "
            "External bridge 130 mm is NOT copied into a host with different cover.",
        ),
        ShapeRuleSource(
            "СП 63.13330.2018, published 2019 scan", "10.3.33",
            "For deformed bars d<20 mm: inner mandrel diameter >=5d; "
            "centre-line radius is (mandrel+d)/2, not 5d.",
        ),
        ShapeRuleSource(
            "СП 63.13330.2018, published 2019 scan", "10.4.9, figure 10.1(a)",
            "Flat-slab end U-bars and leg projection >=2h. "
            "The implementation conservatively checks the straight return against 2h.",
        ),
        ShapeRuleSource(
            "Техническое_задание_А101_ТЗ_Техлаб_2026.pdf", "PDF pages 7, 11, 14",
            "40d is retained as a control length, not a calculated bent anchorage capacity.",
        ),
        ShapeRuleSource(
            "A101 post-meeting message", "11700 mm stock clarification",
            "Stock cutting uses true centre-line cut lengths including arcs; zero kerf "
            "and mixed cuts remain separately declared manufacturing assumptions.",
        ),
    ),
    engineering_assumptions=(
        "The return is an anchoring node in the opposite zone; compression state, "
        "confinement, materials and anchorage capacity have NOT been verified.",
        "No arc, bridge, or return receives original horizontal FE demand credit.",
        "Main/return Z and the whole-plate X/Y order must be explicitly supplied; "
        "no existing reinforcement inventory is inferred.",
        "Published SP edition is the source of this research profile; all later "
        "amendments and material-specific bending requirements are not certified.",
        "The drawing uses external dimension sums; chosen stock length is instead "
        "the actual arc-inclusive centre-line length, so the main external leg changes.",
    ),
)


@dataclass(frozen=True)
class ShapedBarBuildResult:
    status: str
    bar: ShapedPhysicalBar | None
    report: dict


@dataclass(frozen=True)
class CertifiedCurveChord:
    segment_index: int
    start_mm: Point3D
    end_mm: Point3D
    maximum_deviation_mm: float
    # Contains the WHOLE curve piece, not just sampled points. Constructed as
    # chord bbox + sagitta, intersected with analytic whole-segment extrema.
    centerline_bounds_mm: tuple[Point3D, Point3D]
