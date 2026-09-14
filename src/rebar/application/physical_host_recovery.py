"""Whole-plan host fitting followed by fresh source, stock and geometry checks.

This is deliberately not clipping or permission to place a partial plan. Bars
which cannot fit retain their original geometry and remain visible blockers.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib

from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS, PlateProblem
from rebar.optimization.services.physical_host_fit import fit_physical_bar_to_solid_host
from rebar.reporting.serialization import to_jsonable

from .physical_bar_trial import build_physical_bar_trial
from .physical_layout_recovery import PhysicalLayoutRecoveryResult, _bytes, _raw, _source_bars
from .working_solid_host import _finite, inspect_working_solid, review_working_solid_bars


@dataclass(frozen=True)
class PhysicalHostRecoveryResult:
    recovery: PhysicalLayoutRecoveryResult
    host_review: dict
    host_review_bytes: bytes


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _pairs(packet):
    """Stable physical IDs, independent of regrouped Revit run numbering."""
    ids = {(d["direction"], run["id"], index): bar["bar_id"]
           for d in packet["directions"] for run in d["runs"]
           for index, bar in enumerate(run["bar_sources"])}
    return {(t["direction"], *sorted(ids[(t["direction"], t[side]["run_id"], t[side]["bar_index"])]
                                   for side in ("first", "second"))) for t in packet["manual_joint_tasks"]}


def _translate(bar, dx, dy, *, source=False, reverse=False):
    along, across = (dx, dy) if str(bar.direction).endswith("X") else (dy, dx)
    sign = -1 if reverse else 1
    values = {"transverse_axis_mm": bar.transverse_axis_mm + sign * across,
              "installed_interval_mm": tuple(v + sign * along for v in bar.installed_interval_mm)}
    if source:
        values.update(required_interval_mm=tuple(v + sign * along for v in bar.required_interval_mm),
                      background_origin_mm=bar.background_origin_mm + sign * across)
    return replace(bar, **values)


def fit_physical_layout_to_host(
    recovery: PhysicalLayoutRecoveryResult, original_problem: PlateProblem, report: dict, *,
    offset_x_mm: float, offset_y_mm: float, binding_source: str,
    axis_depths_mm: dict | None = None, conservative_whole_height: bool = False,
    source_report_sha256: str, candidate_index: int = 0, stock_time_limit_s: float = 10,
) -> PhysicalHostRecoveryResult:
    """Fit only surplus length; preserve all original FE and every source owner.

    Exactly one vertical profile must be chosen. Whole-height mode is conservative
    XY containment, NOT verification of actual Z or cross-direction intersections.
    Moves introducing new same-plane body pairs are reverted, retaining the other
    improvements. The bounded repair falls back to the whole incumbent if needed.
    The caller supplies the digest of the exact external host-report bytes.
    """
    if not isinstance(recovery, PhysicalLayoutRecoveryResult) or recovery.packet is None:
        raise ValueError("A complete independently checked physical recovery is required")
    if (not isinstance(source_report_sha256, str) or len(source_report_sha256) != 64
            or any(c not in "0123456789abcdef" for c in source_report_sha256)):
        raise ValueError("An exact host report SHA256 is required")
    if type(conservative_whole_height) is not bool or ((axis_depths_mm is not None) == conservative_whole_height):
        raise ValueError("Choose explicit four depths OR conservative whole-height fitting")
    for value, content in ((recovery.patterned_report, recovery.patterned_report_bytes),
                           (recovery.normalization_report, recovery.normalization_report_bytes),
                           (recovery.packet, recovery.packet_bytes), (recovery.review, recovery.review_bytes)):
        if _bytes(value) != content:
            raise ValueError("Recovery dictionaries differ from their bound bytes")
    source_hash, raw_hash = _sha(recovery.patterned_report_bytes), _sha(recovery.normalization_report_bytes)
    if (recovery.packet["source_report_sha256"] != source_hash
            or recovery.packet["raw_report_sha256"] != raw_hash
            or recovery.review["packet_sha256"] != _sha(recovery.packet_bytes)):
        raise ValueError("Recovery hash chain does not match")
    dx, dy = _finite(offset_x_mm), _finite(offset_y_mm)
    before = review_working_solid_bars(report, recovery.packet, offset_x_mm=dx, offset_y_mm=dy,
        binding_source=binding_source, axis_depths_mm=axis_depths_mm)
    host, _ = inspect_working_solid(report)
    sources = _source_bars(recovery.patterned_report, original_problem, candidate_index, source_hash)
    translated_sources = tuple(_translate(s, dx, dy, source=True) for s in sources)
    normal = deepcopy(recovery.normalization_report)
    original_raw = normal["accepted"]["raw_bars_by_direction"]
    initial = build_physical_bar_trial(recovery.patterned_report, original_raw,
        original_problem=original_problem, source_report_sha256=source_hash, raw_report_sha256=raw_hash,
        candidate_index=candidate_index, stock_time_limit_s=stock_time_limit_s)
    for key in ("directions", "source_zones", "expected", "manual_joint_tasks"):
        if initial.packet[key] != recovery.packet[key]:
            raise ValueError("Original recovery packet does not reproduce independently")
    proposed, rows = [], []
    for direction in PLATE_DIRECTIONS:
        name = str(direction)
        z = None if axis_depths_mm is None else (
            host.sections[0].bottom_z_mm + axis_depths_mm[name] if name.startswith("bottom")
            else host.sections[-1].top_z_mm - axis_depths_mm[name])
        for raw in original_raw[name]:
            original = PhysicalBar(raw["id"], direction, raw["steel_class"], raw["diameter_mm"],
                raw["coordinate_mm"], tuple(raw["longitudinal_mm"]), tuple(raw["source_bar_ids"]))
            fit = fit_physical_bar_to_solid_host(_translate(original, dx, dy), translated_sources, host,
                axis_z_mm=z, conservative_whole_height=conservative_whole_height)
            bar = _translate(fit.bar, dx, dy, reverse=True) if fit.status == "fitted" else original
            proposed.append(bar)
            rows.append(to_jsonable(fit))
    normal["accepted"] = {"raw_bars_by_direction": _raw(proposed)}
    host_fit = {"schema_version": "physical-host-fit-review/v1", "placement_eligible": False,
        "engineering_approval": False, "source_demand_removed": False,
        "source_host_report_sha256": source_report_sha256, "input_packet_sha256": _sha(recovery.packet_bytes),
        "source_to_revit_xy_mm": [dx, dy], "binding_source": binding_source,
        "axis_depths_mm": axis_depths_mm, "conservative_whole_height": conservative_whole_height,
        "proposal": {"fitted_bar_count": sum(r["status"] == "fitted" for r in rows),
            "blocked_bar_count": sum(r["status"] == "blocked" for r in rows), "bars": rows},
        "proposal_accepted": True, "rejected_moved_bar_ids": [], "repair_trace": [],
        "source_axes_and_full_40d_preserved": True,
        "same_plane_new_pairs_allowed": False, "actual_cross_direction_3d_checked": False}
    normal["host_fit"] = host_fit
    # Keep pre-fit normalizer telemetry explicitly historical, not current axes.
    if "normalization" in normal:
        normal["pre_host_fit_normalization"] = normal.pop("normalization")

    def rebuild():
        return build_physical_bar_trial(recovery.patterned_report, normal["accepted"]["raw_bars_by_direction"],
            original_problem=original_problem, source_report_sha256=source_hash,
            raw_report_sha256=_sha(_bytes(normal)), candidate_index=candidate_index,
            stock_time_limit_s=stock_time_limit_s)

    trial = rebuild()
    original_pairs = _pairs(initial.packet)
    initial_bars = {(name, b["id"]): b for name, bars in original_raw.items() for b in bars}
    changed = {(name, b["id"]) for name, bars in normal["accepted"]["raw_bars_by_direction"].items()
               for b in bars if b != initial_bars[(name, b["id"])]}
    rejected = set()
    for iteration in range(9):
        new_pairs = _pairs(trial.packet) - original_pairs
        if not new_pairs:
            break
        revert = {(direction, bar_id) for direction, first, second in new_pairs
                  for bar_id in (first, second)} & changed
        if not revert:
            raise ValueError("Independent checker reports a new intersection between unchanged bars")
        if iteration == 8:
            revert = set(changed)  # bounded fallback, never suppress a new conflict
        host_fit.update(proposal_accepted=False, rejection_reason="new_same_plane_body_intersections")
        host_fit["repair_trace"].append({"new_pairs": [list(p) for p in sorted(new_pairs)],
            "reverted_bar_ids": [list(key) for key in sorted(revert)], "complete_fallback": iteration == 8})
        for name, bars in normal["accepted"]["raw_bars_by_direction"].items():
            for index, bar in enumerate(bars):
                if (name, bar["id"]) in revert:
                    bars[index] = deepcopy(initial_bars[(name, bar["id"])])
        rejected.update(revert)
        changed.difference_update(revert)
        trial = rebuild()
    if _pairs(trial.packet) - original_pairs:
        raise ValueError("New same-plane intersections remain after bounded fit repair")
    host_fit["rejected_moved_bar_ids"] = [list(key) for key in sorted(rejected)]
    for key in ("physical_bar_count", "source_zone_count", "position_count"):
        if trial.packet["expected"][key] != initial.packet["expected"][key]:
            raise ValueError("Host fitting changed dimensions, inventory or source zones")
    if abs(trial.packet["expected"]["additional_mass_kg"] - initial.packet["expected"]["additional_mass_kg"]) > 1e-6:
        raise ValueError("Host fitting changed the complete physical mass")
    after = review_working_solid_bars(report, trial.packet, offset_x_mm=dx, offset_y_mm=dy,
        binding_source=binding_source, axis_depths_mm=axis_depths_mm)
    criterion = "outside_solid_with_cover" if axis_depths_mm is not None else "outside_full_height_common_footprint"
    before_count, after_count = (check["bar_check"]["totals"][criterion] for check in (before, after))
    if after_count > before_count:
        raise ValueError("Independent host check worsened; no result exported")
    host_fit.update(accepted_moved_bar_count=len(changed),
                    check_criterion=criterion, blocked_before=before_count, blocked_after=after_count,
                    same_plane_pairs_before=len(_pairs(initial.packet)), same_plane_pairs_after=len(_pairs(trial.packet)))
    normal.update(status="host_fit_blocked" if after_count else "host_fit_checked_under_explicit_profile",
                  accepted_metrics=deepcopy(trial.packet["expected"]))
    normal_bytes = _bytes(normal)
    packet, review = deepcopy(trial.packet), deepcopy(trial.review)
    packet["raw_report_sha256"] = _sha(normal_bytes)
    packet["source_blockers"] = sorted(set(packet["source_blockers"]) | set(recovery.packet["source_blockers"]) |
        ({"working-host-fit-incomplete"} if after_count else set()))
    packet_bytes = _bytes(packet)
    review.update(raw_report_sha256=_sha(normal_bytes), packet_sha256=_sha(packet_bytes),
        source_provenance=deepcopy(recovery.review.get("source_provenance", {})),
        permanent_blockers=deepcopy(packet["source_blockers"]), host_fit=deepcopy(host_fit),
        normalization_status=normal["status"])
    for key in ("normalization_mass_limit", "engineer_comparison"):
        if key in recovery.review:
            review[key] = deepcopy(recovery.review[key])
    for check in (before, after):
        check["bar_check"]["packet_source_certificates_checked"] = True
    host_review = {**deepcopy(host_fit), "before": before, "after": after,
                   "output_packet_sha256": _sha(packet_bytes)}
    status = "blocked_working_host" if after_count else (
        "prepared_with_manual_tasks" if packet["manual_joint_tasks"] else "prepared_rollback_only")
    updated = PhysicalLayoutRecoveryResult(status, recovery.patterned_report, normal, packet, review,
        recovery.patterned_report_bytes, normal_bytes, packet_bytes, _bytes(review))
    return PhysicalHostRecoveryResult(updated, host_review, _bytes(host_review))
