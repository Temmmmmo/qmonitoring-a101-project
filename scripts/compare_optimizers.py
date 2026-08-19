#!/usr/bin/env python3
"""Сравнить алгоритмы раскладки на одном DXF и создать автономный HTML-отчёт."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.dxf_ingest import read_mosaic  # noqa: E402
from rebar.optimization import MissingRebarSpecificationError  # noqa: E402
from rebar.reporting import generate_comparison_report  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf", type=Path, help="путь к DXF с доступным совместимым .shk")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "artifacts/comparison")
    parser.add_argument(
        "--algorithms",
        nargs="+",
        default=["bbox", "bsp", "greedy", "greedy-priority", "agglomerative"],
    )
    parser.add_argument("--max-details", type=int, default=8)
    parser.add_argument("--detail-penalty-kg", type=float, default=0.0)
    parser.add_argument("--min-width-cells", type=int, default=2)
    parser.add_argument("--allow-overlaps", action="store_true")
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
        )
    except MissingRebarSpecificationError as error:
        raise SystemExit(f"Нельзя запустить детализацию: {error}") from error
    print(f"Готово: {report}")


if __name__ == "__main__":
    main()
