"""Fresh-source adapter for joint small-opening correction of a complete plan."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib

from shapely.affinity import translate

from rebar.optimization.algorithms.opening_relocation import relocate_small_openings
from rebar.optimization.contracts.opening_relocation import SourceServiceLane
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS

from .physical_bar_trial import _revalidate_sources, build_physical_bar_trial
from .physical_layout_recovery import PhysicalLayoutRecoveryResult, _bytes, _raw, _source_bars
from .working_solid_host import _finite, inspect_working_solid


@dataclass(frozen=True)
class OpeningRelocationDraft:
    draft: dict
    review: dict
    draft_bytes: bytes
    review_bytes: bytes


def source_service_lanes(report, problem, *, candidate_index=0):
    """Reconstruct finite service widths from original periodic patterns, not user flags."""
    digest = hashlib.sha256(_bytes(report)).hexdigest()
    sources = _source_bars(report, problem, candidate_index, digest)
    _, _, _, retained, _ = _revalidate_sources(report, problem, candidate_index, digest)
    drafts = {(row["direction"], z["source_zone_id"]): z for row in retained for z in row["zone_drafts"]}
    result = []
    for source in sources:
        zone_id, component_index, bar_index = source.id.rsplit("/", 2)
        component = next(c for c in drafts[(str(source.direction), zone_id)]["components"]
                         if c["component_index"] == int(component_index))
        placement = component["placement"]
        period, offsets = placement["pattern"]["period_mm"], placement["pattern"]["offsets_mm"]
        q, origin = source.transverse_axis_mm, placement["origin_mm"]
        gaps = []
        for before in (True, False):
            values = [((q-origin-offset) if before else (origin+offset-q)) % period for offset in offsets]
            gaps.append(min(period if min(v, period-v) <= 1e-6 else v for v in values)/2)
        result.append(SourceServiceLane(source, zone_id, int(component_index), int(bar_index),
            component["nominal_step_mm"], tuple(component["axis_window_mm"]), tuple(gaps)))
    return tuple(result)


def correct_small_openings(recovery, original_problem, report, *, config,
    offset_x_mm, offset_y_mm, binding_source, source_report_sha256, stock_time_limit_s=10):
    """Return a NEW explicit execution draft; never forge legacy unchanged-axis certificates.

The old Revit physical trial is deliberately kept intact. Graphic preview may show
the new draft; structural placement needs a corresponding execution validator.
All host, source, counts, stock, coverage and pair checks run on the whole plan.
    """
    if not isinstance(recovery, PhysicalLayoutRecoveryResult) or recovery.packet is None:
        raise ValueError("Complete previously checked physical plan required")
    if (not isinstance(binding_source, str) or not binding_source.strip()
            or not isinstance(source_report_sha256, str) or len(source_report_sha256) != 64
            or any(c not in "0123456789abcdef" for c in source_report_sha256)):
        raise ValueError("Explicit binding source and exact external host-report SHA256 required")
    for value, content in ((recovery.packet, recovery.packet_bytes), (recovery.review, recovery.review_bytes),
        (recovery.patterned_report, recovery.patterned_report_bytes),
        (recovery.normalization_report, recovery.normalization_report_bytes)):
        if _bytes(value) != content:
            raise ValueError("Recovery data changed from its bound bytes")
    source_hash = hashlib.sha256(recovery.patterned_report_bytes).hexdigest()
    raw_hash = hashlib.sha256(recovery.normalization_report_bytes).hexdigest()
    packet_hash = hashlib.sha256(recovery.packet_bytes).hexdigest()
    if (recovery.packet["source_report_sha256"] != source_hash or recovery.packet["raw_report_sha256"] != raw_hash
            or recovery.review["packet_sha256"] != packet_hash):
        raise ValueError("Recovery hash chain differs")
    raw = recovery.normalization_report["accepted"]["raw_bars_by_direction"]
    initial = build_physical_bar_trial(recovery.patterned_report, raw, original_problem=original_problem,
        source_report_sha256=source_hash, raw_report_sha256=raw_hash, stock_time_limit_s=stock_time_limit_s)
    for key in ("directions", "source_zones", "expected", "manual_joint_tasks"):
        if initial.packet[key] != recovery.packet[key]:
            raise ValueError("Original physical packet does not reproduce independently")
    dx, dy = _finite(offset_x_mm), _finite(offset_y_mm)
    host, _ = inspect_working_solid(report)
    # Move the read-only host copy into SOURCE coordinates, never move/clip source FE.
    host = replace(host, sections=tuple(replace(s, footprint=translate(s.footprint, -dx, -dy)) for s in host.sections))
    lanes = source_service_lanes(recovery.patterned_report, original_problem)
    bars = tuple(PhysicalBar(b["id"], direction, b["steel_class"], b["diameter_mm"], b["coordinate_mm"],
        tuple(b["longitudinal_mm"]), tuple(b["source_bar_ids"]))
        for direction in PLATE_DIRECTIONS for b in raw[str(direction)])
    corrected = relocate_small_openings(bars, lanes, original_problem, host, config=config)
    review = deepcopy(corrected.review)
    # Independent original stock certificate remains applicable only because the
    # checker proved the entire per-direction material/length/count multiset equal.
    review.update(schema_version="small-opening-relocation-review/v1", source_packet_sha256=packet_hash,
        source_host_report_sha256=source_report_sha256, source_report_sha256=source_hash,
        source_to_revit_xy_mm=[dx, dy], binding_source=binding_source,
        stock_cutting=deepcopy(initial.review["stock_cutting"]),
        structural_revit_transport_status="not_supported_by_legacy_unchanged_axis_packet",
        original_source_zone_count=initial.packet["expected"]["source_zone_count"])
    draft = {"schema_version": "physical-bar-relocation-draft/v1", "units": "mm",
        "placement_eligible": False, "case_id": recovery.packet["case_id"],
        "source_packet_sha256": packet_hash, "source_report_sha256": source_hash,
        "source_host_report_sha256": source_report_sha256, "source_to_revit_xy_mm": [dx, dy],
        "binding_source": binding_source, "raw_bars_by_direction": _raw(corrected.bars),
        "original_source_zones": deepcopy(recovery.packet["source_zones"]),
        "expected": {key: recovery.packet["expected"][key] for key in
                     ("physical_bar_count", "additional_mass_kg", "source_zone_count", "position_count")},
        "status": "blocked_working_host" if review["host_blocked_after"] else "checked_research_draft",
        "structural_placement_supported": False,
        "correction": {key: review[key] for key in ("policy_id", "moved_bar_count", "moves",
                       "host_blocked_before", "host_blocked_after", "new_same_direction_body_pairs")}}
    draft_bytes = _bytes(draft)
    review["draft_sha256"] = hashlib.sha256(draft_bytes).hexdigest()
    return OpeningRelocationDraft(draft, review, draft_bytes, _bytes(review))
