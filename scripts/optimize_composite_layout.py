"""Поиск составных зон DXF/SHK с явными фазами; RVT не изменяет."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from rebar.application.composite_layout_review import MAX_INPUT_BYTES, load_review_input
from rebar.application.composite_host_review import HOST_COORDINATE_POLICY
from rebar.application.optimize_composite_layout import optimize_composite_layout
from rebar.dxf_ingest import read_mosaic


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dxf", type=Path, required=True)
    parser.add_argument("--shk", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True, help="review-input/v1 с пустым zones и всеми фазами")
    parser.add_argument("--output", type=Path, required=True, help="Только новый JSON")
    parser.add_argument("--algorithm", choices=["whole-cell-pool", "fragment-grid"], default="whole-cell-pool")
    parser.add_argument("--along-step-mm", type=float, help="Только fragment-grid: шаг разрезов вдоль стержней")
    parser.add_argument("--across-step-mm", type=float, help="Только fragment-grid: шаг разрезов поперёк стержней")
    parser.add_argument("--across-origin-mm", type=float, help="Только fragment-grid: глобальная фаза линий разреза, не осей стержней")
    parser.add_argument("--maximum-zones", type=int, default=64)
    parser.add_argument("--maximum-physical-bars", type=int, help="Жёсткий лимит физических стержней, не позиций спецификации")
    parser.add_argument("--maximum-mass-kg", type=float, help="Жёсткий лимит массы искомых зон; не сертификат сравнения с инженером")
    parser.add_argument("--maximum-bar-length-mm", type=float, help="Жёсткий предел длины; стыки автоматически не добавляются")
    parser.add_argument("--maximum-candidates", type=int)
    parser.add_argument("--partition-depth", type=int)
    parser.add_argument("--solver-time-limit", type=float, default=30)
    parser.add_argument("--reference", type=Path, help="Read-only Reference Probe JSON для офлайн host-проверки")
    parser.add_argument("--coordinate-policy", choices=[HOST_COORDINATE_POLICY])
    parser.add_argument("--host-policy", choices=["report-only", "require-planar-containment", "interior-exceptions"], default="report-only")
    args = parser.parse_args()
    if args.output.suffix.lower() != ".json" or args.output.exists():
        parser.error("output должен быть новым .json")
    if (args.reference is None) != (args.coordinate_policy is None):
        parser.error("--reference и --coordinate-policy требуются вместе")
    if args.algorithm == "fragment-grid":
        if args.maximum_candidates is not None or args.partition_depth is not None:
            parser.error("fragment-grid не использует параметры целоклеточного пула")
        search = {"along_step_mm": 1800 if args.along_step_mm is None else args.along_step_mm,
                  "across_step_mm": 900 if args.across_step_mm is None else args.across_step_mm,
                  "across_origin_mm": args.across_origin_mm,
                  "time_limit_s": args.solver_time_limit}
    else:
        if args.along_step_mm is not None or args.across_step_mm is not None or args.across_origin_mm is not None:
            parser.error("параметры сетки требуют --algorithm fragment-grid")
        search = {"maximum_candidates": 384 if args.maximum_candidates is None else args.maximum_candidates,
                  "partition_depth": 6 if args.partition_depth is None else args.partition_depth,
                  "solver_time_limit_s": args.solver_time_limit}
    try:
        raw, paths = {}, {"dxf": args.dxf, "shk": args.shk, "config": args.config}
        limits = [("dxf", 64 * 1024 * 1024), ("shk", 1024 * 1024), ("config", MAX_INPUT_BYTES)]
        if args.reference is not None:
            paths["reference"] = args.reference
            limits.append(("reference", MAX_INPUT_BYTES))
        for key, limit in limits:
            if paths[key].suffix.lower() != (".json" if key in ("config", "reference") else "." + key):
                raise ValueError("неожиданное расширение " + key)
            with paths[key].open("rb") as stream:
                raw[key] = stream.read(limit + 1)
            if not raw[key] or len(raw[key]) > limit:
                raise ValueError("пустой или слишком большой файл " + key)
        hashes = {k: hashlib.sha256(v).hexdigest() for k, v in raw.items()}
        result = optimize_composite_layout(read_mosaic(str(args.dxf), str(args.shk)), load_review_input(raw["config"]),
                                           host_reference=load_review_input(raw["reference"]) if args.reference else None,
                                           coordinate_policy=args.coordinate_policy,
                                           host_policy=args.host_policy,
                                           algorithm=args.algorithm, maximum_zones=args.maximum_zones,
                                           maximum_physical_bars=args.maximum_physical_bars, maximum_mass_kg=args.maximum_mass_kg,
                                           maximum_bar_length_mm=args.maximum_bar_length_mm,
                                           **search)
        for key, path in paths.items():
            with path.open("rb") as stream:
                after = stream.read(len(raw[key]) + 1)
            if hashlib.sha256(after).hexdigest() != hashes[key]:
                raise ValueError("исходный файл изменился во время поиска: " + key)
        result["source_sha256"] = hashes
        content = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except (ValueError, KeyError, OSError, TypeError, OverflowError) as error:
        parser.error(str(error))
    print(args.output)
    print(result["status"], "вариантов:", len(result["front"]), "размещение: запрещено")
    if result["interior_partition"] is not None:
        partition = result["interior_partition"]
        print("ЧАСТИЧНАЯ ЗАДАЧА. Внутренних КЭ:", partition["target_cell_count"],
              "краевых исключений:", partition["boundary_exception_cell_count"])
        print("Край сохранён в полной проверке. Масса не включает его завершение; сравнение с инженером не подтверждено.")
    for point in result["front"]:
        print("Зон:", point["zone_count"], "стержней:", point["physical_bar_count"],
              "масса, кг:", round(point["additional_mass_kg"], 3), "непокрытых КЭ:", point["uncovered_cell_count"])
        print("Максимальная длина стержня, мм:", round(point["maximum_installed_bar_length_mm"], 3),
              "; товарные длины/стыки не согласованы; позиции спецификации не посчитаны")
        if "host_preflight" in point:
            print("Host:", point["host_preflight"]["status"], "стержней с ошибками:", point["host_preflight"]["invalid_bar_count"])
            projected = point["host_preflight"]["unknown_depth_projected_interzone_conflicts"]
            if projected["pair_count"]:
                print("Потенциальных межзонных конфликтов в XY при неизвестной высоте:", projected["pair_count"],
                      "; требуется решение стыков/укладки")
    return 0 if result["selected_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
