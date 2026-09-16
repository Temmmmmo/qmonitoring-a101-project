"""Reuse a fresh K09 source: physical cut to BOTH native exterior projections."""
import argparse
import hashlib
import json
from pathlib import Path

from audit_k09_repair_residuals import read_case
from rebar.application.k09_outer_repair_web import _render_k09_dual_exterior


def run(inputs, prior, host_path, output):
    if output.exists():
        raise ValueError('Refusing to overwrite a prior delivery')
    source = json.loads((prior/'source-web-report.json').read_text())
    previous = json.loads((prior/'web-report.json').read_text())
    problem, lanes, _ = read_case(inputs, previous)
    host_bytes = host_path.read_bytes()
    raw = {f'{row["direction"]["layer"]}-{row["direction"]["axis"]}': row['candidates'][0]['physical_bars']
        for row in source['directions']}
    sha = previous['graphic_bar_plan_repaired']['source_trim_packet']['source_report_sha256']
    results = {}
    for repair, name in ((False, 'trimmed-web-report.json'), (True, 'web-report.json')):
        report = _render_k09_dual_exterior(problem, source, raw, lanes, sha, host_bytes, repair=repair)
        report['k09_mvp_scope'].update({
            'source_stage': str(prior/'source-web-report.json'),
            'source_web_sha256': hashlib.sha256((prior/'source-web-report.json').read_bytes()).hexdigest()})
        results[name] = report
    output.mkdir(parents=True)
    results['graphic-bar-plan-draft.json'] = results['trimmed-web-report.json']['graphic_bar_plan_draft']
    results['graphic-bar-plan-repaired.json'] = results['web-report.json'].get('graphic_bar_plan_repaired')
    for name, data in results.items():
        (output/name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    check = results['web-report.json']['flat_trim_repair']
    print(json.dumps({'metrics': check['physical_metrics'], 'presence': check['geometric_presence']['uncovered_cell_count'],
        'control40d': check['coverage_with_control_40d']['uncovered_cell_count'],
        'external': check['material_boundary_failures_after'], 'pairs': check['collisions']['proven_collision_pair_count'],
        'elapsed_s': check['search']['elapsed_s'], 'budget_exhausted': check['search']['budget_exhausted']}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--prior', type=Path, required=True)
    parser.add_argument('--host', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.inputs, args.prior, args.host, args.output)
