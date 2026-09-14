"""Opt-in demand-transfer records; original FE/model contracts remain unchanged.

Intensity is additional steel area per transverse millimetre [mm²/mm]. Its
transverse line integral is steel area [mm²], NOT installed reinforcement mass.
"""
from __future__ import annotations

from dataclasses import dataclass

from rebar.models import Direction

Point = tuple[float, float]


@dataclass(frozen=True)
class DemandTransferPolygon:
    exterior_mm: tuple[Point, ...]
    holes_mm: tuple[tuple[Point, ...], ...] = ()


@dataclass(frozen=True)
class TransverseDemandTransferPiece:
    id: str
    source_cell_id: int
    source_polygon: DemandTransferPolygon
    # s' = s, q' = scale*q + offset; positive scale preserves station integrals.
    transverse_scale: float
    transverse_offset_mm: float


@dataclass(frozen=True)
class DemandTransferProfile:
    id: str = "local-transverse-additions-only-prescribed-background/research-v1"
    maximum_transverse_span_mm: float = 600.0
    recipient_clearance_mm: float = 33.0
    maximum_source_cells: int = 10000
    maximum_vertices: int = 100000
    maximum_transfer_pieces: int = 20000
    maximum_target_patches: int = 50000
    maximum_overlay_pairs: int = 100000


@dataclass(frozen=True)
class SourceDemandTransfer:
    case_id: str
    direction: Direction
    source_snapshot_sha256: str
    source_demand_sha256: str
    host_report_sha256: str
    host_geometry_sha256: str
    profile: DemandTransferProfile
    pieces: tuple[TransverseDemandTransferPiece, ...]
    schema_version: str = "source-demand-transfer/v1"


@dataclass(frozen=True)
class TransferredDemandPatch:
    id: str
    source_cell_id: int
    polygon: DemandTransferPolygon
    required_additional_as_mm2_per_mm: float
    origin: str  # retained_source or transferred_source


@dataclass(frozen=True)
class DemandTransferSupplyOffer:
    id: str
    polygon: DemandTransferPolygon
    supplied_additional_as_mm2_per_mm: float


@dataclass(frozen=True)
class CheckedDemandTransfer:
    certificate: SourceDemandTransfer
    target_patches: tuple[TransferredDemandPatch, ...]
    report: dict
