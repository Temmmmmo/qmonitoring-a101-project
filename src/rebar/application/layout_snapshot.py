"""Restore local run_audit snapshots from freshly read, hash-matched source DXF.

This deliberately narrow reader accepts only the three known engineer comparison
cases and preserved-demand profiles. A snapshot is not a placement authorization.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rebar.application.analyze_direction import load_direction_mosaic
from rebar.application.layout_plate_trial import build_layout_plate_trial_bundle
from rebar.golden import get_engineer_reference_case
from rebar.models import Axis, Direction, Layer, Rebar
from rebar.optimization import (
    AlgorithmRequest, LayoutConstraints, LayoutMetrics, LayoutSolution, LayoutZone,
    ObjectiveWeights, PlateDirectionSolution, PlateProblem, PlateSolution, SolutionStatus,
    build_layout_problem, build_plate_problem, build_plate_solution,
)
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.preprocessing import apply_single_cell_rule
from rebar.optimization.services.source_revalidation import revalidate_source_demand
from rebar.reporting.serialization import to_jsonable

CASE_MAPPINGS = {
    "plate-zero-k09": "plate-zero-d12-v1",
    "k09-minus-2": "k09-minus-2-d12-v1",
    "k09-typical-3-14": "k09-above-3-d10-v1",
}
SELECTIONS = ("minimum_mass", "minimum_bars_under_15pct_mass", "minimum_mass_without_extra_bars")
# Recovery provenance is intentionally repeated in every saved candidate. The
# full local audit can exceed 64 MiB; transport packet still has its own 8 MiB cap.
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"Nonfinite JSON number: {value}")


def decode_solution(raw: dict) -> LayoutSolution:
    """Decode the existing dataclass snapshot without inventing omitted fields."""
    zones = []
    for item in raw["zones"]:
        values = dict(item)
        values["rebar"] = Rebar(**values["rebar"])
        for key in ("bbox", "demand_bbox", "covered_cell_ids", "overcovered_cell_ids"):
            values[key] = tuple(values[key])
        zones.append(LayoutZone(**values))
    request = dict(raw["request"])
    request["objective"] = ObjectiveWeights(**request["objective"])
    return LayoutSolution(algorithm=raw["algorithm"], status=SolutionStatus(raw["status"]),
        zones=tuple(zones), metrics=LayoutMetrics(**raw["metrics"]),
        request=AlgorithmRequest(**request), runtime_ms=raw["runtime_ms"],
        diagnostics=tuple(raw["diagnostics"]), meta=raw["meta"])


def _source_record(record: dict, suffix: str) -> Path:
    path = Path(record["path"]).resolve(strict=True)
    digest = record["sha256"]
    if (not path.is_file() or path.suffix.lower() != suffix
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)):
        raise ValueError("Invalid source path, extension or SHA256")
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else None
        if actual is None:
            hasher = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
            actual = hasher.hexdigest()
    if actual != digest:
        raise ValueError(f"Changed source: {path}")
    return path


def _same_metrics(actual: dict, recorded: dict, context: str) -> None:
    if actual.keys() != recorded.keys():
        raise ValueError(f"{context}: metric fields differ")
    for key, value in actual.items():
        other = recorded[key]
        if (isinstance(other, bool) or not isinstance(other, (int, float))
                or not math.isfinite(other) or not math.isclose(value, other, rel_tol=1e-12, abs_tol=1e-5)):
            raise ValueError(f"{context}: stale or changed metric {key}")


@dataclass(frozen=True)
class LoadedLayoutSnapshot:
    """Independently restored local snapshot; typed data are not a placement permit."""

    problem: PlateProblem
    solution: PlateSolution
    source_sha256: str
    snapshot: dict[str, Any]
    engineer_comparison: dict[str, Any]


def load_layout_snapshot(
    snapshot_path: str | Path, *, candidate_id: str | None = None,
    selection: str | None = None,
) -> LoadedLayoutSnapshot:
    """Recreate source demand, revalidate candidates and return a typed selection.

    Preset selection is recomputed from independently checked full candidates,
    not from snapshot selection flags or stored mass deltas. Every unique saved
    direction is validated once; malformed/stale snapshots fail, not auto-repair.
    """
    if (candidate_id is None) == (selection is None):
        raise ValueError("Choose exactly one candidate_id or selection")
    if selection is not None and selection not in SELECTIONS:
        raise ValueError("Unknown candidate selection")
    snapshot_path = Path(snapshot_path).resolve(strict=True)
    if snapshot_path.suffix.lower() != ".json" or not snapshot_path.is_file():
        raise ValueError("Expected a local JSON snapshot")
    with snapshot_path.open("rb") as stream:
        content = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if not content or len(content) > MAX_SNAPSHOT_BYTES:
        raise ValueError("Empty snapshot or snapshot exceeds 256 MiB")
    data = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant)
    case_id = data["case_id"]
    if case_id not in CASE_MAPPINGS:
        raise ValueError("Snapshot case has no explicitly supported mapping")
    case = get_engineer_reference_case(case_id)
    if data["input_set_id"] not in {item.id for item in case.input_sets}:
        raise ValueError("Input set does not belong to the engineer reference case")
    engineer = data["engineer"]
    if (engineer["mass_kg"] != case.expected_mass_kg
            or engineer["physical_bar_count"] != case.expected_bar_count
            or engineer["specification_rows"] != case.expected_position_count):
        raise ValueError("Engineer comparison totals differ from the verified case catalog")
    profile = data["engineering_profile"]
    if (profile["single_cell_policy"] != "preserve" or profile["host_supplied"] is not False
            or profile["cutting_profile"] not in ("continuous", "plate-11700")):
        raise ValueError("Only unchanged-demand run_audit profiles without a host are supported")
    if type(profile["min_width_cells"]) is not int or profile["min_width_cells"] != 2:
        raise ValueError("This audit profile requires the recorded minimum width of two FE")
    expected_constraints = LayoutConstraints(min_width_cells=2,
        cutting_profile=profile["cutting_profile"], allowed_cut_lengths_mm=(
            PLATE_11700_CUT_LENGTHS_MM if profile["cutting_profile"] == "plate-11700" else ()))
    if len(data["source_dxf"]) != 4 or len(data["problems"]["direction_problems"]) != 4:
        raise ValueError("Exactly four complete DXF sources and problems are required")
    records = data["source_dxf"]
    source_paths = [_source_record(record, ".dxf") for record in records]
    if len(set(source_paths)) != 4:
        raise ValueError("Repeated DXF source paths")
    _source_record(data["source_pdf"], ".pdf")
    problems = []
    used_paths = []
    for recorded in data["problems"]["direction_problems"]:
        path = Path(recorded["demand"]["source_path"]).resolve(strict=True)
        if path not in source_paths or path in used_paths:
            raise ValueError("Recorded problem is not bound to one unique hash-verified source DXF")
        used_paths.append(path)
        if recorded["constraints"] != to_jsonable(expected_constraints):
            raise ValueError("Recorded constraints differ from the explicit audit profile")
        mapping = recorded["demand"]["meta"]["rebar_mapping"]["id"]
        if mapping != CASE_MAPPINGS[case_id]:
            raise ValueError("Recorded mapping differs from the explicit case mapping")
        mosaic = load_direction_mosaic(recorded["demand"]["source_path"], mapping_id=mapping)
        fresh = apply_single_cell_rule(build_layout_problem(mosaic, expected_constraints), policy="preserve")
        if to_jsonable(fresh.demand) != recorded["demand"]:
            raise ValueError("Fresh DXF demand/legend/metadata differs from the preserved snapshot")
        if fresh.meta != recorded["meta"]:
            # Normalize tuple/list representation, but never discard a change report.
            if to_jsonable(fresh.meta) != recorded["meta"]:
                raise ValueError("Recorded problem metadata differs from fresh preserved demand")
        problems.append(fresh)
    problem = build_plate_problem(problems, case_id=case_id)
    candidates, cache = {}, {}
    if not isinstance(data["candidates"], list) or not 1 <= len(data["candidates"]) <= 1000:
        raise ValueError("Expected 1..1000 complete saved candidates")
    for candidate in data["candidates"]:
        identifier = candidate["id"]
        if not isinstance(identifier, str) or not identifier.strip() or identifier in candidates:
            raise ValueError("Missing or duplicate candidate ID")
        directions = []
        for item in candidate["solution"]["direction_solutions"]:
            direction = Direction(Layer(item["direction"]["layer"]), Axis(item["direction"]["axis"]))
            raw = item["solution"]
            key = (direction, json.dumps(raw, sort_keys=True, allow_nan=False))
            if key not in cache:
                decoded = decode_solution(raw)
                audited = revalidate_source_demand(problem.problem(direction), decoded)
                _same_metrics(to_jsonable(audited.solution.metrics), raw["metrics"], "direction")
                if audited.solution.zones != decoded.zones:
                    raise ValueError("Preserved snapshot has stale coverage annotations")
                cache[key] = audited.solution
            directions.append(PlateDirectionSolution(direction, cache[key]))
        restored = build_plate_solution(directions, meta=candidate["solution"]["meta"])
        _same_metrics(to_jsonable(restored.metrics), candidate["solution"]["metrics"], "plate solution")
        _same_metrics(to_jsonable(restored.metrics), candidate["metrics"], "candidate")
        candidates[identifier] = restored
    if candidate_id is None:
        eligible = [(key, value) for key, value in candidates.items() if value.valid]
        if selection == "minimum_mass_without_extra_bars":
            eligible = [(key, value) for key, value in eligible
                        if value.metrics.physical_bar_count <= case.expected_bar_count]
        if selection == "minimum_bars_under_15pct_mass":
            eligible = [(key, value) for key, value in eligible
                        if value.metrics.total_mass_kg <= 1.15 * case.expected_mass_kg]

            def score(item):
                return item[1].metrics.physical_bar_count, item[1].metrics.total_mass_kg, item[0]
        else:

            def score(item):
                return item[1].metrics.total_mass_kg, item[1].metrics.physical_bar_count, item[0]
        if not eligible:
            raise ValueError("No independently validated full candidate satisfies this selection")
        candidate_id, _ = min(eligible, key=score)
    if candidate_id not in candidates:
        raise ValueError("Unknown candidate ID")
    solution = candidates[candidate_id]
    if not solution.valid:
        raise ValueError("Chosen candidate does not cover the preserved full demand")
    # Recheck files after parsing/validation as well: a changed source invalidates the bundle.
    for record in records:
        _source_record(record, ".dxf")
    _source_record(data["source_pdf"], ".pdf")
    if snapshot_path.read_bytes() != content:
        raise ValueError("Snapshot changed during export")
    snapshot = {"path": str(snapshot_path), "candidate_id": candidate_id,
        "selection": selection, "candidate_count": len(candidates),
        "source_dxf": records, "source_pdf": data["source_pdf"],
        "engineering_profile": profile, "code_sha256_at_calculation": data.get("code_sha256")}
    comparison = {
        "mass_kg": case.expected_mass_kg, "physical_bar_count": case.expected_bar_count,
        "specification_rows": case.expected_position_count,
        "mass_delta_pct": (solution.metrics.total_mass_kg / case.expected_mass_kg - 1) * 100,
        "bar_delta_pct": (solution.metrics.physical_bar_count / case.expected_bar_count - 1) * 100,
        "mass_threshold_15pct_met": solution.metrics.total_mass_kg <= 1.15 * case.expected_mass_kg,
        "scope": "additional reinforcement of the matched case, not all project gates"}
    return LoadedLayoutSnapshot(problem, solution, hashlib.sha256(content).hexdigest(),
                                snapshot, comparison)


def load_layout_trial_snapshot(
    snapshot_path: str | Path, *, candidate_id: str | None = None,
    selection: str | None = None, steel_class: str,
) -> dict[str, Any]:
    """Backward-compatible uniform-layout rollback bundle from the strict reader."""
    loaded = load_layout_snapshot(snapshot_path, candidate_id=candidate_id, selection=selection)
    bundle = build_layout_plate_trial_bundle(loaded.problem, loaded.solution,
        source_sha256=loaded.source_sha256, steel_class=steel_class, original_problem=loaded.problem)
    bundle["review"]["snapshot"] = loaded.snapshot
    bundle["review"]["engineer_comparison"] = loaded.engineer_comparison
    return bundle
