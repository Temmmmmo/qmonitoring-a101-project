"""Recheck saved full-plate research layouts against unchanged source DXF demand.

Input is the local run_audit.py snapshot, not an externally accepted Revit packet.
Source hashes are checked; original maps are reparsed, never recovered from already
downgraded cells. Optional bounded repairs retain the entire original demand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rebar.application.analyze_direction import load_direction_mosaic
from rebar.application.gate_assessment import assess_plate_gates
from rebar.models import Axis, Direction, Layer, Rebar
from rebar.optimization import (
    AlgorithmRequest, ComplexityAxis, LayoutConstraints, LayoutMetrics,
    LayoutSolution, LayoutZone, ObjectiveWeights, PlateDirectionSolution,
    SolutionStatus, build_direction_pareto_front, build_layout_problem,
    build_plate_problem, build_plate_solution, combine_direction_pareto_fronts,
)
from rebar.optimization.algorithms.source_recovery import recover_source_demand
from rebar.optimization.services.source_revalidation import revalidate_source_demand
from rebar.reporting.serialization import to_jsonable


def decode_solution(raw: dict) -> LayoutSolution:
    zones = []
    for item in raw["zones"]:
        values = dict(item)
        values["rebar"] = Rebar(**values["rebar"])
        for key in ("bbox", "demand_bbox", "covered_cell_ids", "overcovered_cell_ids"):
            values[key] = tuple(values[key])
        zones.append(LayoutZone(**values))
    request = dict(raw["request"])
    request["objective"] = ObjectiveWeights(**request["objective"])
    return LayoutSolution(
        algorithm=raw["algorithm"], status=SolutionStatus(raw["status"]), zones=tuple(zones),
        metrics=LayoutMetrics(**raw["metrics"]), request=AlgorithmRequest(**request),
        runtime_ms=raw["runtime_ms"], diagnostics=tuple(raw["diagnostics"]), meta=raw["meta"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repair-bar-penalties", nargs="*", type=float, default=[])
    parser.add_argument("--neighbors", type=int, default=6)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"Output already exists: {args.output}")
    started = time.perf_counter()
    snapshot_bytes = args.snapshot.read_bytes()
    payload = json.loads(snapshot_bytes)
    for record in (*payload["source_dxf"], payload["source_pdf"]):
        if hashlib.sha256(Path(record["path"]).read_bytes()).hexdigest() != record["sha256"]:
            raise SystemExit(f"Changed source: {record['path']}")
    problems = {}
    for recorded in payload["problems"]["direction_problems"]:
        path = recorded["demand"]["source_path"]
        mosaic = load_direction_mosaic(path, mapping_id=recorded["demand"]["meta"]["rebar_mapping"]["id"])
        constraints = dict(recorded["constraints"])
        constraints["allowed_cut_lengths_mm"] = tuple(constraints["allowed_cut_lengths_mm"])
        original = build_layout_problem(mosaic, LayoutConstraints(**constraints))
        problems[original.demand.direction] = original
    plate_problem = build_plate_problem(problems.values(), case_id=payload["case_id"])
    cache, direction_proposals, audit_rows = {}, {direction: [] for direction in problems}, []
    rows = []
    for index, candidate in enumerate(payload["candidates"], 1):
        checked_directions = []
        for item in candidate["solution"]["direction_solutions"]:
            direction = Direction(Layer(item["direction"]["layer"]), Axis(item["direction"]["axis"]))
            raw = item["solution"]
            key = (direction, json.dumps(raw["zones"], sort_keys=True))
            if key not in cache:
                old = decode_solution(raw)
                audited = revalidate_source_demand(problems[direction], old)
                cache[key] = audited.solution
                audit_rows.append({
                    "direction": str(direction), "source_candidate_id": candidate["id"],
                    "original_demanded_cell_count": audited.solution.metrics.demanded_cell_count,
                    "covered_original_cell_count": audited.solution.metrics.covered_demanded_cell_count,
                    "residuals": to_jsonable(audited.uncovered_cells),
                    "old_mass_kg": old.metrics.total_mass_kg,
                    "checked_mass_kg": audited.solution.metrics.total_mass_kg,
                })
                direction_proposals[direction].append(audited.solution)
                for penalty in args.repair_bar_penalties:
                    recovered = recover_source_demand(
                        problems[direction], old, bar_penalty_kg=penalty, neighbor_count=args.neighbors,
                    )
                    direction_proposals[direction].append(recovered)
                print(f"{direction} unique={len(cache)} original gaps={len(audited.uncovered_cells)}", flush=True)
            checked_directions.append(PlateDirectionSolution(direction, cache[key]))
        checked = build_plate_solution(checked_directions)
        rows.append({
            "source_candidate_id": candidate["id"], "original_demand_valid": checked.valid,
            "metrics": to_jsonable(checked.metrics),
            "gates": to_jsonable(assess_plate_gates(plate_problem, checked)),
            "source_mass_kg": candidate["metrics"]["total_mass_kg"],
        })
        if index % 10 == 0:
            print(f"Audited {index}/{len(payload['candidates'])} plate combinations", flush=True)
    fronts = tuple(build_direction_pareto_front(
        problems[direction], proposals, complexity_axis=ComplexityAxis.PHYSICAL_BAR_COUNT,
    ) for direction, proposals in direction_proposals.items())
    recovered_front = combine_direction_pareto_fronts(plate_problem, fronts)
    result = {
        "schema": "legacy-source-recovery-audit/v1", "case_id": payload["case_id"],
        "snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "snapshot_path": str(args.snapshot), "source_dxf": payload["source_dxf"],
        "source_pdf": payload["source_pdf"], "engineer": payload["engineer"],
        "single_cell_policy": "preserve-original-demand",
        "original_legacy_candidate_count": len(rows),
        "unchanged_full_coverage_count": sum(row["original_demand_valid"] for row in rows),
        "unchanged_candidate_audits": rows, "unique_direction_audits": audit_rows,
        "repair_bar_penalties": args.repair_bar_penalties,
        "direction_proposals": {str(d): to_jsonable(v) for d, v in direction_proposals.items()},
        "recovered_front": to_jsonable(recovered_front),
        "elapsed_s": time.perf_counter() - started, "installation_approved": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, allow_nan=False, indent=2)
    print("Unchanged full coverage", result["unchanged_full_coverage_count"], "/", len(rows), flush=True)
    usable = recovered_front.candidates
    if usable:
        minimum = min(usable, key=lambda c: c.solution.metrics.total_mass_kg)
        under15 = [c for c in usable if c.solution.metrics.total_mass_kg <= 1.15 * payload["engineer"]["mass_kg"]]
        balanced = min(under15, key=lambda c: (c.solution.metrics.physical_bar_count, c.solution.metrics.total_mass_kg)) if under15 else None
        print("Recovered minimum", minimum.solution.metrics, flush=True)
        print("Recovered fewest bars under +15% mass", balanced.solution.metrics if balanced else None, flush=True)
    print("Report", args.output, "elapsed", result["elapsed_s"], flush=True)


if __name__ == "__main__":
    main()
