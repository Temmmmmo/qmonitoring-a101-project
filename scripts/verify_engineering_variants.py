"""Fresh real preset run, with a compact independently reproducible variant summary."""
import argparse
import json
import logging
from pathlib import Path
from time import perf_counter

from rebar.application.engineering_example import analyze_engineering_example


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--example', default='k09-typical-3-14')
    parser.add_argument('--output', type=Path, default=Path('artifacts/engineering_variants_2026_09_16'))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('ezdxf').setLevel(logging.WARNING)
    started = perf_counter()
    print('Fresh preset run: ' + args.example, flush=True)
    report = analyze_engineering_example(args.example)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    variants = report.get('layout_variants', [])
    summary = []
    for i, variant in enumerate(variants):
        current = report if i == 0 else variant['report']
        point = variant['metrics']
        checks = current['boundary_trim']
        summary.append({'variant': i+1, 'label': variant['label'], 'geometry_sha256': variant['geometry_sha256'],
            'mass_kg': point['additional_mass_kg'], 'bars': point['physical_bar_count'],
            'positions': point['position_count'], 'outside': checks['external_boundary_failures_after'],
            'uncovered_FE': checks['geometric_presence']['uncovered_cell_count'],
            'control40d_FE': checks['coverage_with_control_40d']['uncovered_cell_count'],
            'stock': point['stock_cutting']['status']})
    result = {'example': args.example, 'elapsed_s': perf_counter()-started, 'variants': summary}
    (args.output / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
