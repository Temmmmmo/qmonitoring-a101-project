"""Explicit planar MVP domain from the original FE mesh, never a Revit snapshot."""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import math

from shapely.geometry import Polygon
from shapely.ops import unary_union

from rebar.models import Axis, Layer
from rebar.optimization.services.solid_host import OrthogonalSolidHost, SolidHostSection

from .boundary_trim_web import _render_trimmed_report, _trim_web_report
from .physical_layout_recovery import _bars_from_source_geometry, _bytes, _raw

PROFILE_ID = "mvp-flat-original-mesh-exterior-no-holes-no-cover/v1"


@dataclass(frozen=True)
class FlatMvpLayers:
    id: str = "mvp-flat-t800-X-outer-Y-offset36/v1"
    thickness_mm: float = 800
    orthogonal_offset_mm: float = 36


def flat_mvp_domain(problem):
    """Preserve exact external concavities, fill only closed interior rings."""
    geometries = []
    for direction in problem.direction_problems:
        polygons = [Polygon(cell.poly) for cell in direction.demand.cells]
        if not polygons or any(not p.is_valid or p.is_empty or p.area <= 0 for p in polygons):
            raise ValueError("MVP needs a valid complete source FE mesh")
        geometry = unary_union(polygons)
        if geometry.geom_type != "Polygon" or not geometry.is_valid:
            raise ValueError("MVP supports one connected slab; disconnected pieces are not bridged")
        geometries.append(geometry)
    if len(geometries) != 4 or any(not g.equals(geometries[0]) for g in geometries[1:]):
        raise ValueError("The four source meshes must cover the same exact geometry")
    mesh = geometries[0]
    outer = Polygon(mesh.exterior)
    profile = FlatMvpLayers()
    domain = OrthogonalSolidHost((SolidHostSection(0, profile.thickness_mm, outer),),
                                0, 0, 0, outer.area * profile.thickness_mm, 0)
    declaration = {"schema_version": "flat-mvp-domain/v1", "profile_id": PROFILE_ID,
        "case_id": problem.case_id, "units": "mm", "geometry_basis": "original-DXF-FE-mesh-exterior",
        "exterior_mm": list(map(list, outer.exterior.coords)), "thickness_mm": profile.thickness_mm,
        "ignored_mesh_hole_count": len(mesh.interiors),
        "ignored_mesh_hole_area_mm2": math.fsum(Polygon(r).area for r in mesh.interiors),
        "openings_checked": False, "height_irregularities_checked": False, "cover_checked": False,
        "original_FE_geometry_changed": False, "original_FE_values_changed": False,
        "actual_Revit_host_checked": False, "placement_eligible": False}
    return domain, declaration


def flat_mvp_elevations(host, direction, diameter, profile):
    if (profile != FlatMvpLayers() or type(diameter) is not int
            or diameter not in (10, 12, 14, 16, 18, 20, 22, 25, 28, 32, 36)):
        raise ValueError("Unsupported explicit S1 flat-layer profile or diameter")
    inset = 0 if direction.axis is Axis.X else profile.orthogonal_offset_mm
    bottom = inset + diameter / 2
    top = profile.thickness_mm - inset - diameter / 2
    return (top, bottom) if direction.layer is Layer.TOP else (bottom, top)


def flat_mvp_web_report(problem, recovery, *, stock_time_limit_s=10):
    host, declaration = flat_mvp_domain(problem)
    report = _trim_web_report(problem, recovery, host,
        {"host_id": None, "geometry": declaration}, _bytes(declaration),
        profile=FlatMvpLayers(), elevations=flat_mvp_elevations,
        stock_time_limit_s=stock_time_limit_s, respect_openings=False)
    return _label_flat_report(report, declaration)


