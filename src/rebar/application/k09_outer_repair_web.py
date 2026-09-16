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
from .physical_layout_recovery import _bytes


def k09_dxf_outer_web_report(problem, recovery):
    """Default web MVP: clip actual bodies to the source mesh's outer contour.

    Not the measured native contour: the Review plugin must still check the live
    floor. The 200mm layer model is explicit and does not invent a Revit snapshot.
    """
    from .flat_mvp import flat_mvp_domain, _label_flat_report, flat_mvp_source_web_report, FlatMvpLayers
    if problem.case_id != 'k09-typical-3-14':
        raise ValueError('K09 source mesh required')
    if recovery.packet is None:
        result = flat_mvp_source_web_report(problem, recovery.patterned_report,
            layers=FlatMvpLayers(id='k09-dxf-exterior-flat200/v1', thickness_mm=200), repair_deficits=True,
            layer_profile=ResearchLayerProfile(), elevation_policy=layer_elevations)
        result['source_stock_recovery_status'] = recovery.status
        result['physical_normalization_completed'] = False
    else:
        raw, sha = _fresh_recovery(recovery, problem, 10)
        lanes = source_service_lanes(recovery.patterned_report, problem,
                                    candidate_index=recovery.patterned_report['selected_index'])
        mesh_host, declaration = flat_mvp_domain(problem)
        footprint = mesh_host.sections[0].footprint
        domain = replace(mesh_host, sections=(SolidHostSection(0, 200, footprint),),
                         volume_mm3=footprint.area*200)
        declaration['thickness_mm'] = 200
        result = _render_trimmed_report(problem, physical_web_report(problem, recovery), raw, lanes, sha,
            domain, {'host_id': None, 'geometry': declaration}, _bytes(declaration),
            profile=ResearchLayerProfile(), elevations=layer_elevations, stock_time_limit_s=10,
            respect_openings=False, repair_flat_deficits=True)
        result = _label_flat_report(result, declaration)
        result['physical_normalization_completed'] = True
    result['placement_profile'] = {'id': 'k09-dxf-exterior-flat200/v1', 'measured_in_Revit': False,
        'engineering_approval': False, 'layer_policy_id': ResearchLayerProfile().id,
        'orthogonal_axis_inset_mm': 16,
        'note': 'К09: плоская модель 200 мм, внешний контур DXF; X снаружи, Y глубже на 16 мм. Не снимок Revit.'}
    result['warning'] = ('К09 MVP: стержни обработаны по внешнему контуру исходной КЭ-сетки. '
        'Отверстия, перепады и защитный слой не учитываются. Исходная потребность сохранена; '
        'покрытие, контрольные 40d и раскрой проверяются отдельно. Модель толщиной 200 мм условная; '
        'совпадение с текущей плитой Revit проверяется плагином перед созданием. Не инженерный выпуск.')
    if result['mvp_checks']['outer_boundary'] != 'pass':
        raise ValueError('K09 variant still exceeds the source outer contour after repair')
    return result


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
