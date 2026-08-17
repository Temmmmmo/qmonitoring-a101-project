"""Общие вычисления, которыми алгоритмы пользуются на равных условиях."""

from .detailing import (
    STEEL_KG_PER_M_PER_MM2,
    DetailingContext,
    build_zone,
    demanded_cells,
    prepare_detailing,
)
from .evaluation import evaluate_layout
from .geometry import bboxes_overlap, cell_bbox, point_in_bbox, polygon_area

__all__ = [
    "STEEL_KG_PER_M_PER_MM2",
    "DetailingContext",
    "bboxes_overlap",
    "build_zone",
    "cell_bbox",
    "demanded_cells",
    "evaluate_layout",
    "point_in_bbox",
    "polygon_area",
    "prepare_detailing",
]
