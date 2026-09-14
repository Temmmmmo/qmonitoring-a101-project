"""Opt-in application pipeline from complete layout to a physical rollback trial.

No source material, intermediary artifact or CLI module is imported. Callers ingest
the original demand and choose the complete layout; this service preserves those
parameterized zones while deriving and validating a separate physical-bar plan.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math

from rebar.optimization.algorithms.physical_normalization import normalize_physical_bars
from rebar.optimization.contracts import PlateProblem, PlateSolution
from rebar.optimization.contracts.physical import PhysicalNormalizationConfig, PhysicalSourceBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS
from rebar.reporting.serialization import to_jsonable

from .analyze_composite_plate import CompositeDirectionSettings
from .patterned_layout_recovery import recover_patterned_layout
from .physical_bar_trial import _revalidate_sources, build_physical_bar_trial


@dataclass(frozen=True)
class PhysicalLayoutRecoveryResult:
    """All artifacts and their exact canonical UTF-8 bytes; no file-system writes.

    ``packet`` is None only when a complete source/physical stock plan cannot be
    independently certified within the stated constraints. A prepared packet is
    always diagnostic and rollback-only, including when manual tasks are empty.
    """

    status: str
    patterned_report: dict
    normalization_report: dict
    packet: dict | None
    review: dict
    patterned_report_bytes: bytes
    normalization_report_bytes: bytes
    packet_bytes: bytes | None
    review_bytes: bytes


def _bytes(value: dict) -> bytes:
    # Exactly the pyRevit serialize_report_utf8 representation; no trailing newline.
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _configuration(config, stock_time_limit_s):
    if not isinstance(config, PhysicalNormalizationConfig):
        raise ValueError("Explicit typed physical normalization configuration required")
    if (isinstance(stock_time_limit_s, bool) or not isinstance(stock_time_limit_s, (int, float))
            or not math.isfinite(stock_time_limit_s) or not 0.001 <= stock_time_limit_s <= 60):
        raise ValueError("stock_time_limit_s must be finite in 0.001..60")


def _provenance(value):
    if value is None:
        return {}
    if not isinstance(value, dict) or len(_bytes(value)) > 1024 * 1024:
        raise ValueError("Source provenance must be a finite JSON object up to 1 MiB")
    # Names are deliberately nested, never interpreted as layout permissions.
    return to_jsonable(deepcopy(value))


def _source_bars(report, problem, candidate_index, source_hash):
    """Extract only from independently reconstructed source zones and original FE."""
    certificates, _refs, _coverage, retained, _expected = _revalidate_sources(
        report, problem, candidate_index, source_hash)
    drafts = {(row["direction"], z["source_zone_id"]): z
              for row in retained for z in row["zone_drafts"]}
    directions = {str(d): d for d in PLATE_DIRECTIONS}
    bars = []
    for zone in certificates:
        draft = drafts[(zone["direction"], zone["zone_id"])]
        along = 0 if zone["direction"].endswith("X") else 1
        for component in zone["components"]:
            old = next(c for c in draft["components"] if c["component_index"] == component["component_index"])
            interval = (old["bar_axis_bbox_mm"][along], old["bar_axis_bbox_mm"][along + 2])
            for index, coordinate in enumerate(component["axis_coordinates_mm"]):
                bars.append(PhysicalSourceBar(
                    id=f"{zone['zone_id']}/{component['component_index']}/{index}",
                    direction=directions[zone["direction"]], steel_class=component["steel_class"],
                    diameter_mm=component["diameter_mm"], transverse_axis_mm=coordinate,
                    installed_interval_mm=interval, required_interval_mm=tuple(component["required_interval_mm"]),
                    background_diameter_mm=component["background_diameter_mm"],
                    background_origin_mm=component["background_origin_mm"],
                    background_step_mm=draft["background"]["specification"]["step"],
                ))
    return tuple(bars)


def _raw(bars, *, source=False):
    result = {str(direction): [] for direction in PLATE_DIRECTIONS}
    for bar in bars:
        result[str(bar.direction)].append({"id": bar.id, "steel_class": bar.steel_class,
            "diameter_mm": bar.diameter_mm, "coordinate_mm": bar.transverse_axis_mm,
            "longitudinal_mm": list(bar.installed_interval_mm),
            "source_bar_ids": [bar.id] if source else list(bar.source_bar_ids)})
    return result


def _blocked(patterned, normalization, review, status):
    patterned = to_jsonable(patterned)
    normalization = to_jsonable(normalization)
    review = to_jsonable(review)
    source_bytes = _bytes(patterned)
    normalization["source_report_sha256"] = _sha(source_bytes)
    normal_bytes = _bytes(normalization)
    review.update(schema_version="physical-layout-recovery-review/v1", placement_eligible=False,
                  engineering_approval=False, status=status, source_report_sha256=_sha(source_bytes),
                  raw_report_sha256=_sha(normal_bytes))
    return PhysicalLayoutRecoveryResult(status, patterned, normalization, None, review,
                                        source_bytes, normal_bytes, None, _bytes(review))


def recover_physical_layout_from_report(
    patterned_report: dict, original_problem: PlateProblem, *,
    normalization_config: PhysicalNormalizationConfig, source_provenance: dict | None = None,
    candidate_index: int = 0, stock_time_limit_s: float = 10,
) -> PhysicalLayoutRecoveryResult:
    """Resume a selected complete patterned report, freshly checking original FE.

    A bounded normalizer may return an unchanged incumbent. It still yields a
    complete diagnostic trial after independent source/stock checks; non-improvement
    is not an authorization failure. A rejected proposal is explicitly recorded and
    replaced only by the original complete physical inventory, never partial bars.
    """
    _configuration(normalization_config, stock_time_limit_s)
    if not isinstance(original_problem, PlateProblem) or not isinstance(patterned_report, dict):
        raise ValueError("Fresh typed original problem and patterned report required")
    if type(candidate_index) is not int or candidate_index < 0:
        raise ValueError("Explicit nonnegative candidate index required")
    report = to_jsonable(deepcopy(patterned_report))
    provenance = _provenance(source_provenance if source_provenance is not None
                             else report.get("source_provenance"))
    if source_provenance is not None or "source_provenance" in report:
        report["source_provenance"] = provenance
    source_bytes = _bytes(report)
    source_hash = _sha(source_bytes)
    sources = _source_bars(report, original_problem, candidate_index, source_hash)
    # Configuration/invalid source errors from the core are not hidden as a
    # successful fallback. The source has already passed fresh full coverage.
    normalized = normalize_physical_bars(sources, config=normalization_config)
    raw = _raw(normalized.bars)
    normalization = {"schema_version": "physical-layout-normalization/v1", "units": "mm",
        "placement_eligible": False, "engineering_approval": False, "source_demand_removed": False,
        "source_report_sha256": source_hash, "source_provenance": deepcopy(provenance),
        "configuration": to_jsonable(normalization_config), "status": normalized.status,
        "normalization": to_jsonable(normalized), "accepted": {"raw_bars_by_direction": raw},
        "application_trace": []}

    def build_current():
        normal_bytes = _bytes(normalization)
        return build_physical_bar_trial(report, normalization["accepted"]["raw_bars_by_direction"],
            original_problem=original_problem, source_report_sha256=source_hash,
            raw_report_sha256=_sha(normal_bytes), candidate_index=candidate_index,
            stock_time_limit_s=stock_time_limit_s)

    try:
        trial = build_current()
    except ValueError as error:
        # An unchanged full source is the only fallback; do not reinterpret failed
        # geometry, discard conflicts, or suppress the normalizer rejection reason.
        normalization["status"] = "normalization_rejected_source_retained"
        normalization["application_trace"].append({"stage": "independent_physical_validation",
            "status": "candidate_rejected", "reason": str(error)[:2000],
            "fallback": "complete_original_physical_inventory"})
        normalization["accepted"] = {"raw_bars_by_direction": _raw(sources, source=True)}
        try:
            trial = build_current()
        except ValueError as fallback_error:
            normalization["status"] = "blocked_complete_physical_validation"
            return _blocked(report, normalization, {"issues": [str(error), str(fallback_error)],
                "permanent_blockers": ["complete-physical-plan-validation", "permanent-placement"]},
                "blocked_complete_physical_validation")
    checked_normal_bytes = _bytes(normalization)
    if (trial.packet["source_report_sha256"] != source_hash
            or trial.packet["raw_report_sha256"] != _sha(checked_normal_bytes)
            or trial.review["packet_sha256"] != _sha(_bytes(trial.packet))):
        raise ValueError("Application report/physical packet byte hashes differ")
    # Distinguish rejected proposal metrics from the actual, independently checked
    # full source fallback. Never relabel a failed proposal's counts as accepted.
    if normalization["status"] == "normalization_rejected_source_retained":
        normalization["proposed_result"] = normalization.pop("normalization")
        normalization["proposed_result_status"] = "rejected_by_independent_physical_validation"
    normalization["accepted_metrics"] = deepcopy(trial.packet["expected"])
    limit = normalization_config.maximum_mass_kg
    if limit is None:
        limit = trial.review["prior_metrics"]["additional_mass_kg"]
    mass_limit = {"kg": limit, "actual_mass_kg": trial.packet["expected"]["additional_mass_kg"],
                  "status": "pass" if trial.packet["expected"]["additional_mass_kg"] <= limit + 1e-6 else "fail",
                  "scope": "normalization mass target; complete source fallback is diagnostic, not a passed target"}
    normalization["normalization_mass_limit"] = mass_limit
    normal_bytes = _bytes(normalization)
    # Final binding adds validated metrics to the report, not new geometry. Rebind
    # the hashes only after this immutable report content has been finalized.
    packet, review = deepcopy(trial.packet), deepcopy(trial.review)
    packet["raw_report_sha256"] = _sha(normal_bytes)
    if mass_limit["status"] != "pass":
        packet["source_blockers"] = sorted({*packet["source_blockers"], "normalization-mass-target-not-met"})
        review["permanent_blockers"] = sorted({*review["permanent_blockers"], "normalization-mass-target-not-met"})
    if len(packet["source_blockers"]) > 100:
        raise ValueError("Complete physical blocker inventory exceeds diagnostic packet budget")
    packet_bytes = _bytes(packet)
    review["raw_report_sha256"] = packet["raw_report_sha256"]
    review["packet_sha256"] = _sha(packet_bytes)
    review["normalization_mass_limit"] = deepcopy(mass_limit)
    review["normalization_status"] = normalization["status"]
    review["normalization_trace"] = to_jsonable(normalized.trace)
    review["normalization_application_trace"] = deepcopy(normalization["application_trace"])
    review["normalization_budget_exhausted"] = normalized.budget_exhausted
    review["source_provenance"] = deepcopy(provenance)
    status = "prepared_with_manual_tasks" if packet["manual_joint_tasks"] else "prepared_rollback_only"
    return PhysicalLayoutRecoveryResult(status, report, normalization, packet, review,
                                        source_bytes, normal_bytes, packet_bytes, _bytes(review))


def recover_physical_layout(
    problem: PlateProblem, solution: PlateSolution, settings: tuple[CompositeDirectionSettings, ...], *,
    normalization_config: PhysicalNormalizationConfig, source_provenance: dict | None = None,
    maximum_patches_per_direction: int = 32, maximum_zones_per_direction: int = 128,
    maximum_batch_mass_increase_pct: float = 5, balance_time_limit_s: float = 20,
    stock_time_limit_s: float = 10,
) -> PhysicalLayoutRecoveryResult:
    """Complete typed layout → exact STO patterns → normalization → checked trial.

    There is no source-path lookup, artifact dependency, permission inference or
    implicit engineering profile. Callers can provide fresh four-DXF optimization
    results or strict snapshots through this same application entry point.
    """
    _configuration(normalization_config, stock_time_limit_s)
    provenance = _provenance(source_provenance)
    patterned = recover_patterned_layout(problem, solution, settings,
        maximum_patches_per_direction=maximum_patches_per_direction,
        maximum_zones_per_direction=maximum_zones_per_direction,
        maximum_batch_mass_increase_pct=maximum_batch_mass_increase_pct,
        balance_time_limit_s=balance_time_limit_s)
    if not patterned.stock_balanced:
        report = deepcopy(patterned.report)
        report["source_provenance"] = provenance
        return _blocked(report, {"schema_version": "physical-layout-normalization/v1", "units": "mm",
            "status": "not_started_source_stock_blocked", "placement_eligible": False,
            "engineering_approval": False, "source_demand_removed": False, "accepted": None,
            "configuration": to_jsonable(normalization_config), "source_provenance": provenance,
            "application_trace": [{"stage": "patterned_stock", "status": "not_passed",
                                    "normalization_started": False}]},
            {"permanent_blockers": list(report["blocking_check_ids"]),
             "issues": ["No complete independently checked zero-waste patterned source; source FE retained."]},
            "blocked_patterned_stock")
    return recover_physical_layout_from_report(patterned.report, problem,
        normalization_config=normalization_config, source_provenance=provenance,
        stock_time_limit_s=stock_time_limit_s)
