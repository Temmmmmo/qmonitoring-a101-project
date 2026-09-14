"""Fresh shared input proof for research starting AFTER the first FE repair.

No layout solver is rerun. The recorded endpoints are reversed and BOTH prior
stages are independently checked against fresh DXF and the new shifted source.
Neither stale unchanged-axis certificates nor archived stock witnesses authorize
the next experiment. No file is written and no Revit packet is returned.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import math
from pathlib import Path
from types import SimpleNamespace

from correct_small_openings import _read
from experiment_fe_host_repair import decode_bars
from experiment_shifted_physical_repair import _same, restore_shifted_inputs
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.layout_snapshot import LoadedLayoutSnapshot, load_layout_snapshot
from rebar.application.opening_relocation import source_service_lanes
from rebar.application.working_host import load_working_host_json
from rebar.application.working_solid_host import MAX_WORKING_REPORT_BYTES, inspect_working_solid
from rebar.optimization.contracts.opening_relocation import SourceServiceLane
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.contracts.plate import PlateProblem
from rebar.optimization.services.fe_host_repair import _valid_bars, check_fe_host_repair
from rebar.optimization.services.opening_relocation import check_relocation
from rebar.optimization.services.solid_host import OrthogonalSolidHost


@dataclass(frozen=True)
class CheckedFeResearchInputs:
    problem: PlateProblem
    lanes: tuple[SourceServiceLane, ...]
    bars: tuple[PhysicalBar, ...]
    host: OrthogonalSolidHost
    source_report: dict
    source_files: tuple[dict, ...]
    loaded: LoadedLayoutSnapshot
    input_report: dict
    input_sha256: str
    checks: dict


def _research_flags(value):
    for key in ("placement_eligible", "structural_placement_supported", "engineering_approval",
                "source_demand_removed", "source_demand_values_changed"):
        if value.get(key) is not False:
            raise ValueError("Literal false research permissions and unchanged demand required")


def reverse_recorded_fe_changes(current, changes):
    """Rebuild before-FE without trusting a copied baseline or changed ID list."""
    _valid_bars(current)
    if not isinstance(changes, list) or len(changes) > len(current):
        raise ValueError("Bounded complete FE changes list required")
    by_id = {(str(bar.direction), bar.id): bar for bar in current}
    seen = set()
    for row in changes:
        key = row["direction"], row["bar_id"]
        if key not in by_id or key in seen:
            raise ValueError("Unique existing changed physical ID required")
        seen.add(key)
        bar = by_id[key]
        before, after = row["before_interval_mm"], row["after_interval_mm"]
        if (not _same(after, list(bar.installed_interval_mm)) or _same(before, after)
                or not isinstance(before, list) or len(before) != 2):
            raise ValueError("Recorded FE endpoints differ from actual complete output")
        by_id[key] = replace(bar, installed_interval_mm=tuple(before))
    previous = tuple(by_id[(str(bar.direction), bar.id)] for bar in current)
    _valid_bars(previous)
    return previous


def validate_report_binding(report, *, loaded, candidate_id, host_record, required_records):
    """Require the exact new source/host/snapshot chain, not just matching counts."""
    if report["schema_version"] != "shifted-physical-repair-experiment/v1" or report["units"] != "mm":
        raise ValueError("Exact shifted physical FE research schema in mm required")
    _research_flags(report)
    _research_flags(report["checks"])
    if (report["case_id"] != loaded.problem.case_id or report["candidate_id"] != candidate_id
            or not _same(report["source_to_revit_xy_mm"], [0, 0])
            or report["source_host_report_sha256"] != host_record["sha256"]):
        raise ValueError("FE research case/candidate/host/identity XY differs")
    records = report["source_files"]
    if not isinstance(records, list) or not 1 <= len(records) <= 200:
        raise ValueError("Bounded FE source chain required")
    verify_source_records(records)
    present = {(str(Path(row["path"]).resolve()), row["sha256"], row["role"]) for row in records}
    required = {(str(Path(row["path"]).resolve()), row["sha256"], row["role"]) for row in required_records}
    if not required <= present:
        raise ValueError("FE report is not bound to the exact shifted source/normalization/review inputs")
    if not any(row["role"] == "source-snapshot" and row["sha256"] == loaded.source_sha256 for row in records):
        raise ValueError("Fresh original snapshot SHA missing from FE report")


def load_fe_research_inputs(*, shifted_dir, snapshot, candidate_id, working_host_report,
                            fe_report, confirm_identity_xy, stock_time_limit_s=30):
    """Read-only fresh source, opening and FE proof; incomplete proof raises."""
    if confirm_identity_xy is not True:
        raise ValueError("Explicit identity XY confirmation required")
    if (isinstance(stock_time_limit_s, bool) or not isinstance(stock_time_limit_s, (int, float))
            or not math.isfinite(stock_time_limit_s) or not 0.001 <= stock_time_limit_s <= 60):
        raise ValueError("Finite stock check budget in 0.001..60 required")
    loader_record = source_record(Path(__file__), role="research-input-loader-code")
    snapshot, working_host_report = Path(snapshot), Path(working_host_report)
    args = SimpleNamespace(shifted_dir=Path(shifted_dir), candidate_id=candidate_id,
        stock_time_limit_s=stock_time_limit_s)
    loaded = load_layout_snapshot(snapshot, candidate_id=candidate_id)
    snapshot_record = source_record(snapshot, role="source-snapshot")
    if snapshot_record["sha256"] != loaded.source_sha256:
        raise ValueError("Snapshot changed during fresh restoration")
    host_record = source_record(working_host_report, role="working-host-snapshot")
    source, normalized, fresh, upstream = restore_shifted_inputs(args, loaded, host_record)
    report, content, report_record = _read(Path(fe_report))
    validate_report_binding(report, loaded=loaded, candidate_id=candidate_id,
        host_record=host_record, required_records=(*upstream, snapshot_record, host_record))
    if not _same(report["stages"]["restored_shifted_normalized"]["expected"], fresh.packet["expected"]):
        raise ValueError("Recorded restored normalized inventory differs from fresh proof")
    with working_host_report.open("rb") as stream:
        host_bytes = stream.read(MAX_WORKING_REPORT_BYTES+1)
    if len(host_bytes) > MAX_WORKING_REPORT_BYTES or hashlib.sha256(host_bytes).hexdigest() != host_record["sha256"]:
        raise ValueError("Host exceeds budget or changed during reading")
    host, _ = inspect_working_solid(load_working_host_json(host_bytes, maximum_bytes=MAX_WORKING_REPORT_BYTES))
    bars = decode_bars(report["raw_bars_by_direction"])
    before_fe = reverse_recorded_fe_changes(bars, report["checks"]["changes"])
    lanes = source_service_lanes(source, loaded.problem)
    maximum_shift = report["stages"]["small_openings"]["configuration"]["maximum_shift_mm"]
    if (isinstance(maximum_shift, bool) or not isinstance(maximum_shift, (int, float))
            or not math.isfinite(maximum_shift) or not 0 <= maximum_shift <= 300):
        raise ValueError("Bounded recorded prior opening shift required")
    opening_checks = check_relocation(normalized, before_fe, lanes, loaded.problem, host,
        maximum_shift_mm=maximum_shift)
    if not _same(opening_checks, report["stages"]["small_openings"]["checks"]):
        raise ValueError("Recorded opening stage does not reproduce independently")
    fe_checks = check_fe_host_repair(before_fe, bars, lanes, loaded.problem, host,
        stock_time_limit_s=stock_time_limit_s)
    # A fresh exact cutting solution may use another equally valid witness.
    # The archived witness is never used for acceptance in this new experiment.
    def without_stock(value):
        return {key: item for key, item in value.items() if key != "stock_cutting"}
    if (not _same(without_stock(fe_checks), without_stock(report["checks"]))
            or fe_checks["stock_cutting"]["status"] != "pass"
            or report["checks"]["stock_cutting"]["status"] != "pass"):
        raise ValueError("Full current FE geometry/coverage/inventory/stock proof differs")
    records = [*upstream, *report["source_files"], snapshot_record, host_record, report_record,
        loader_record]
    verify_source_records(records)
    return CheckedFeResearchInputs(loaded.problem, lanes, bars, host, source, tuple(records), loaded,
        report, hashlib.sha256(content).hexdigest(),
        {"small_openings": opening_checks, "fe_host_repair": fe_checks,
            "archived_stock_witness_reused": False, "placement_eligible": False})
