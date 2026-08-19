"""Общие вычисления, которыми алгоритмы пользуются на равных условиях."""

from .detailing import (
    STEEL_KG_PER_M_PER_MM2,
    DetailingContext,
    build_zone,
    demanded_cells,
    prepare_detailing,
    rebar_mass_kg,
)
from .evaluation import evaluate_layout, zones_conflict
from .geometry import (
    bboxes_distance,
    bboxes_overlap,
    cell_bbox,
    point_in_bbox,
    polygon_area,
    polygon_bbox_intersection_area,
    polygon_in_bbox,
)

__all__ = [
    "STEEL_KG_PER_M_PER_MM2",
    "DetailingContext",
    "bboxes_distance",
    "bboxes_overlap",
    "build_zone",
    "cell_bbox",
    "demanded_cells",
    "evaluate_layout",
    "point_in_bbox",
    "polygon_area",
    "polygon_bbox_intersection_area",
    "polygon_in_bbox",
    "prepare_detailing",
    "rebar_mass_kg",
    "zones_conflict",
]
