"""Парное сравнение сохранённых benchmark.json в заново рассчитанных общих границах."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.reporting.genetic_benchmark import normalized_point_hypervolume, point_hypervolume_bounds


def load_series(paths, generations, *, local_search_passes=None):
    runs = {}
    references = []
    provenance = []
    for path in paths:
        raw = path.read_bytes()
        payload = json.loads(raw)
        references.append(payload["reference"])
        provenance.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
        for run in payload["runs"]:
            if run["generations"] != generations:
                continue
            if local_search_passes is not None and run.get("local_search_passes", 0) != local_search_passes:
                continue
            key = (run["population_size"], run["generations"], run["random_seed"],
                   run["operator_policy"], run["complexity_axis"])
            if key in runs:
                raise ValueError(f"неоднозначная пара конфигураций {key}: выбирайте отдельные серии")
            candidates = [row for row in payload["candidates"] if row["run_id"] == run["run_id"]]
            if any(not row["valid"] or row["under_reinforced_cell_count"] for row in candidates):
                raise ValueError("в сохранённом фронте есть невалидные кандидаты")
            if not candidates:
                raise ValueError("пустой фронт нельзя представить как нулевой gap")
            runs[key] = (run, candidates)
    if not references or any(reference != references[0] for reference in references):
        raise ValueError("серии должны иметь один и тот же проверенный инженерный эталон")
    return references[0], runs, provenance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, nargs="+", required=True)
    parser.add_argument("--after", type=Path, nargs="+", required=True)
    parser.add_argument("--generations", type=int, default=3)
    parser.add_argument("--before-local-search-passes", type=int)
    parser.add_argument("--after-local-search-passes", type=int)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    reference, before, sources_before = load_series(args.before, args.generations, local_search_passes=args.before_local_search_passes)
    reference_after, after, sources_after = load_series(args.after, args.generations, local_search_passes=args.after_local_search_passes)
    if reference is None or reference != reference_after:
        raise ValueError("нужен один и тот же непустой инженерный эталон")
    keys = sorted(before.keys() & after.keys())
    if not keys or len({key[-1] for key in keys}) != 1:
        raise ValueError("нужны парные запуски на одной оси сложности")

    def points(candidates):
        return [(row["complexity"], row["total_mass_kg"]) for row in candidates]

    bounds = point_hypervolume_bounds(
        point for key in keys for series in (before, after) for point in points(series[key][1])
    )
    rows = []
    for key in keys:
        old_hash = before[key][0].get("normalized_input_sha256")
        new_hash = after[key][0].get("normalized_input_sha256")
        if old_hash and new_hash and old_hash != new_hash:
            raise ValueError("числовой спрос или инженерные ограничения парных запусков различаются")
        old, new = before[key][1], after[key][1]
        old_points, new_points = points(old), points(new)
        old_minimum = min(old, key=lambda c: (c["total_mass_kg"], c["complexity"]))
        new_minimum = min(new, key=lambda c: (c["total_mass_kg"], c["complexity"]))
        at_old_mass = min(
            (c for c in new if c["total_mass_kg"] <= old_minimum["total_mass_kg"] + 1e-6),
            key=lambda c: (c["complexity"], c["total_mass_kg"]), default=None,
        )

        def point_metrics(candidate):
            if candidate is None:
                return None
            return {name: candidate.get(name) for name in (
                "total_mass_kg", "complexity", "physical_bar_count", "zone_count",
            )}
        old_hv = normalized_point_hypervolume(old_points, bounds)
        new_hv = normalized_point_hypervolume(new_points, bounds)
        budgets = []
        for budget in sorted({x for x, _mass in (*old_points, *new_points)}):
            old_mass = min((mass for complexity, mass in old_points if complexity <= budget), default=None)
            new_mass = min((mass for complexity, mass in new_points if complexity <= budget), default=None)
            budgets.append({"complexity_budget": budget, "before_mass_kg": old_mass, "after_mass_kg": new_mass})
        row = {
            "population_size": key[0], "generations": key[1], "random_seed": key[2],
            "operator_policy": key[3], "complexity_axis": key[4],
            "normalized_input_verified": bool(old_hash and new_hash),
            "before_mass_kg": min(mass for _x, mass in old_points),
            "after_mass_kg": min(mass for _x, mass in new_points),
            "before_hv": old_hv, "after_hv": new_hv, "hv_delta": new_hv - old_hv,
            "budgets": budgets,
            "before_minimum_mass_point": point_metrics(old_minimum),
            "after_minimum_mass_point": point_metrics(new_minimum),
            "after_at_before_mass": point_metrics(at_old_mass),
        }
        rows.append(row)
        print({key: value for key, value in row.items() if key != "budgets"})
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "comparison.json").write_text(json.dumps({
        "reference": reference, "common_bounds": bounds,
        "scope": "paired_saved_valid_fronts_not_equal_runtime_or_engineering_approval",
        "sources_before": sources_before, "sources_after": sources_after,
        "unpaired_before": sorted(before.keys() - after.keys()),
        "unpaired_after": sorted(after.keys() - before.keys()), "runs": rows,
    }, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()
