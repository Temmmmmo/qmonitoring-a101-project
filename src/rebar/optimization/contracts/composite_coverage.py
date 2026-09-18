"""Результат ограниченной проверки покрытия, не разрешение на инженерный выпуск."""
from __future__ import annotations

from dataclasses import dataclass

from .problem import BBox

COMPOSITE_COVERAGE_POLICY = "a101-247-ordered-recipe-voronoi/research-v1"
STO_279_COVERAGE_POLICY = "a101-sto-279-ordered-recipe-contact/research-v1"
MONOTONE_SINGLE_STO_COVERAGE_POLICY = "a101-sto-279-monotone-single-addition/research-v1"
MONOTONE_COMPONENT_STO_COVERAGE_POLICY = "a101-sto-279-monotone-ordered-components/research-v1"


@dataclass(frozen=True)
class CompositeCellCoverage:
    cell_id: int
    level_index: int
    area_mm2: float
    covered_area_mm2: float
    uncovered_area_mm2: float
    covered: bool
    contributing_zone_ids: tuple[str, ...]


@dataclass(frozen=True)
class CompositeCoverageZoneCheck:
    zone_id: str
    geometry_and_pattern_valid: bool
    # По компонентам; не площадь тела стержня и не граница Revit-host.
    component_service_bboxes_mm: tuple[BBox, ...]
    physical_bar_count: int | None
    additional_mass_kg: float | None
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class CompositeCoverageEvaluation:
    policy_id: str
    # pass относится ТОЛЬКО к явно названной исследовательской модели покрытия.
    status: str
    geometry_and_patterns_valid: bool
    coverage_passed: bool
    demanded_cell_count: int
    covered_cell_count: int
    uncovered_cell_count: int
    uncovered_area_mm2: float
    zone_count: int
    component_count: int
    physical_bar_count: int | None
    additional_mass_kg: float | None
    cells: tuple[CompositeCellCoverage, ...]
    zones: tuple[CompositeCoverageZoneCheck, ...]
    diagnostics: tuple[str, ...]
    remaining_check_ids: tuple[str, ...]
    placement_eligible: bool = False
