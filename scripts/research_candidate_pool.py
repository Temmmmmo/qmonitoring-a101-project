"""Сравнить точные фронты GA-пула и полного малого grid-пула без изменения GA."""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar
from rebar.optimization import AlgorithmRequest, LayoutConstraints, build_layout_problem
from rebar.optimization.algorithms.genetic.exact_oracle import solve_exact_candidate_front
from rebar.optimization.algorithms.genetic.grid_oracle import expand_to_complete_grid_space
from rebar.optimization.algorithms.genetic_pareto import _build_search_space
from rebar.reporting.serialization import to_jsonable


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--candidate-expansion", choices=("none", "layered"), default="none")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "artifacts" / "candidate_pool_research")
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    background = Rebar(300, 12)
    bands = [
        Band(0, 181, "base", 3.77, background, None),
        Band(1, 254, "weak", 7.54, background, Rebar(300, 12)),
        Band(2, 2, "strong", 35.19, background, Rebar(100, 20)),
    ]
    rows = []
    for height, width in ((2, 2), (1, 4)):
        for mask in itertools.product(range(3), repeat=height * width):
            if not any(mask):
                continue
            cells = []
            for index, level in enumerate(mask):
                row, column = divmod(index, width)
                x, y = column * 500, row * 500
                cells.append(Cell(
                    [(x, y), (x + 500, y), (x + 500, y + 500), (x, y + 500)],
                    (x + 250, y + 250), bands[level].aci, bands[level],
                ))
            for axis in Axis:
                problem = replace(build_layout_problem(
                    Mosaic(Direction(Layer.BOTTOM, axis), cells, bands, (0, 0, width * 500, height * 500)),
                    LayoutConstraints(min_width_cells=1),
                ), case_id=f"{height}x{width}-{''.join(map(str, mask))}-{axis.value}")
                request = AlgorithmRequest(max_details=len(cells))
                space = _build_search_space(
                    problem, request, candidate_window=6, candidate_trajectories=3,
                    layer_bridge_span=6, maximum_merge_reduction=12, maximum_pool_merges=5000,
                    baseline_seed_algorithms=("agglomerative", "bsp", "greedy-priority"),
                    random_seed=args.seed, deadline=None,
                    candidate_expansion=args.candidate_expansion,
                )
                complete = expand_to_complete_grid_space(problem, space)
                native_front = solve_exact_candidate_front(
                    problem, space, request, max_candidates=40, max_subsets=150_000,
                )
                complete_front = solve_exact_candidate_front(
                    problem, complete, request, max_candidates=40, max_subsets=150_000,
                )
                native_mass = min(point.metrics.total_mass_kg for point in native_front.solutions)
                complete_mass = min(point.metrics.total_mass_kg for point in complete_front.solutions)
                budget_gaps = []
                for point in complete_front.solutions:
                    eligible_masses = [
                        other.metrics.total_mass_kg for other in native_front.solutions
                        if other.metrics.detail_count <= point.metrics.detail_count
                    ]
                    gap = None if not eligible_masses else (
                        100 * (min(eligible_masses) - point.metrics.total_mass_kg)
                        / point.metrics.total_mass_kg
                    )
                    budget_gaps.append({"zone_budget": point.metrics.detail_count, "mass_gap_pct": gap})
                row = {
                    "case_id": problem.case_id, "mask": mask, "axis": axis.value,
                    "native_pool_size": len(space.candidates), "complete_pool_size": len(complete.candidates),
                    "native_mass_kg": native_mass, "complete_mass_kg": complete_mass,
                    "mass_gap_pct": 100 * (native_mass - complete_mass) / complete_mass,
                    "budget_gaps": budget_gaps,
                    "native_front": to_jsonable(native_front.solutions),
                    "complete_front": to_jsonable(complete_front.solutions),
                }
                rows.append(row)
    payload = {
        "scope": "native_pool_union_all_grid_rectangles_and_levels",
        "seed": args.seed, "candidate_expansion": args.candidate_expansion, "cases": rows,
    }
    report = args.out_dir / "pool_research.json"
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    improved = [row for row in rows if row["mass_gap_pct"] > 1e-6]
    print(f"Готово: {report}; случаев {len(rows)}, улучшений grid-эталона {len(improved)}")
    print("Максимальные gap:", sorted(
        ((row["mass_gap_pct"], row["case_id"]) for row in improved), reverse=True,
    )[:10])
    print("Отставаний при бюджете зон:", sum(
        point["mass_gap_pct"] is None or point["mass_gap_pct"] > 1e-6
        for row in rows for point in row["budget_gaps"]
    ))


if __name__ == "__main__":
    main()
