"""Audit whole-set stock-surplus shifts on a saved complete plate, without deleting failed zones."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path

from rebar.application.analyze_composite_plate import CompositeDirectionSettings, _placements
from rebar.application.analyze_direction import load_direction_mosaic
from rebar.application.composite_host_review import rectangular_host_from_reference
from rebar.application.composite_layout_review import _reject, _unique
from rebar.application.plate_revit_trial import build_full_plate_trial
from rebar.application.working_host import load_working_host_json
from rebar.models import Axis, Direction, Layer
from rebar.optimization.adapters.mosaic import build_demand_map
from rebar.optimization.contracts.composite_coverage import STO_279_COVERAGE_POLICY
from rebar.optimization.contracts.problem import LayoutConstraints
from rebar.optimization.services.composite_coverage import evaluate_composite_coverage
from rebar.optimization.services.composite_detailing import build_composite_zone
from rebar.optimization.services.composite_host import evaluate_composite_host
from rebar.optimization.services.composite_host_fit import fit_composite_zone_to_host


def _source(root, filename, digest):
    for path in sorted(root.rglob(filename)):
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
            return path
    raise ValueError("Нет исходного файла с подтверждённым SHA256: " + filename)


def experiment(analysis, reference, source_root, candidate_index, digest):
    build_full_plate_trial(analysis, candidate_index, digest)  # Independent full-source metrics/axis check.
    host = rectangular_host_from_reference(reference)
    kwargs = dict(analysis["constraints"])
    kwargs["allowed_cut_lengths_mm"] = tuple(kwargs["allowed_cut_lengths_mm"])
    constraints = LayoutConstraints(**kwargs)
    point = analysis["front"][candidate_index]
    rows = []
    for source, index in zip(analysis["directions"], point["direction_candidate_indexes"]):
        paths = {role: _source(source_root, name, source["source"]["sha256"][role])
                 for role, name in source["source"]["filenames"].items()}
        mosaic = load_direction_mosaic(paths["dxf"], shk_path=paths["shk"])
        demand = build_demand_map(mosaic)
        settings = dict(source["settings"])
        settings["direction"] = Direction(Layer(settings["direction"]["layer"]), Axis(settings["direction"]["axis"]))
        placements = dict(_placements(demand, CompositeDirectionSettings(**settings)))
        selected = next(c for c in source["candidates"] if c["candidate_index"] == index)
        zones = []
        for draft in selected["zone_drafts"]:
            zone = build_composite_zone(demand, tuple(draft["demand_bbox_mm"]), draft["level_index"],
                draft["source_zone_id"], placements[draft["level_index"]], constraints=constraints,
                installed_lengths_mm=tuple(c["installed_length_mm"] for c in draft["components"]))
            a = 0 if demand.direction.axis is Axis.X else 1
            zone = replace(zone, components=tuple(replace(c, longitudinal_interval_mm=(d["bar_axis_bbox_mm"][a],
                d["bar_axis_bbox_mm"][a+2])) for c, d in zip(zone.components, draft["components"])))
            zones.append(zone)
        before = evaluate_composite_host(demand, tuple(zones), host, constraints=constraints)
        after_zones, failures, shifts = [], [], []
        for zone in zones:
            try:
                fitted = fit_composite_zone_to_host(demand, zone, host, constraints=constraints)
                for a, b in zip(zone.components, fitted.components):
                    shift = b.longitudinal_interval_mm[0] - a.longitudinal_interval_mm[0]
                    if abs(shift) > 1e-6:
                        shifts.append({"zone_id": zone.id, "component_index": a.component_index, "shift_mm": shift})
                after_zones.append(fitted)
            except ValueError as exc:
                failures.append({"zone_id": zone.id, "reason": str(exc)})
                after_zones.append(zone)  # Preserve every unsolved zone; NEVER report just a convenient interior.
        after = evaluate_composite_host(demand, tuple(after_zones), host, constraints=constraints)
        checks = [evaluate_composite_coverage(demand, group, constraints=constraints, policy_id=STO_279_COVERAGE_POLICY)
                  for group in (zones, after_zones)]
        if checks[0] != checks[1] or checks[0].status != "pass":
            raise ValueError("Сдвиг изменил покрытие/количество/массу; эксперимент отвергнут")
        rows.append({"direction": str(demand.direction), "zone_count": len(zones),
            "physical_bar_count": checks[0].physical_bar_count, "additional_mass_kg": checks[0].additional_mass_kg,
            "invalid_bars_before": before["invalid_bar_count"], "invalid_bars_after": after["invalid_bar_count"],
            "coverage_and_metrics_unchanged": True, "shifts": shifts, "failed_zones_retained": failures})
    return {"schema_version": "host-stock-surplus-experiment/v1", "placement_eligible": False,
        "source_report_sha256": digest, "candidate_index": candidate_index, "directions": rows,
        "invalid_bars_before": sum(r["invalid_bars_before"] for r in rows),
        "invalid_bars_after": sum(r["invalid_bars_after"] for r in rows),
        "warning": "Исследование на явно выбранном Reference-контуре, не рабочий RVT и не новая готовая раскладка. "
                   "Неисправленные зоны сохранены, 40d/масса/количество не ослаблены."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--confirm-reference-xy", action="store_true", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--candidate", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.analysis.open("rb") as stream:
        content = stream.read(64 * 1024 * 1024 + 1)
    if len(content) > 64 * 1024 * 1024:
        parser.error("Анализ превышает 64 MiB")
    with args.reference.open("rb") as stream:
        reference = load_working_host_json(stream.read(8 * 1024 * 1024 + 1))
    result = experiment(json.loads(content, object_pairs_hook=_unique, parse_constant=_reject), reference,
                        args.source_root, args.candidate, hashlib.sha256(content).hexdigest())
    if args.output.suffix.lower() != ".json":
        parser.error("Нужен новый JSON")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "directions"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
