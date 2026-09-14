"""Revalidate a saved complete candidate against fresh DXF and try opt-in uniform phase repair."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.application.analyze_direction import load_direction_mosaic
from rebar.application.layout_snapshot import (
    CASE_MAPPINGS, MAX_SNAPSHOT_BYTES, _reject_constant, _same_metrics,
    _source_record, _unique_object, decode_solution,
)
from rebar.models import Axis, Direction, Layer
from rebar.optimization import (
    LayoutConstraints, PlateDirectionSolution, build_layout_problem, build_plate_solution, evaluate_layout,
)
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.preprocessing import apply_single_cell_rule
from rebar.optimization.services.uniform_phase_repair import repair_uniform_zone_phases
from rebar.reporting.serialization import to_jsonable


def experiment(snapshot: Path, candidate_id: str, *, maximum_pair_checks: int = 100_000) -> dict:
    with snapshot.open("rb") as stream:
        content = stream.read(MAX_SNAPSHOT_BYTES + 1)
    if len(content) > MAX_SNAPSHOT_BYTES:
        raise ValueError("snapshot exceeds bounded size")
    data = json.loads(content.decode("utf-8-sig"), object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant)
    mapping = CASE_MAPPINGS[data["case_id"]]
    profile = data["engineering_profile"]
    if (profile["single_cell_policy"] != "preserve" or profile["host_supplied"] is not False
            or profile["cutting_profile"] not in ("continuous", "plate-11700")
            or type(profile["min_width_cells"]) is not int or profile["min_width_cells"] != 2):
        raise ValueError("experiment requires preserved-demand audit profile with no host")
    constraints = LayoutConstraints(min_width_cells=2, cutting_profile=profile["cutting_profile"],
        allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM if profile["cutting_profile"] == "plate-11700" else ())
    source_paths = {_source_record(record, ".dxf") for record in data["source_dxf"]}
    if len(source_paths) != 4 or len(data["source_dxf"]) != 4:
        raise ValueError("expected four unique verified source DXF")
    _source_record(data["source_pdf"], ".pdf")
    selected = [item for item in data["candidates"] if item["id"] == candidate_id]
    if len(selected) != 1:
        raise ValueError("candidate ID must match exactly once")
    selected = selected[0]
    problems = {}
    used_paths = set()
    for raw in data["problems"]["direction_problems"]:
        path = Path(raw["demand"]["source_path"]).resolve(strict=True)
        if path not in source_paths or path in used_paths:
            raise ValueError("source problem is not bound to one unique verified DXF")
        if raw["constraints"] != to_jsonable(constraints):
            raise ValueError("recorded constraints do not match the explicit audit profile")
        used_paths.add(path)
        mosaic = load_direction_mosaic(path, mapping_id=mapping)
        fresh = apply_single_cell_rule(build_layout_problem(mosaic, constraints), policy="preserve")
        if to_jsonable(fresh.demand) != raw["demand"] or to_jsonable(fresh.meta) != raw["meta"]:
            raise ValueError("fresh source demand differs from snapshot")
        if fresh.demand.direction in problems:
            raise ValueError("duplicate source direction")
        problems[fresh.demand.direction] = fresh
    if used_paths != source_paths or len(problems) != 4:
        raise ValueError("incomplete preserved source plate")
    records, before_directions, after_directions = [], [], []
    for raw in selected["solution"]["direction_solutions"]:
        direction = Direction(Layer(raw["direction"]["layer"]), Axis(raw["direction"]["axis"]))
        decoded = decode_solution(raw["solution"])
        before_check = evaluate_layout(problems[direction], decoded.zones, decoded.request)
        _same_metrics(to_jsonable(before_check.metrics), raw["solution"]["metrics"], "before snapshot")
        started = perf_counter()
        result = repair_uniform_zone_phases(problems[direction], decoded.zones,
            request=decoded.request, maximum_pair_checks=maximum_pair_checks)
        after = replace(decoded, zones=result.zones, metrics=result.evaluation.metrics,
                        diagnostics=result.evaluation.diagnostics)
        before_directions.append(PlateDirectionSolution(direction, decoded))
        after_directions.append(PlateDirectionSolution(direction, after))
        records.append({"direction": str(direction), "source_cell_count": len(problems[direction].demand.cells),
                        "seconds": perf_counter() - started,
                        "before_metrics": to_jsonable(decoded.metrics),
                        "after_metrics": to_jsonable(after.metrics),
                        "result": to_jsonable(result)})
    before_plate = build_plate_solution(before_directions)
    after_plate = build_plate_solution(after_directions)
    _same_metrics(to_jsonable(before_plate.metrics), selected["metrics"], "before plate")
    for record in data["source_dxf"]:
        _source_record(record, ".dxf")
    if snapshot.read_bytes() != content:
        raise ValueError("snapshot changed during experiment")
    helper = REPO_ROOT / "src/rebar/optimization/services/uniform_phase_repair.py"
    return {"schema": "qmonitoring-uniform-phase-experiment/v1",
        "snapshot": {"path": str(snapshot.resolve()), "sha256": hashlib.sha256(content).hexdigest()},
        "case_id": data["case_id"], "candidate_id": candidate_id, "source_dxf": data["source_dxf"],
        "engineering_profile": profile, "helper_sha256": hashlib.sha256(helper.read_bytes()).hexdigest(),
        "before_metrics": to_jsonable(before_plate.metrics), "after_metrics": to_jsonable(after_plate.metrics),
        "before_conflicting_zone_pairs": sum(len(r["result"]["conflicting_zone_pairs_before"]) for r in records),
        "after_conflicting_zone_pairs": sum(len(r["result"]["conflicting_zone_pairs_after"]) for r in records),
        "before_zone_gap_pairs": sum(len(r["result"]["zone_gap_pairs_before"]) for r in records),
        "after_zone_gap_pairs": sum(len(r["result"]["zone_gap_pairs_after"]) for r in records),
        "directions": records, "placement_eligible": False,
        "scope": "uniform same-direction research only; no periodic @150, host, background or 3D acceptance"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--maximum-pair-checks", type=int, default=100_000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose a new report path")
    report = experiment(args.snapshot, args.candidate_id, maximum_pair_checks=args.maximum_pair_checks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, allow_nan=False, indent=2)
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ("directions", "source_dxf")}, ensure_ascii=False, allow_nan=False, indent=2))
    print(args.output.resolve())


if __name__ == "__main__":
    main()
