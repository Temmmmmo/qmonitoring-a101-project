"""Проверить составные шкалы DXF/SHK, не запуская несовместимый однокомпонентный GA."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.dxf_ingest import read_mosaic
from rebar.models import UnsupportedReinforcementRecipeError
from rebar.optimization import LayoutProblem, a101_247_slab_recipe_placement, build_demand_map
from rebar.reporting.serialization import to_jsonable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", type=Path)
    parser.add_argument("--shk", type=Path, required=True)
    parser.add_argument("--axis-scheme", choices=("unresolved", "a101-247-slab-300-150"), default="unresolved")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    mosaic = read_mosaic(str(args.dxf), str(args.shk))
    demand = build_demand_map(mosaic)
    levels = []
    for level in demand.levels:
        placement, issue = None, None
        if args.axis_scheme != "unresolved":
            try:
                placement = a101_247_slab_recipe_placement(level.recipe)
            except ValueError as error:
                issue = str(error)
        levels.append({"index": level.index, "label": level.label, "recipe": to_jsonable(level.recipe),
                       "requires_extra": level.requires_extra, "placement": to_jsonable(placement),
                       "axis_scheme_issue": issue, "actual_bar_count": None,
                       "geometry_status": "unresolved_origins"})
    try:
        LayoutProblem(demand)
        legacy_status = "single_component_model_only_not_engineering_release"
        legacy_issue = None
    except UnsupportedReinforcementRecipeError as error:
        legacy_status, legacy_issue = "blocked_composite_recipe", str(error)
    payload = {"schema_version": "reinforcement-recipes/v1", "source": str(args.dxf),
               "scale_source": str(args.shk), "direction": to_jsonable(demand.direction),
               "cell_count": len(demand.cells), "bbox_mm": list(demand.bbox),
               "axis_scheme": args.axis_scheme, "levels": levels,
               "legacy_optimizer_status": legacy_status, "legacy_optimizer_issue": legacy_issue,
               "export_eligible": False,
               "pending": ["background_origin", "additional_set_phases", "xy_vertical_order",
                           "host_cover_openings", "composite_coverage_and_validation"]}
    args.out_dir.mkdir(parents=True, exist_ok=True)
    output = args.out_dir / "recipes.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"{len(demand.cells)} КЭ; {len(levels)} полос; {legacy_status}; {output}")


if __name__ == "__main__":
    main()
