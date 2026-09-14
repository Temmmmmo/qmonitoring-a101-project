"""Opt-in physical-bar normalization; never a production placement approval.

One source record denotes one actual additional bar, not an equivalent nominal
spacing or a demand cell. Required intervals exclude anchorage; installed intervals
include it. Source IDs are unique within their typed Direction.
"""

from __future__ import annotations

from dataclasses import dataclass

from rebar.models import Direction


@dataclass(frozen=True)
class PhysicalSourceBar:
    id: str
    direction: Direction
    steel_class: str
    diameter_mm: int
    transverse_axis_mm: float
    installed_interval_mm: tuple[float, float]
    required_interval_mm: tuple[float, float]
    background_diameter_mm: int
    background_origin_mm: float
    background_step_mm: float = 300.0


@dataclass(frozen=True)
class PhysicalBar:
    id: str
    direction: Direction
    steel_class: str
    diameter_mm: int
    transverse_axis_mm: float
    installed_interval_mm: tuple[float, float]
    source_bar_ids: tuple[str, ...]

    @property
    def installed_length_mm(self) -> float:
        return self.installed_interval_mm[1] - self.installed_interval_mm[0]


@dataclass(frozen=True)
class PhysicalNormalizationConfig:
    # Diameter substitution is a separate engineering assumption, never implicit.
    allow_diameter_increase: bool = False
    # None forbids any increase above the complete physical input's mass.
    maximum_mass_kg: float | None = None
    maximum_batch_mass_increase_pct: float = 5.0
    time_limit_s: float = 120.0
    stock_balance_time_limit_s: float = 30.0
    maximum_bars: int = 5000
    maximum_merge_operations: int = 5000
    maximum_pair_checks: int = 20_000_000
    maximum_exchange_attempts: int = 300


@dataclass(frozen=True)
class PhysicalNormalizationMetrics:
    mass_kg: float
    physical_bar_count: int
    position_count: int
    body_intersection_pair_count: int
    affected_bar_count: int


@dataclass(frozen=True)
class PhysicalConflictTask:
    direction: Direction
    first_bar_id: str
    second_bar_id: str
    transverse_axis_distance_mm: float
    longitudinal_overlap_interval_mm: tuple[float, float]
    mandatory_source_new40d_overlap_interval_mm: tuple[float, float] | None
    fixed_length_shift_cannot_separate: bool
    single_merged_full40d_minimum_length_mm: float
    exceeds_11700: bool
    engineering_resolution_required: bool = True


@dataclass(frozen=True)
class PhysicalNormalizationResult:
    bars: tuple[PhysicalBar, ...]
    metrics: PhysicalNormalizationMetrics
    stock_report: dict
    trace: tuple[dict, ...]
    source_certificate: dict
    unresolved_pairs: tuple[PhysicalConflictTask, ...]
    status: str
    budget_exhausted: bool
    placement_eligible: bool = False
    actual_3d_checked: bool = False
    host_checked: bool = False


class PhysicalNormalizationLimitError(ValueError):
    """Input/final verification cannot complete within an explicit resource bound."""
