"""Optimistic ANY-axis presence bound; no phase/stock/40d/zone permission."""
from __future__ import annotations

import argparse
from pathlib import Path

from shapely.geometry import LineString, box
from shapely.ops import unary_union

from audit_trimmed_demand import _parts, audit_missing, load_trimmed_inputs
from rebar.application.assistant_inputs import source_record, verify_source_records
from rebar.application.physical_layout_recovery import _bytes
from rebar.models import Axis
from rebar.optimization.services.host_search_domain import recipe_service_half_width_mm


def any_axis_presence_domain(material, axis, diameter, service_half_width, *, maximum_parts=50000):
    """Exact open-station orthogonal sections, closures make the bound optimistic.

    Permit arbitrarily short bars, arbitrary q, symmetric maximum half-gap and
    any height from unioned sections. Only full transverse diameter is retained.
    Pointwise presence here is not anchored effective structural reinforcement.
    """
    along = 0 if axis is Axis.X else 1
    across = 1-along
    vertices = [p for poly in _parts(material) for ring in (poly.exterior, *poly.interiors) for p in ring.coords]
    if len(vertices) > 20000:
        raise ValueError("Bounded orthogonal material required")
    for poly in _parts(material):
        for ring in (poly.exterior, *poly.interiors):
            for a, b in zip(ring.coords, list(ring.coords)[1:]):
                if a[0] != b[0] and a[1] != b[1]:
                    raise ValueError("Exact orthogonal input required for event-section bound")
    stations = sorted({p[along] for p in vertices})
    qlo, qhi = material.bounds[across]-1, material.bounds[across+2]+1
    pieces = []
    def lines(geometry):
        if isinstance(geometry, LineString):
            return (geometry,)
        return tuple(p for child in getattr(geometry, "geoms", ()) for p in lines(child))
    for slo, shi in zip(stations, stations[1:]):
        s = (slo+shi)/2
        line = LineString(((s, qlo), (s, qhi))) if along == 0 else LineString(((qlo, s), (qhi, s)))
        for run in lines(material.intersection(line)):
            a, b = run.bounds[across]+diameter/2, run.bounds[across+2]-diameter/2
            if a > b:
                continue
            a -= service_half_width
            b += service_half_width
            pieces.append(box(slo, a, shi, b) if along == 0 else box(a, slo, b, shi))
            if len(pieces) > maximum_parts:
                raise ValueError("Complete any-axis event-piece budget exceeded")
    return unary_union(pieces)


def run(args):
    if args.output.exists():
        raise ValueError("New read-only audit output required")
    code = source_record(Path(__file__), role="any-axis-audit-code")
    inputs = load_trimmed_inputs(report_dir=args.report_dir, snapshot=args.snapshot,
        working_host_report=args.working_host_report, candidate_id=args.candidate_id)
    material = unary_union([s.footprint for s in inputs.host.sections])
    offers, recipe_rows = {}, []
    for p in inputs.problem.direction_problems:
        recipes = tuple(dict.fromkeys(level.recipe for level in p.demand.levels if level.recipe.additions))
        for recipe in recipes:
            spec = recipe.additions[0]
            half = recipe_service_half_width_mm(recipe)
            domain = any_axis_presence_domain(material, p.demand.direction.axis, spec.diameter, half)
            offers.setdefault(p.demand.direction, []).append((spec.diameter, spec.step, domain))
            recipe_rows.append({"direction": str(p.demand.direction), "diameter_mm": spec.diameter,
                "nominal_step_mm": spec.step, "maximum_service_half_width_mm": half,
                "optimistic_presence_domain_area_mm2": domain.area})
    result = audit_missing(inputs.problem, offers, inputs.host)
    result.update(schema_version="trimmed-any-axis-presence-bound/v1", source_files=(*inputs.source_files, code),
        recipes=recipe_rows, placement_eligible=False, engineering_approval=False,
        source_demand_removed=False, source_demand_transferred=False, actual_bar_geometry_changed=False,
        relaxed=["finite_source_windows", "fixed_q_and_joint_phases", "actual_Z_sections", "finite_cut_lengths",
            "bar_count", "position_count", "stock", "background_and_interbar_collisions", "control40d"],
        conclusion_scope="Necessary obstruction ONLY in original recipe pointwise service-width presence model; not engineering impossibility.")
    verify_source_records(result["source_files"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(_bytes(result))
    print({key: result[key] for key in ("uncovered_direction_FE_count", "categories_above_0_001mm2", "areas_mm2")})
    print([(x["direction"], x["cell_id"], x["inside_material_area_mm2"],
        x["over_opening_area_mm2"], x["outside_outer_area_mm2"]) for x in result["cells"]])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("report-dir", "snapshot", "working-host-report", "output"):
        parser.add_argument("--"+name, type=Path, required=True)
    parser.add_argument("--candidate-id", default="plate:52")
    run(parser.parse_args())
