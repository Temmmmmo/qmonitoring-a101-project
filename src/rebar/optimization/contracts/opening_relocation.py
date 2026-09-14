"""Explicit straight-bar execution correction; source LayoutZones stay unchanged."""
from __future__ import annotations

from dataclasses import dataclass

from .physical import PhysicalBar, PhysicalSourceBar


@dataclass(frozen=True)
class SourceServiceLane:
    source: PhysicalSourceBar
    zone_id: str
    component_index: int
    bar_index: int
    nominal_step_mm: int
    axis_window_mm: tuple[float, float]
    # Half-distances to the neighbours of the ORIGINAL infinite STO pattern.
    service_half_widths_mm: tuple[float, float]


@dataclass(frozen=True)
class OpeningRelocationConfig:
    # Search bounds, NOT new engineering maximum spacing rules.
    maximum_shift_mm: float = 300.0
    maximum_candidates_per_bar: int = 32
    maximum_total_candidates: int = 12000
    maximum_coverage_atoms: int = 100000
    maximum_pair_checks: int = 2000000
    time_limit_s: float = 30.0


@dataclass(frozen=True)
class OpeningRelocationResult:
    bars: tuple[PhysicalBar, ...]
    review: dict
    placement_eligible: bool = False


class OpeningRelocationLimitError(ValueError):
    """A bounded verification cannot finish; never accept partial verification."""
