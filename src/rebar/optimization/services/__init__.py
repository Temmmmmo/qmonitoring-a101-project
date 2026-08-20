"""Общие вычисления, которыми алгоритмы пользуются на равных условиях."""

from .anchorage import AnchoragePolicy, FixedDiameterAnchoragePolicy
from .bar_geometry import (
    BarSegment,
    bar_coordinates,
    bar_segments,
    bars_conflict,
    coverage_bbox,
    longitudinal_interval,
    transverse_axis_gap,
    transverse_interval,
    zone_coverage_bbox,
)
from .cutting import (
    PLATE_11700_CATALOG,
    PLATE_11700_CUT_LENGTHS_MM,
    CutLengthCatalog,
    select_installed_length_mm,
)
from .detailing import (
    STEEL_KG_PER_M_PER_MM2,
    DetailingContext,
    build_zone,
    build_zone_from_bbox,
    demanded_cells,
    prepare_detailing,
    rebar_mass_kg,
)
from .evaluation import evaluate_layout, partition_zones_conflict, zones_conflict
from .geometry import (
    bboxes_distance,
    bboxes_overlap,
    cell_bbox,
    point_in_bbox,
    polygon_area,
    polygon_bbox_intersection_area,
    polygon_bboxes_union_intersection_area,
    polygon_in_bbox,
)
from .phase import feasible_first_bar_coordinates, resolve_zone_phases

__all__ = [
    "PLATE_11700_CATALOG",
    "PLATE_11700_CUT_LENGTHS_MM",
    "STEEL_KG_PER_M_PER_MM2",
    "AnchoragePolicy",
    "BarSegment",
    "CutLengthCatalog",
    "DetailingContext",
    "FixedDiameterAnchoragePolicy",
    "bar_coordinates",
    "bar_segments",
    "bars_conflict",
    "coverage_bbox",
    "bboxes_distance",
    "bboxes_overlap",
    "build_zone",
    "build_zone_from_bbox",
    "cell_bbox",
    "demanded_cells",
    "evaluate_layout",
    "feasible_first_bar_coordinates",
    "longitudinal_interval",
    "point_in_bbox",
    "polygon_area",
    "polygon_bbox_intersection_area",
    "polygon_bboxes_union_intersection_area",
    "polygon_in_bbox",
    "partition_zones_conflict",
    "prepare_detailing",
    "rebar_mass_kg",
    "resolve_zone_phases",
    "select_installed_length_mm",
    "transverse_axis_gap",
    "transverse_interval",
    "zone_coverage_bbox",
    "zones_conflict",
]
