#!/usr/bin/env python3
"""Проверить 11 инженерских выдач и собрать датасет слабых plate-level меток."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.reporting import generate_engineer_dataset_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO_ROOT / "Дополнительные материалы",
        help="корень локальных материалов организаторов",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "artifacts" / "engineer_dataset",
    )
    args = parser.parse_args()

    json_path, markdown_path = generate_engineer_dataset_report(args.data_dir, args.out_dir)
    print(f"JSON-инвентарь: {json_path}")
    print(f"Краткий отчёт: {markdown_path}")


if __name__ == "__main__":
    main()
