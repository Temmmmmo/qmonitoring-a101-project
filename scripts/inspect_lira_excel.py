"""Проверить три числовых XLSX ЛИРА; опционально построить четыре DemandMap по явным SHK."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rebar.application.inspect_lira_excel import summarize_lira_plate
from rebar.legend import build_legend
from rebar.lira import read_lira_excel
from rebar.lira.models import AS_DIRECTIONS
from rebar.optimization.adapters.lira import build_lira_plate_demands
from rebar.reporting.serialization import to_jsonable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", type=Path, required=True)
    parser.add_argument("--elements", type=Path, required=True)
    parser.add_argument("--reinforcement", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Новый JSON; существующий не перезаписывается")
    parser.add_argument("--include-records", action="store_true")
    parser.add_argument("--confirm-export-axes-global-xy", action="store_true")
    for direction in AS_DIRECTIONS:
        parser.add_argument(f"--shk-{str(direction).lower()}", type=Path)
    args = parser.parse_args()
    scales = {d: getattr(args, f"shk_{str(d).lower().replace('-', '_')}") for d in AS_DIRECTIONS}
    if any(scales.values()) and not all(scales.values()):
        parser.error("для подбора нужны все четыре явно выбранных .shk")
    if args.output.suffix.lower() != ".json" or args.output.exists():
        parser.error("output должен быть новым файлом .json")
    try:
        plate = read_lira_excel(args.nodes, args.elements, args.reinforcement)
        result = summarize_lira_plate(plate, include_records=args.include_records)
        if all(scales.values()):
            legends = {d: build_legend(str(path)) for d, path in scales.items()}
            mapping_sources = {d: f"{p.name}; sha256={hashlib.sha256(p.read_bytes()).hexdigest()}"
                               for d, p in scales.items()}
            result["demand_maps"] = to_jsonable(build_lira_plate_demands(
                plate, legends, mapping_sources=mapping_sources,
                export_axes_are_global_xy=args.confirm_export_axes_global_xy,
            ))
            result["axis_alignment"] = "caller_confirmed_global_xy"
        # Validate serialization before creating a destination file.
        content = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(content + "\n")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(f"КЭ: {len(plate.elements)}; узлов: {len(plate.nodes)}; направлений: 4")
    print(f"Отчёт: {args.output}; исходники не изменены; размещение Revit не разрешено")


if __name__ == "__main__":
    main()
