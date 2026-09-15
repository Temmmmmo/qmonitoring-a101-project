"""Read the real local S1 sources, run the application, save a fresh review."""
import argparse
import json
import os
from pathlib import Path
from zipfile import ZipFile

from rebar.application.engineering_example import ENV_NAME, _checked_bytes, install_original_archive
from rebar.application.physical_layout_recovery import _bytes
from rebar.application.s1_example import EXAMPLE_ID, SOURCES, analyze_s1_example, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--replay-source-report", type=Path,
                        help="Revalidate an earlier source candidate against all original DXF, without rerunning GA")
    parser.add_argument("--normalize-source", action="store_true", help="Explicit merge/diameter normalization experiment")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a new output directory; prior runs are not overwritten")
    contents = {name: _checked_bytes(args.source_dir / name, digest) for _, _, name, digest in SOURCES}
    args.output.mkdir(parents=True)
    archive = args.output / "s1-original-dxf.zip"
    with ZipFile(archive, "x") as bundle:
        for name, content in contents.items():
            bundle.writestr(name, content)
    installed = args.output / "inputs"
    install_original_archive(archive, installed, example_id=EXAMPLE_ID)
    os.environ[ENV_NAME] = str(installed.resolve())
    print("Original S1 files verified; starting fresh application calculation", flush=True)
    if args.replay_source_report:
        from rebar.application.analyze_direction import load_direction_mosaic
        from rebar.application.flat_mvp import flat_mvp_source_web_report
        from rebar.optimization import LayoutConstraints, build_layout_problem, build_plate_problem
        from rebar.optimization.mappings.legacy_s1 import LEGACY_S1_D18
        from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
        source = json.loads(args.replay_source_report.read_text(encoding="utf-8"))
        if source["source_provenance"]["sources"] != metadata(available=True, status="ready")["sources"]:
            raise ValueError("Replay source provenance does not match the exact four original S1 files")
        constraints = LayoutConstraints(min_width_cells=2, cutting_profile="plate-11700",
                                         allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM)
        problem = build_plate_problem(tuple(build_layout_problem(load_direction_mosaic(
            installed / EXAMPLE_ID / name, mapping_id=LEGACY_S1_D18.id), constraints)
            for _, _, name, _ in SOURCES), case_id=EXAMPLE_ID)
        report = flat_mvp_source_web_report(problem, source, normalize_source=args.normalize_source)
        report["engineering_example"] = metadata(available=True, status="ready")
        report["run_mode"] = "original-DXF-revalidated-existing-source-candidate; NOT new GA"
    else:
        report = analyze_s1_example()
    (args.output / "web-report.json").write_bytes(_bytes(report))
    for key, filename in (("source_graphics", "source-isofields-zones.json"),
                          ("graphic_bar_plan_draft", "graphic-bar-plan.json")):
        if report.get(key):
            (args.output / filename).write_bytes(_bytes(report[key]))
    print({"status": report["status"], "output_kind": report.get("output_kind"),
        "metrics": [{k: v for k, v in row.items() if k not in ("bar_schedule", "stock_cutting")}
                    for row in report.get("front", [])[:1]], "mvp_checks": report.get("mvp_checks"),
        "blockers": report.get("blocking_check_ids")}, flush=True)


if __name__ == "__main__":
    main()
