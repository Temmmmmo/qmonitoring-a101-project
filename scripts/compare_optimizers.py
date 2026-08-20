#!/usr/bin/env python3
"""Сравнить алгоритмы раскладки на одном DXF и создать автономный HTML-отчёт."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.dxf_ingest import read_mosaic
from rebar.optimization import MissingRebarSpecificationError
from rebar.reporting import generate_comparison_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", type=Path, help="путь к DXF с доступным совместимым .shk")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "artifacts/comparison")
    parser.add_argument(
        "--algorithms",
        nargs="+",
        default=[
            "spatial-partition-greedy",
            "bbox",
            "bsp",
            "greedy",
            "greedy-priority",
            "agglomerative",
            "row-run-greedy",
            "strip-profile-dp",
        ],
    )
    parser.add_argument("--max-details", type=int, default=32)
    parser.add_argument("--detail-penalty-kg", type=float, default=0.0)
    parser.add_argument("--min-width-cells", type=int, default=2)
    overlap_group = parser.add_mutually_exclusive_group()
    overlap_group.add_argument(
        "--allow-overlaps",
        action="store_true",
        dest="allow_overlaps",
        help="разрешить пересечения зон (рабочий режим по умолчанию)",
    )
    overlap_group.add_argument(
        "--forbid-overlaps",
        action="store_false",
        dest="allow_overlaps",
        help="включить прежний строгий исследовательский профиль",
    )
    parser.set_defaults(allow_overlaps=True)
    parser.add_argument(
        "--cutting-profile",
        choices=("continuous", "plate-11700"),
        default="continuous",
    )
    args = parser.parse_args()

    mosaic = read_mosaic(str(args.dxf))
    try:
        report = generate_comparison_report(
            mosaic,
            args.out_dir,
            tuple(args.algorithms),
            max_details=args.max_details,
            detail_penalty_kg=args.detail_penalty_kg,
            min_width_cells=args.min_width_cells,
            allow_overlaps=args.allow_overlaps,
            cutting_profile=args.cutting_profile,
        )
    except MissingRebarSpecificationError as error:
        raise SystemExit(f"Нельзя запустить детализацию: {error}") from error
    print(f"Готово: {report}")


if __name__ == "__main__":
    main()
