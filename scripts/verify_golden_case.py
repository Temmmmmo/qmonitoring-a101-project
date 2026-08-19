#!/usr/bin/env python3
"""Проверить инженерный golden-case и собрать автономный HTML-отчёт."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.golden import (  # noqa: E402
    GOLDEN_CASES,
    GoldenPdfToolError,
    GoldenReferenceMismatchError,
    GoldenSourceNotFoundError,
    get_golden_case,
)
from rebar.reporting import generate_golden_report  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=sorted(GOLDEN_CASES),
        default="plate-zero-k09",
        help="стабильный идентификатор эталона",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT / "Дополнительные материалы",
        help="корень локальных материалов организаторов",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "artifacts/golden_plate_zero",
    )
    parser.add_argument("--dpi", type=int, default=120, help="разрешение превью PDF")
    args = parser.parse_args()

    try:
        report = generate_golden_report(
            get_golden_case(args.case),
            args.data_dir,
            args.out_dir,
            render_dpi=args.dpi,
        )
    except (
        GoldenPdfToolError,
        GoldenReferenceMismatchError,
        GoldenSourceNotFoundError,
    ) as error:
        raise SystemExit(f"Golden-case не собран: {error}") from error
    print(f"Golden-case подтверждён: {report}")


if __name__ == "__main__":
    main()
