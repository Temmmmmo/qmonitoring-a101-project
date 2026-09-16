"""Exact remaining FE geometry from fresh DXFs and a repaired lane manifest."""
import argparse
import json
from pathlib import Path
from shapely.geometry import Polygon
from shapely.ops import unary_union
from rebar.models import Axis, Direction, Layer
from rebar.application.analyze_direction import load_direction_mosaic
from rebar.application.engineering_example import SOURCES, MAPPING_ID, EXAMPLE_ID
from rebar.optimization import LayoutConstraints, build_layout_problem, build_plate_problem
from rebar.optimization.contracts.physical import PhysicalBar, PhysicalSourceBar
from rebar.optimization.contracts.opening_relocation import SourceServiceLane
from rebar.optimization.services.cutting import PLATE_11700_CUT_LENGTHS_MM
from rebar.optimization.services.shaped_geometry import straight_bar_from_physical
from rebar.optimization.services.tz_boundary_trim import geometry_presence_offers
from rebar.optimization.services.opening_relocation import lane_map


def read_case(inputs, report):
    constraints = LayoutConstraints(min_width_cells=2, cutting_profile='plate-11700', allowed_cut_lengths_mm=PLATE_11700_CUT_LENGTHS_MM)
    problem = build_plate_problem(tuple(build_layout_problem(load_direction_mosaic(inputs/name, mapping_id=MAPPING_ID), constraints)
        for _, _, name, _ in SOURCES), case_id=EXAMPLE_ID)
    packet = report['graphic_bar_plan_repaired']
    lanes = []
    for row in packet['repair']['source_lanes']:
        raw = dict(row['source'])
        raw['direction'] = Direction(Layer(raw['direction']['layer']), Axis(raw['direction']['axis']))
        for key in ('installed_interval_mm', 'required_interval_mm'):
            raw[key] = tuple(raw[key])
        lane = {k: v for k, v in row.items() if k != 'source_fe_ids'}
        lane['source'] = PhysicalSourceBar(**raw)
        for key in ('axis_window_mm', 'service_half_widths_mm'):
            lane[key] = tuple(lane[key])
        lanes.append(SourceServiceLane(**lane))
    bars = []
    for row in packet['directions']:
        direction = Direction(Layer(row['direction']['layer']), Axis(row['direction']['axis']))
        for raw in row['bars']:
            bar = PhysicalBar(raw['id'], direction, raw['steel_class'], raw['diameter_mm'], raw['coordinate_mm'],
                tuple(raw['longitudinal_mm']), tuple(raw['source_bar_ids']))
            bars.append(straight_bar_from_physical(bar, axis_z_mm=0, placement_profile_id='residual-2D-only'))
    return problem, tuple(lanes), tuple(bars)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    problem, lanes, bars = read_case(args.inputs, report)
    offers = geometry_presence_offers(bars, lane_map(lanes))
    for original in problem.direction_problems:
        for cell in original.demand.cells:
            recipe = original.demand.level(cell.level_index).recipe.additions
            if not recipe:
                continue
            spec = recipe[0]
            union = unary_union([p for d, s, p in offers.get(original.demand.direction, ()) if d >= spec.diameter and s <= spec.step])
            lost = Polygon(cell.poly).difference(union)
            if lost.area > 0:
                print(json.dumps({'direction': str(original.demand.direction), 'cell_id': cell.id,
                    'diameter': spec.diameter, 'step': spec.step, 'area_mm2': lost.area,
                    'geometry': lost.__geo_interface__}), flush=True)
