"""Revalidate the saved complete S1 source with fresh DXFs; never rerun GA."""
from pathlib import Path
import json
import hashlib
from rebar.application.s1_example import SOURCES, EXAMPLE_ID
from rebar.application.analyze_direction import load_direction_mosaic
from rebar.application.flat_mvp import flat_mvp_source_web_report
from rebar.optimization import LayoutConstraints, build_layout_problem, build_plate_problem
from rebar.optimization.mappings.legacy_s1 import LEGACY_S1_D18
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM


def replay(folder):
    constraints = LayoutConstraints(min_width_cells=2, cutting_profile='plate-11700', allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM)
    problems = []
    for _, _, name, digest in SOURCES:
        path = folder/'inputs'/EXAMPLE_ID/name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        problems.append(build_layout_problem(load_direction_mosaic(path, mapping_id=LEGACY_S1_D18.id), constraints))
    problem = build_plate_problem(tuple(problems), case_id=EXAMPLE_ID)
    source = json.loads((folder.parent/'fresh-v1/web-report.json').read_text())
    return flat_mvp_source_web_report(problem, source, repair_deficits=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    report = replay(args.folder)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False), encoding='utf-8')
    if report.get('graphic_bar_plan_repaired'):
        args.output.with_name('graphic-bar-plan-repaired.json').write_text(
            json.dumps(report['graphic_bar_plan_repaired'], ensure_ascii=False), encoding='utf-8')
    checks = report['flat_trim_repair']
    print(json.dumps({'output_kind': report['output_kind'], 'metrics': checks['physical_metrics'],
        'presence': checks['geometric_presence']['uncovered_cell_count'],
        'control40d': checks['coverage_with_control_40d']['uncovered_cell_count'],
        'pairs': checks['collisions']['proven_collision_pair_count'],
        'search': checks['search']}, ensure_ascii=False), flush=True)
