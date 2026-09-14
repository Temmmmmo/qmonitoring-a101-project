"""Неизменяемый числовой источник без искусственных цветов и Revit-привязки."""
from __future__ import annotations

from dataclasses import dataclass

from rebar.models import Axis, Direction, Layer

AS_DIRECTIONS = (
    Direction(Layer.BOTTOM, Axis.X), Direction(Layer.TOP, Axis.X),
    Direction(Layer.BOTTOM, Axis.Y), Direction(Layer.TOP, Axis.Y),
)
IMPORT_PROFILE = "lira-a101-plates-first-row-v1"


@dataclass(frozen=True)
class LiraSource:
    role: str
    filename: str
    sha256: str
    sheet: str
    row_count: int


@dataclass(frozen=True)
class LiraNode:
    id: int
    xyz_mm: tuple[float, float, float]
    source_row: int


@dataclass(frozen=True)
class LiraGroup:
    id: int
    calculation_thickness_mm: float
    concrete_class: str
    steel_class_x: str
    steel_class_y: str
    source_step_mm: float
    source_row: int
    source_text: str


@dataclass(frozen=True)
class LiraElement:
    id: int
    element_type: int
    stiffness_id: int
    local_axis_angle_deg: float | None
    node_ids: tuple[int, ...]
    vertices_mm: tuple[tuple[float, float, float], ...]
    group_id: int
    # AS1, AS2, AS3, AS4: экспортные оси; не разрешение привязки к осям Revit.
    total_as_cm2_m: tuple[float, float, float, float]
    second_row_as_cm2_m_unassigned: tuple[float, float, float, float]
    geometry_row: int
    reinforcement_rows: tuple[int, int]

    def required_as(self, direction: Direction) -> float:
        return self.total_as_cm2_m[AS_DIRECTIONS.index(direction)]

    @property
    def polygon_xy_mm(self) -> tuple[tuple[float, float], ...]:
        # FE44 exports tensor-product node order, not a perimeter walk.
        order = (0, 1, 3, 2) if self.element_type == 44 else (0, 1, 2)
        return tuple(self.vertices_mm[i][:2] for i in order)


@dataclass(frozen=True)
class LiraPlate:
    sources: tuple[LiraSource, ...]
    nodes: tuple[LiraNode, ...]
    groups: tuple[LiraGroup, ...]
    elements: tuple[LiraElement, ...]
    bbox_xyz_mm: tuple[tuple[float, float, float], tuple[float, float, float]]
    input_coordinate_unit: str
    unused_node_ids: tuple[int, ...]
    profile_id: str = IMPORT_PROFILE
