"""Fresh read-only audit of original FE demand after actual contour/hole cuts.

No demand transfer, clipping of FE, repair, Revit writes, or readiness upgrade.
The shared loader replays the physical cuts and checks against fresh DXF demand.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import sys

from shapely.geometry import Polygon, box, mapping
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))

from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.layout_snapshot import load_layout_snapshot
from rebar.application.physical_layout_recovery import _bytes
from rebar.application.working_host import load_working_host_json
from rebar.application.working_solid_host import inspect_working_solid
from rebar.models import Axis, Direction, Layer
from rebar.optimization.contracts.opening_relocation import SourceServiceLane
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.services.opening_relocation import lane_map
from rebar.optimization.services.shaped_fe_repair import ResearchLayerProfile, layer_elevations
from rebar.optimization.services.shaped_geometry import main_horizontal_interval_mm, straight_bar_from_physical
from rebar.optimization.services.tz_boundary_trim import check_boundary_trim, geometry_presence_offers


@dataclass(frozen=True)
class TrimmedAuditInputs:
    bars: tuple
    before: tuple
    lanes: tuple
    problem: object
    host: object
    source_files: tuple
    checks: dict
    report: dict
    graphic: dict


def _read(path, maximum=128*1024*1024):
    data = Path(path).read_bytes()
    if len(data) > maximum:
        raise ValueError("Bounded input file required")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate input JSON key")
            result[key] = value
        return result
    def nonfinite(value):
        raise ValueError("Nonfinite input JSON value: "+value)
    return json.loads(data, object_pairs_hook=unique, parse_constant=nonfinite), hashlib.sha256(data).hexdigest()


def _direction(value):
    if isinstance(value, str):
        layer, axis = value.split("-")
        return Direction(Layer(layer), Axis(axis))
    return Direction(Layer(value["layer"]), Axis(value["axis"]))


def reconstruct_lanes(report):
    """Cross-check finite drafts and source metadata, then shared STO validation."""
    metadata = {(row["direction"], row["zone_id"]): row for row in report["original_source_zones"]}
    lanes, seen = [], set()
    for row in report["directions"]:
        direction = _direction(row["direction"])
        along = 0 if direction.axis is Axis.X else 1
        for draft in row["source_zone_drafts"]:
            zone_id = draft["source_zone_id"]
            key = str(direction), zone_id
            if key in seen or key not in metadata or _direction(draft["direction"]) != direction:
                raise ValueError("Unique exact original source zone/direction required")
            seen.add(key)
            stored = metadata[key]
            if len(draft["components"]) != len(stored["components"]) or len(draft["components"]) != 1:
                raise ValueError("Exact single-component original source family required")
            for component, original in zip(draft["components"], stored["components"], strict=True):
                index = component["component_index"]
                axes = component["axis_coordinates_mm"]
                required = tuple(draft["demand_bbox_mm"][i] for i in (along, along+2))
                if (index != original["component_index"] or axes != original["axis_coordinates_mm"]
                        or len(axes) != original["source_bar_count"] or required != tuple(original["required_interval_mm"])
                        or component["diameter_mm"] != original["diameter_mm"]):
                    raise ValueError("Original source metadata differs from the full zone draft")
                placement = component["placement"]
                pattern, origin = placement["pattern"], placement["origin_mm"]
                period, offsets = pattern["period_mm"], pattern["offsets_mm"]
                for bar_index, q in enumerate(axes):
                    halves = []
                    for previous in (True, False):
                        gaps = [((q-origin-offset) if previous else (origin+offset-q)) % period for offset in offsets]
                        halves.append(min(period if min(v, period-v) <= 1e-6 else v for v in gaps)/2)
                    source = PhysicalSourceBar(f"{zone_id}/{index}/{bar_index}", direction,
                        original["steel_class"], original["diameter_mm"], q,
                        tuple(component["bar_axis_bbox_mm"][i] for i in (along, along+2)), required,
                        original["background_diameter_mm"], original["background_origin_mm"])
                    lanes.append(SourceServiceLane(source, zone_id, index, bar_index,
                        component["nominal_step_mm"], tuple(component["axis_window_mm"]), tuple(halves)))
    if seen != set(metadata):
        raise ValueError("Source zones omitted from reconstructable original drafts")
    lane_map(tuple(lanes))
    return tuple(lanes)


def load_trimmed_inputs(*, report_dir, snapshot, working_host_report, candidate_id="plate:52",
                        stock_time_limit_s=10):
    """Replay every trim and check original FE, current body, stock and 3D pairs.

    Original computation-code hashes are historical, not acceptance evidence:
    current code is recorded and before/after geometry rechecked independently.
    """
    folder = Path(report_dir)
    report, report_sha = _read(folder/"web-report.json")
    graphic, graphic_sha = _read(folder/"graphic-bar-plan-draft.json")
    summary, _ = _read(folder/"summary.json")
    if (summary["output_sha256"]["web-report.json"] != report_sha
            or summary["output_sha256"]["graphic-bar-plan-draft.json"] != graphic_sha
            or report["graphic_bar_plan_draft"] != graphic or graphic["respect_openings"] is not True
            or any(report[k] is not False for k in ("placement_eligible", "engineering_approval"))
            or graphic["source_to_revit_xy_mm"] != [0, 0]):
        raise ValueError("Exact bound original report/graphic with explicit unapproved XY policy required")
    loaded = load_layout_snapshot(snapshot, candidate_id=candidate_id)
    if report["case_id"] != loaded.problem.case_id or report["source_provenance"]["candidate_id"] != candidate_id:
        raise ValueError("Fresh original source case/candidate differs")
    expected_sources = {(str(_direction(v["direction"])), v["sha256"]) for v in report["source_provenance"]["sources"]}
    source_hashes = {str(Path(v["path"]).resolve()): v["sha256"] for v in loaded.snapshot["source_dxf"]}
    fresh_sources = {(str(p.demand.direction), source_hashes[str(Path(p.demand.source_path).resolve())])
        for p in loaded.problem.direction_problems}
    if expected_sources != fresh_sources:
        raise ValueError("Fresh original four DXF hashes differ from this web computation")
    host_raw, host_sha = _read(working_host_report, 32*1024*1024)
    if host_sha != summary["source_host_sha256"] or host_sha != graphic["source_host_report_sha256"]:
        raise ValueError("Exact Working Host hash differs")
    host, _ = inspect_working_solid(load_working_host_json(_bytes(host_raw)))
    lanes = reconstruct_lanes(report)
    profile = ResearchLayerProfile()
    if report["placement_profile"]["id"] != profile.id:
        raise ValueError("Explicit actual computation research layer profile required")
    def decode(rows):
        bars = []
        for row in rows:
            direction = _direction(row["direction"])
            for item in row["bars"]:
                physical = PhysicalBar(item["id"], direction, item["steel_class"], item["diameter_mm"],
                    item["coordinate_mm"], tuple(item["longitudinal_mm"]), tuple(item["source_bar_ids"]))
                bars.append(straight_bar_from_physical(physical,
                    axis_z_mm=layer_elevations(host, direction, physical.diameter_mm, profile)[0],
                    placement_profile_id=profile.id))
        return tuple(bars)
    before, bars = decode(graphic["before"]["directions"]), decode(graphic["directions"])
    checks = check_boundary_trim(before, bars, tuple(graphic["piece_mapping"]), lanes, loaded.problem, host,
        stock_time_limit_s=stock_time_limit_s, nudge_edge_axis=True, discard_empty_intersections=True,
        respect_openings=True)
    for key in ("geometric_presence", "coverage_with_control_40d", "physical_metrics", "physical_metrics_before",
                "piece_mapping", "changes", "material_boundary_failures_after", "opening_intersections_after",
                "removed_input_bars", "collisions"):
        if _bytes(checks[key]) != _bytes(report["boundary_trim"][key]):
            raise ValueError("Fresh physical cut check does not reproduce "+key)
    if checks["stock_cutting"]["status"] != report["boundary_trim"]["stock_cutting"]["status"]:
        raise ValueError("Fresh stock status differs")
    paths = [(folder/"web-report.json", "trimmed-web-report"), (folder/"graphic-bar-plan-draft.json", "trimmed-graphic"),
        (folder/"summary.json", "trimmed-summary"), (Path(snapshot), "source-snapshot"),
        (Path(working_host_report), "working-host")]
    paths += [(ROOT/p, "trimmed-audit-code") for p in ("scripts/audit_trimmed_demand.py",
        "src/rebar/optimization/services/tz_boundary_trim.py", "src/rebar/optimization/services/opening_relocation.py",
        "src/rebar/optimization/services/shaped_global_coverage.py", "src/rebar/optimization/services/shaped_geometry.py")]
    paths += [(Path(path), "source-DXF") for path in source_hashes]
    records = tuple(source_record(path, role=role) for path, role in paths)
    if any(r["sha256"] != source_hashes[str(Path(r["path"]).resolve())] for r in records if r["role"] == "source-DXF"):
        raise ValueError("Original DXF changed during fresh demand/physical verification")
    verify_source_records(records)
    return TrimmedAuditInputs(bars, before, lanes, loaded.problem, host, records, checks, report, graphic)


def _parts(geometry):
    if isinstance(geometry, Polygon):
        return (geometry,) if geometry.area > 0 else ()
    return tuple(p for child in getattr(geometry, "geoms", ()) for p in _parts(child))


def relaxed_presence_offers(bars, sources, *, release_windows=False, credit_actual_diameter=False):
    """Diagnostic-only alternatives; no source-owner or physical acceptance claim."""
    offered = {}
    for bar in bars:
        along = 0 if bar.direction.axis is Axis.X else 1
        interval = main_horizontal_interval_mm(bar)
        q = bar.segments[0].start_mm[1-along]
        for owner in bar.source_bar_ids:
            lane = sources[bar.direction, owner]
            lo, hi = q-lane.service_half_widths_mm[0], q+lane.service_half_widths_mm[1]
            if not release_windows:
                lo, hi = max(lo, lane.axis_window_mm[0]), min(hi, lane.axis_window_mm[1])
            if lo >= hi:
                continue
            shape = box(interval[0], lo, interval[1], hi) if along == 0 else box(lo, interval[0], hi, interval[1])
            diameter = bar.diameter_mm if credit_actual_diameter else lane.source.diameter_mm
            offered.setdefault(bar.direction, []).append((diameter, lane.nominal_step_mm, shape))
    return offered


def audit_missing(problem, offers, host):
    """Exact original missing polygons, split only for classification, never target editing."""
    material = unary_union([section.footprint for section in host.sections])
    outer = unary_union([Polygon(part.exterior) for part in _parts(material)])
    holes = outer.difference(material)
    rows, categories = [], Counter()
    for direction_problem in problem.direction_problems:
        demand = direction_problem.demand
        unions = {}
        for cell in demand.cells:
            recipe = demand.level(cell.level_index).recipe
            if not recipe.additions:
                continue
            spec = recipe.additions[0]
            if cell.level_index not in unions:
                unions[cell.level_index] = unary_union([p for d, s, p in offers.get(demand.direction, ())
                    if d >= spec.diameter and s <= spec.step])
            original = Polygon(cell.poly)
            missing = original.difference(unions[cell.level_index])
            if missing.area <= 0:
                continue
            inside = math.fsum(p.intersection(material).area for p in _parts(missing))
            exterior = math.fsum(p.difference(outer).area for p in _parts(missing))
            over_holes = math.fsum(p.intersection(holes).area for p in _parts(missing))
            positive = tuple(label for label, area in (("inside_material", inside), ("outside_outer", exterior),
                ("over_opening", over_holes)) if area > .001)
            label = "+".join(positive) if positive else "only_below_0.001mm2"
            categories[label] += 1
            rows.append({"direction": str(demand.direction), "cell_id": cell.id, "level_index": cell.level_index,
                "required_diameter_mm": spec.diameter, "required_nominal_step_mm": spec.step,
                "source_area_mm2": original.area, "uncovered_area_mm2": missing.area,
                "inside_material_area_mm2": inside, "outside_outer_area_mm2": exterior,
                "over_opening_area_mm2": over_holes, "classification_above_0_001mm2": label,
                "source_polygon": mapping(original), "missing_polygons": [mapping(p) for p in _parts(missing)]})
    return {"uncovered_direction_FE_count": len(rows), "categories_above_0_001mm2": dict(categories),
        "areas_mm2": {key: math.fsum(r[key] for r in rows) for key in
            ("uncovered_area_mm2", "inside_material_area_mm2", "outside_outer_area_mm2", "over_opening_area_mm2")},
        "material_policy": "optimistic_union_of_all_measured_Z_sections", "cells": rows}


def run(args):
    if args.output.exists():
        raise ValueError("A new audit output path is required")
    inputs = load_trimmed_inputs(report_dir=args.report_dir, snapshot=args.snapshot,
        working_host_report=args.working_host_report, candidate_id=args.candidate_id)
    print({"fresh_loaded": True, "before": len(inputs.before), "bars": len(inputs.bars), "lanes": len(inputs.lanes)}, flush=True)
    sources = lane_map(inputs.lanes)
    baseline = geometry_presence_offers(inputs.bars, sources)
    rows = {"current": audit_missing(inputs.problem, baseline, inputs.host)}
    for name, windows, actual_diameter in (("actual_D_only", False, True), ("unclamped_windows_only", True, False),
            ("actual_D_and_unclamped_windows", True, True)):
        offers = relaxed_presence_offers(inputs.bars, sources, release_windows=windows, credit_actual_diameter=actual_diameter)
        rows[name] = audit_missing(inputs.problem, offers, inputs.host)
    for name, row in rows.items():
        print(name, row["uncovered_direction_FE_count"], row["categories_above_0_001mm2"], row["areas_mm2"], flush=True)
    result = {"schema_version": "trimmed-demand-independent-audit/v1", "units": "mm",
        "source_files": inputs.source_files, "case_id": inputs.problem.case_id,
        "original_direction_FE_count": sum(len(p.demand.cells) for p in inputs.problem.direction_problems),
        "full_trim_check_reproduced": True, "current_physical_metrics": inputs.checks["physical_metrics"],
        "alternatives": rows, "source_demand_removed": False, "source_demand_transferred": False,
        "actual_bar_geometry_changed": False, "placement_eligible": False, "engineering_approval": False,
        "warning": "Alternative credits are DIAGNOSTIC hypotheses, not replacement-zone or structural acceptance."}
    verify_source_records(inputs.source_files)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("report-dir", "snapshot", "working-host-report", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", default="plate:52")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
