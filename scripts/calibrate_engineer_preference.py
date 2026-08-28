#!/usr/bin/env python3
"""Обучить один вес выбора Парето-точки и выполнить leave-one-project-out."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from rebar.learning import calibrate_preference, load_benchmark_observation
from rebar.reporting import generate_preference_calibration_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "benchmark_json",
        nargs="+",
        type=Path,
        help="минимум три benchmark.json разных инженерских проектов",
    )
    parser.add_argument("--grid-points", type=int, default=101)
    parser.add_argument("--target-mass-weight", type=float, default=0.5)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=REPO_ROOT / "artifacts" / "engineer_preference_calibration",
    )
    args = parser.parse_args()

    observations = tuple(
        load_benchmark_observation(path) for path in args.benchmark_json
    )
    calibration = calibrate_preference(
        observations,
        target_mass_weight=args.target_mass_weight,
        grid_points=args.grid_points,
    )
    report = generate_preference_calibration_report(
        calibration,
        observations,
        args.out_dir,
    )
    print(f"Готово: {report}")


if __name__ == "__main__":
    main()
