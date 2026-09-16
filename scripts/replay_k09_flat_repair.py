"""One fresh K09 control and bounded outer-only repair, never an S1 host."""
import argparse
import hashlib
import json
from pathlib import Path
import time

from rebar.models import Axis, Direction, Layer
from rebar.application.analyze_composite_plate import CompositeDirectionSettings
from rebar.application.analyze_plate import PlateDirectionSource
from rebar.application.assistant_inputs import analyze_assistant_sources
from rebar.application.boundary_trim_web import _fresh_recovery, _render_trimmed_report
from rebar.application.engineering_example import EXAMPLE_ID, MAPPING_ID, PROFILE_SOURCE, SOURCES, example_metadata
from rebar.application.opening_relocation import source_service_lanes
from rebar.application.physical_layout_recovery import recover_physical_layout
from rebar.application.physical_web_report import physical_web_report
from rebar.application.working_host import load_working_host_json
from rebar.application.working_solid_host import inspect_working_solid, MAX_WORKING_REPORT_BYTES
from rebar.optimization.contracts.physical import PhysicalNormalizationConfig
from rebar.optimization.services.shaped_fe_repair import ResearchLayerProfile, layer_elevations
from rebar.optimization.services.tz_boundary_trim import trimming_domain


def run(inputs, host_path, output):
    if output.exists():
        raise ValueError('Refusing to overwrite a prior control')
    started = time.monotonic()
    sources, settings = [], []
    for (layer, axis, name, digest) in SOURCES:
        path = inputs/name
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('Original K09 DXF SHA mismatch')
        sources.append(PlateDirectionSource(path, mapping_id=MAPPING_ID))
        settings.append(CompositeDirectionSettings(Direction(Layer(layer), Axis(axis)), 0, 100, 0,
                                                    'A500', PROFILE_SOURCE, 'left'))
    host_bytes = host_path.read_bytes()
    measured, host_check = inspect_working_solid(load_working_host_json(host_bytes, maximum_bytes=MAX_WORKING_REPORT_BYTES))
    if host_check['host_id'] != 11020633 or measured.sections[-1].top_z_mm-measured.sections[0].bottom_z_mm != 200:
        raise ValueError('Expected exact K09 Floor11020633/200mm')
    print('Fresh K09 source/normalization started', flush=True)
    source = analyze_assistant_sources(tuple(sources), case_id=EXAMPLE_ID, maximum_source_bars=1227)
    provenance = {'mode': 'fresh-four-original-dxf', 'case_id': EXAMPLE_ID,
        'sources': example_metadata(available=True, status='ready')['sources'], **source.provenance}
    recovery = recover_physical_layout(source.problem, source.solution, tuple(settings),
        normalization_config=PhysicalNormalizationConfig(allow_diameter_increase=True), source_provenance=provenance)
    source_report = physical_web_report(source.problem, recovery)
    raw, sha = _fresh_recovery(recovery, source.problem, 10)
    lanes = source_service_lanes(recovery.patterned_report, source.problem,
                                candidate_index=recovery.patterned_report['selected_index'])
    domain = trimming_domain(measured, respect_openings=False)
    # The original 200mm measured-Z/cover-derived axis profile stays EXACTLY
    # frozen for matching the previous no-holes control. Cover is not a gate.
    def elevations(_domain, direction, diameter, profile):
        return layer_elevations(measured, direction, diameter, profile)
    trimmed = _render_trimmed_report(source.problem, json.loads(json.dumps(source_report)), raw, lanes, sha,
        domain, host_check, host_bytes, profile=ResearchLayerProfile(), elevations=elevations,
        stock_time_limit_s=10, respect_openings=False)
    print('Fresh trim complete; bounded repair started', flush=True)
    repaired = _render_trimmed_report(source.problem, json.loads(json.dumps(source_report)), raw, lanes, sha,
        domain, host_check, host_bytes, profile=ResearchLayerProfile(), elevations=elevations,
        stock_time_limit_s=10, respect_openings=False, repair_flat_deficits=True)
    for report in (source_report, trimmed, repaired):
        report['engineering_example'] = example_metadata(available=True, status='ready')
    repaired['k09_mvp_scope'] = {'host_id': 11020633, 'thickness_mm': 200,
        'host_sha256': hashlib.sha256(host_bytes).hexdigest(), 'openings_checked': False,
        'cover_checked': False, 'actual_Revit_readback': 'not_checked',
        'profile_basis': 'frozen measured-Z research axis profile; no S1 800mm substitution',
        'height_sections_basis': 'same conservative exterior sections as matching noholes control',
        'original_FE_geometry_changed': False, 'source_demand_removed': False}
    output.mkdir(parents=True)
    reports = {'source-web-report.json': source_report, 'trimmed-web-report.json': trimmed,
        'web-report.json': repaired, 'graphic-bar-plan-draft.json': trimmed['graphic_bar_plan_draft'],
        'graphic-bar-plan-repaired.json': repaired.get('graphic_bar_plan_repaired')}
    for name, report in reports.items():
        (output/name).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    checks = repaired['flat_trim_repair']
    summary = {'elapsed_s': time.monotonic()-started, 'source': source_report['front'][0],
        'trimmed': trimmed['boundary_trim']['physical_metrics'], 'repaired': checks['physical_metrics'],
        'presence': checks['geometric_presence']['uncovered_cell_count'],
        'control40d': checks['coverage_with_control_40d']['uncovered_cell_count'],
        'external': checks['material_boundary_failures_after'], 'search': checks['search'],
        'collisions': checks['collisions']['proven_collision_pair_count'], 'stock': checks['stock_cutting']['status']}
    summary['source'].pop('bar_schedule', None)
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--host', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.inputs, args.host, args.output)
