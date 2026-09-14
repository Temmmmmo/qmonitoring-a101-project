"""Воспроизвести офлайн-опыт стыков одной точки составного search JSON, без Revit apply."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rebar.application.composite_host_review import HOST_COORDINATE_POLICY
from rebar.application.composite_joint_review import research_composite_joint_depths
from rebar.application.composite_layout_review import MAX_INPUT_BYTES, _reject, _unique, load_review_input
from rebar.dxf_ingest import read_mosaic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("dxf", "shk", "reference", "search", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--point-index", type=int, help="Индекс с нуля; по умолчанию selected_index исходного поиска")
    parser.add_argument("--coordinate-policy", choices=[HOST_COORDINATE_POLICY], required=True)
    parser.add_argument("--candidate-depths-mm", type=float, nargs="+", action="append", required=True,
                        help="Повторить для каждой добавки в порядке схемы; это эксперимент, не проектные глубины")
    parser.add_argument("--minimum-clear-spacing-mm", type=float, required=True)
    parser.add_argument("--hypothesis-source", required=True)
    parser.add_argument("--preserve-component-order", action="store_true", help="Явно потребовать: первая добавка ближе к грани")
    parser.add_argument("--time-limit-s", type=float, default=10)
    parser.add_argument("--maximum-search-nodes", type=int, default=100000)
    args = parser.parse_args()
    if args.output.suffix.lower() != ".json" or args.output.exists():
        parser.error("output должен быть новым .json")
    try:
        paths = {k: getattr(args, k) for k in ("dxf", "shk", "reference", "search")}
        limits = {"dxf": 64 * 1024 * 1024, "shk": 1024 * 1024, "reference": MAX_INPUT_BYTES, "search": 32 * 1024 * 1024}
        raw = {}
        for key, path in paths.items():
            if path.suffix.lower() != (".json" if key in ("reference", "search") else "." + key):
                raise ValueError("неожиданное расширение " + key)
            with path.open("rb") as stream:
                raw[key] = stream.read(limits[key] + 1)
            if not raw[key] or len(raw[key]) > limits[key]:
                raise ValueError("пустой или слишком большой " + key)
        hashes = {k: hashlib.sha256(v).hexdigest() for k, v in raw.items()}
        search = json.loads(raw["search"].decode("utf-8-sig"), object_pairs_hook=_unique, parse_constant=_reject)
        if (not isinstance(search, dict) or search.get("schema_version") != "composite-layout-search/v1"
                or search.get("mode") != "research-only" or search.get("placement_eligible") is not False
                or search.get("units") != "mm" or search.get("host_policy") != "interior-exceptions"):
            raise ValueError("нужен исходный частичный research search/v1, не команда размещения")
        if any(search.get("source_sha256", {}).get(k) != hashes[k] for k in ("dxf", "shk", "reference")):
            raise ValueError("хеши DXF/SHK/reference не соответствуют исходному поиску")
        index = search.get("selected_index") if args.point_index is None else args.point_index
        front = search.get("front")
        if (isinstance(index, bool) or not isinstance(index, int) or not isinstance(front, list)
                or not 1 <= len(front) <= 64 or not 0 <= index < len(front)):
            raise ValueError("нет выбранной точки в конечном фронте")
        config = load_review_input(json.dumps(front[index]["review_input"], allow_nan=False).encode("utf-8"))
        result = research_composite_joint_depths(read_mosaic(str(args.dxf), str(args.shk)), config,
            load_review_input(raw["reference"]), coordinate_policy=args.coordinate_policy,
            candidate_depths_by_component=tuple(tuple(ds) for ds in args.candidate_depths_mm),
            minimum_clear_spacing_mm=args.minimum_clear_spacing_mm, hypothesis_source=args.hypothesis_source,
            preserve_component_order=args.preserve_component_order, time_limit_s=args.time_limit_s,
            maximum_search_nodes=args.maximum_search_nodes)
        for key, path in paths.items():
            with path.open("rb") as stream:
                if hashlib.sha256(stream.read(len(raw[key]) + 1)).hexdigest() != hashes[key]:
                    raise ValueError("вход изменился во время эксперимента: " + key)
        result.update(source_sha256=hashes, source_point_index=index)
        content = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except (ValueError, KeyError, TypeError, OSError, OverflowError, RecursionError, AttributeError) as error:
        parser.error(str(error))
    print(args.output)
    print(result["status"], "размещение: запрещено")
    print("Зоны:", result["zone_count"], "стержни:", result["physical_bar_count"], "масса, кг:", round(result["additional_mass_kg"], 3))
    print("Непокрытых исходных КЭ:", result["original_uncovered_cell_count"], "; край не завершён")
    print("Максимальная глубина оси, мм:", result["depth_search"].get("maximum_axis_depth_mm"),
          "; рабочая высота/прочность и стыки требуют согласования")
    return 0 if result["proposed_zones"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
