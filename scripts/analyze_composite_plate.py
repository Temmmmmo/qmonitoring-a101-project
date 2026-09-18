#!/usr/bin/env python3
"""Полный четырёхнаправленный DXF-сценарий, тот же application service, что в web."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rebar.application.analyze_composite_plate import CompositeDirectionSettings, analyze_composite_plate
from rebar.application.analyze_plate import PlateDirectionSource
from rebar.application.composite_layout_review import load_review_input
from rebar.application.composite_host_review import HOST_COORDINATE_POLICY
from rebar.application.working_host import HOST_TRANSLATION_POLICY, load_working_host_json
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for direction in PLATE_DIRECTIONS:
        key = f"{direction.layer.value}-{direction.axis.value.lower()}"
        parser.add_argument("--dxf-" + key, type=Path, required=True)
        parser.add_argument("--shk-" + key, "--scale-" + key, dest="scale_" + key.replace("-", "_"),
            type=Path, required=True, metavar="SHK_OR_PNG", help="шкала .shk или .png")
        parser.add_argument("--origin-" + key, type=float, required=True)
    parser.add_argument("--first-300-offset", type=float, required=True)
    parser.add_argument("--second-offset", type=float, required=True)
    parser.add_argument("--steel-class", required=True)
    parser.add_argument("--phase-source", required=True)
    parser.add_argument("--contact-side", choices=("left", "right"), default="left")
    parser.add_argument("--maximum-zones", type=int, default=64)
    parser.add_argument("--maximum-positions", type=int)
    parser.add_argument("--maximum-candidates", type=int, default=128)
    parser.add_argument("--search-mode", choices=("positions", "zone-merge"), default="positions",
        help="отдельный исследовательский поиск кривой масса/число зон")
    parser.add_argument("--solver-time-limit", type=float, default=10)
    parser.add_argument("--cutting-profile", choices=("continuous", "plate-11700", "plate-11700-batch"), default="plate-11700")
    parser.add_argument("--maximum-cutting-overhead-pct", type=float, default=5)
    host_group = parser.add_mutually_exclusive_group()
    host_group.add_argument("--reference", type=Path)
    host_group.add_argument("--host-input", type=Path, help="JSON из prepare_working_host.py с явной XY-трансляцией")
    parser.add_argument("--confirm-host-xy", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.suffix.lower() != ".json" or args.output.exists():
        parser.error("выход должен быть новым .json; существующие файлы не перезаписываются")
    if bool(args.reference) != args.confirm_host_xy:
        parser.error("--reference и --confirm-host-xy передаются вместе")
    try:
        sources, settings = [], []
        for direction in PLATE_DIRECTIONS:
            key = f"{direction.layer.value}_{direction.axis.value.lower()}"
            scale = getattr(args, "scale_" + key)
            suffix = scale.suffix.casefold()
            if suffix not in (".shk", ".png"):
                raise ValueError(f"шкала {scale.name!r} должна иметь расширение .shk или .png")
            sources.append(PlateDirectionSource(getattr(args, "dxf_" + key),
                shk_path=scale if suffix == ".shk" else None,
                png_path=scale if suffix == ".png" else None))
            settings.append(CompositeDirectionSettings(direction, getattr(args, "origin_" + key), args.first_300_offset,
                args.second_offset, args.steel_class, args.phase_source, args.contact_side))
        reference = load_review_input(args.reference.read_bytes()) if args.reference else None
        policy = HOST_COORDINATE_POLICY if reference else None
        if args.host_input:
            with args.host_input.open("rb") as stream:
                reference = load_working_host_json(stream.read(8 * 1024 * 1024 + 1))
            policy = HOST_TRANSLATION_POLICY
        result = analyze_composite_plate(tuple(sources), tuple(settings), maximum_zones_per_direction=args.maximum_zones,
            maximum_positions=args.maximum_positions, maximum_candidates=args.maximum_candidates,
            search_mode=args.search_mode,
            solver_time_limit_s=args.solver_time_limit, cutting_profile=args.cutting_profile,
            maximum_cutting_overhead_pct=args.maximum_cutting_overhead_pct,
            host_reference=reference, coordinate_policy=policy)
        content = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(content)
    except (ValueError, KeyError, OSError) as error:
        parser.error(str(error))
    print(result["status"], "four directions;", len(result["front"]), "plate candidates; placement_eligible=false")
    for point in result["front"]:
        print(f'{point["additional_mass_kg"]:.3f} kg / {point["zone_count"]} zones / {point["position_count"]} positions / '
              f'{point["physical_bar_count"]} bars / cutting={point["stock_cutting"]["status"]}')
    return 0 if result["front"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
