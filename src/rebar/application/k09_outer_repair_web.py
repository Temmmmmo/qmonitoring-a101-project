"""Explicit K09 outer-only delivery; never changes the ordinary holes workflow."""
from copy import deepcopy
from dataclasses import replace
import hashlib

from rebar.optimization.services.shaped_fe_repair import ResearchLayerProfile, layer_elevations
from rebar.optimization.services.solid_host import SolidHostSection
from rebar.optimization.services.tz_boundary_trim import trimming_domain
from .boundary_trim_web import _fresh_recovery, _render_trimmed_report
from .opening_relocation import source_service_lanes
from .physical_web_report import physical_web_report
from .working_host import load_working_host_json
from .working_solid_host import inspect_working_solid, MAX_WORKING_REPORT_BYTES


def _render_k09_dual_exterior(problem, report, raw, lanes, source_sha, host_bytes, *, repair=True):
    measured, host_check = inspect_working_solid(load_working_host_json(host_bytes, maximum_bytes=MAX_WORKING_REPORT_BYTES))
    outer = trimming_domain(measured, respect_openings=False)
    footprint = outer.sections[0].footprint
    for section in outer.sections[1:]:
        footprint = footprint.intersection(section.footprint)
    bottom, top = outer.sections[0].bottom_z_mm, outer.sections[-1].top_z_mm
    if top-bottom != 200:
        raise ValueError('Explicit K09 dual-exterior delivery requires a 200mm host')
    domain = replace(outer, sections=(SolidHostSection(bottom, top, footprint),), volume_mm3=footprint.area*(top-bottom))
    def elevations(_domain, direction, diameter, profile):
        return layer_elevations(measured, direction, diameter, profile)
    result = _render_trimmed_report(problem, deepcopy(report), raw, lanes, source_sha, domain, host_check, host_bytes,
        profile=ResearchLayerProfile(), elevations=elevations, stock_time_limit_s=10,
        respect_openings=False, repair_flat_deficits=repair)
    result['k09_mvp_scope'] = {'profile_id': 'flat200-top-subset-bottom-dual-exterior-mvp/v1',
        'host_id': host_check['host_id'], 'thickness_mm': top-bottom, 'host_sha256': hashlib.sha256(host_bytes).hexdigest(),
        'openings_checked': False, 'cover_checked': False, 'height_irregularities_checked': False,
        'both_native_exterior_projections_required': True, 'actual_Revit_readback': 'not_checked',
        'original_FE_geometry_changed': False, 'source_demand_removed': False}
    result['warning'] = ('К09 MVP: тела всей прямой партии внутри обоих внешних контуров плиты 200 мм. '
        'Отверстия, перепады и защитный слой не проверены. Исходная потребность сохранена полностью; '
        'покрытие, контрольные 40d, раскрой и условные коллизии пересчитаны, незакрытые гейты не скрыты. '
        'Совместимый JSON предназначен для диагностического Rebar Review в копии соответствующей плиты; '
        'не инженерный выпуск. Реальный Revit/readback здесь не проверен.')
    return result


def k09_outer_repaired_web_report(problem, recovery, working_host_bytes, *, confirm_identity_xy):
    """Fresh checked recovery → dual exterior cut → bounded repair → honest JSON."""
    if problem.case_id != 'k09-typical-3-14' or confirm_identity_xy is not True:
        raise ValueError('K09 and explicit identity XY confirmation required')
    raw, sha = _fresh_recovery(recovery, problem, 10)
    lanes = source_service_lanes(recovery.patterned_report, problem,
                                candidate_index=recovery.patterned_report['selected_index'])
    return _render_k09_dual_exterior(problem, physical_web_report(problem, recovery), raw, lanes, sha, working_host_bytes)