def flat_mvp_source_web_report(problem, patterned_report, *, stock_time_limit_s=10, normalize_source=False,
                               repair_deficits=False):
    """A failed batch cut cannot hide valid source geometry; no trial is fabricated."""
    from rebar.reporting.source_graphics import build_source_graphics, render_source_graphics_svg
    from .physical_bar_trial import _revalidate_source_geometry
    from .opening_relocation import _lanes_from_source_geometry

    report = deepcopy(patterned_report)
    source_sha = hashlib.sha256(_bytes(patterned_report)).hexdigest()
    if not report.get("front"):
        candidates = report.get("diagnostic_front_before_cutting", ())
        if not candidates:
            raise ValueError("No complete source geometry to show; input demand not discarded")
        report["front"] = [deepcopy(candidates[0])]
        report["selected_index"] = 0
    selected = report["selected_index"]
    certificates, source_refs, coverage, retained = _revalidate_source_geometry(report, problem, selected)
    sources = _bars_from_source_geometry(certificates, retained)
    lanes = _lanes_from_source_geometry(sources, retained)
    raw = _raw(sources, source=True)
    if type(normalize_source) is not bool:
        raise ValueError("Explicit normalization policy required")
    if normalize_source:
        from rebar.optimization.algorithms.physical_normalization import normalize_physical_bars
        from rebar.optimization.contracts.physical import PhysicalNormalizationConfig
        from rebar.reporting.serialization import to_jsonable
        from .physical_bar_trial import _validate_bars
        normalized = normalize_physical_bars(sources, config=PhysicalNormalizationConfig(
            allow_diameter_increase=True, time_limit_s=45, stock_balance_time_limit_s=5))
        raw = _raw(normalized.bars)
        _validate_bars(raw, source_refs)  # All source axes/owners/material and new40d, no stock waiver.
        report["flat_source_normalization"] = to_jsonable(normalized)
    graphics = build_source_graphics(problem, report)
    report["source_graphics"] = graphics
    report["original_source_zones"] = retained
    point = report["front"][selected]
    for row, source, index in zip(report["directions"], graphics["directions"], point["direction_candidate_indexes"]):
        row["source_svg"] = render_source_graphics_svg(source)
        row["source_zone_drafts"] = deepcopy(source["zone_drafts"])
        row["candidates"] = [deepcopy(row["candidates"][index])]
    report.update(front=[point], selected_index=0, source_graphics_candidate_index=0,
                  default_drawing_view="combined", source_geometry_coverage=coverage,
                  front_scope="source-geometry-only-stock-separately-checked")
    host, declaration = flat_mvp_domain(problem)
    report = _render_trimmed_report(problem, report, raw, lanes, source_sha,
        host, {"host_id": None, "geometry": declaration}, _bytes(declaration),
        profile=FlatMvpLayers(), elevations=flat_mvp_elevations,
        stock_time_limit_s=stock_time_limit_s, respect_openings=False, repair_flat_deficits=repair_deficits)
    return _label_flat_report(report, declaration)


def _label_flat_report(report, declaration):
    report.pop("working_host", None)
    report["mvp_domain"] = declaration
    report["placement_profile"] = {"id": FlatMvpLayers().id, "measured_in_Revit": False,
        "engineering_approval": False,
        "note": "Условная плоская плита 800 мм; X снаружи, Y глубже на 36 мм. Не измерение Revit."}
    report["warning"] = ("MVP: плоская плита по внешнему контуру исходной КЭ-сетки. "
        "Отверстия, перепады высоты и защитный слой исключены по принятому допущению. "
        "Стержни физически укорочены по контуру; исходная потребность не удалена. "
        "Покрытие, 40d и раскрой пересчитаны отдельно. Коллизии относятся только к условной раскладке; "
        "соответствие реальной 3D-модели не проверено. Экспорт в Revit — просмотр или диагностические Rebar "
        "в копии через совместимый плагин; не инженерный выпуск и не разрешение на монтаж.")
    graphic = report.get("graphic_bar_plan_draft")
    if graphic:
        # Existing transport carries a domain digest, never a fabricated native-host report.
        graphic["binding_source"] = "MVP FLAT DXF EXTERIOR; holes/heights/cover excluded; NOT measured Revit; XY to confirm"
        graphic["checks"]["collisions_3d"] = {"status": "not_checked", "proven_pair_count": None,
                                              "uncertain_pair_count": None}
    report["mvp_checks"] = {
        "outer_boundary": "fail" if report["boundary_trim"]["external_boundary_failures_after"] else "pass",
        "original_demand_presence": report["boundary_trim"]["geometric_presence"]["status"],
        "control_40d": report["boundary_trim"]["coverage_with_control_40d"]["status"],
        "stock_11700": report["boundary_trim"]["stock_cutting"]["status"],
        "openings": "out_of_scope", "height_irregularities": "out_of_scope", "cover": "out_of_scope",
        "actual_Revit_geometry": "not_checked"}
    return report
