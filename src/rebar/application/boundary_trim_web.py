"""Explicit physical boundary cuts for a fresh web calculation and real host.

The original source view stays unchanged. The new inventory is NOT accepted by
the legacy unchanged-axis/40d packet: geometry, FE presence, control anchorage and
stock are freshly checked and reported separately, including failed checks.
"""
from copy import deepcopy
import hashlib
import math

from rebar.models import Axis
from rebar.optimization.algorithms.tz_boundary_trim import trim_straight_bars_to_outer_boundary
from rebar.optimization.contracts.physical import PhysicalBar
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS
from rebar.optimization.services.shaped_fe_repair import ResearchLayerProfile, layer_elevations
from rebar.optimization.services.shaped_geometry import shaped_cutting_schedule, straight_bar_from_physical
from rebar.optimization.services.tz_boundary_trim import POLICY, check_boundary_trim
from rebar.optimization.services.tz_outer_scope import outer_scope_domain
from rebar.reporting.composite_svg import render_composite_svg
from rebar.reporting.serialization import to_jsonable

from .opening_relocation import source_service_lanes
from .physical_bar_trial import build_physical_bar_trial
from .physical_layout_recovery import PhysicalLayoutRecoveryResult, _bytes
from .physical_web_report import physical_web_report
from .working_host import load_working_host_json
from .working_solid_host import MAX_WORKING_REPORT_BYTES, inspect_working_solid

OUTPUT_KIND = "boundary-trimmed-physical-bars"
BINDING = "user-confirmed-identity-XY/research"


def _fresh_recovery(recovery, problem, stock_time_limit_s):
    if not isinstance(recovery, PhysicalLayoutRecoveryResult) or recovery.packet is None:
        raise ValueError("Обработка границы требует полной физической партии, не только исходных зон")
    for data, content in ((recovery.patterned_report, recovery.patterned_report_bytes),
            (recovery.normalization_report, recovery.normalization_report_bytes),
            (recovery.packet, recovery.packet_bytes), (recovery.review, recovery.review_bytes)):
        if _bytes(data) != content:
            raise ValueError("Данные физического расчёта изменились после формирования исходного пакета")
    source_sha = hashlib.sha256(recovery.patterned_report_bytes).hexdigest()
    raw_sha = hashlib.sha256(recovery.normalization_report_bytes).hexdigest()
    if (recovery.packet["source_report_sha256"] != source_sha or recovery.packet["raw_report_sha256"] != raw_sha
            or recovery.review["packet_sha256"] != hashlib.sha256(recovery.packet_bytes).hexdigest()):
        raise ValueError("Нарушена связь исходных зон, физической партии и независимой проверки")
    raw = recovery.normalization_report["accepted"]["raw_bars_by_direction"]
    fresh = build_physical_bar_trial(recovery.patterned_report, raw, original_problem=problem,
        source_report_sha256=source_sha, raw_report_sha256=raw_sha, stock_time_limit_s=stock_time_limit_s)
    for key in ("directions", "source_zones", "expected", "manual_joint_tasks"):
        if fresh.packet[key] != recovery.packet[key]:
            raise ValueError("Исходная физическая партия не воспроизводится при независимой проверке")
    return raw, source_sha


def _raw_bar(bar):
    along = 0 if bar.direction.axis is Axis.X else 1
    segment = bar.segments[0]
    return {"id": bar.id, "steel_class": bar.steel_class, "diameter_mm": bar.diameter_mm,
        "longitudinal_mm": [segment.start_mm[along], segment.end_mm[along]],
        "coordinate_mm": segment.start_mm[1-along], "source_bar_ids": list(bar.source_bar_ids)}


def _graphic_packet(problem, before, after, checks, source_sha, host_sha, zone_count):
    originals = {(str(bar.direction), bar.id): _raw_bar(bar) for bar in before}
    parent = {(row["direction"], piece): row["source_bar_id"] for row in checks["piece_mapping"]
              for piece in row["piece_ids"]}
    directions = []
    before_directions = []
    for direction in PLATE_DIRECTIONS:
        rows = []
        for bar in after:
            if bar.direction != direction:
                continue
            original_id = parent[(str(direction), bar.id)]
            original = originals[(str(direction), original_id)]
            raw = _raw_bar(bar)
            rows.append({**raw, "original_bar_id": original_id,
                "original_longitudinal_mm": original["longitudinal_mm"],
                "original_coordinate_mm": original["coordinate_mm"],
                "axis_nudged": raw["coordinate_mm"] != original["coordinate_mm"],
                "physically_cut": raw["longitudinal_mm"] != original["longitudinal_mm"]})
        directions.append({"direction": to_jsonable(direction), "bars": rows})
        before_directions.append({"direction": to_jsonable(direction),
            "bars": [_raw_bar(b) for b in before if b.direction == direction]})
    return {"schema_version": "graphic-bar-plan-draft/v1", "units": "mm", "case_id": problem.case_id,
        "geometry_kind": "straight-bars-only", "source_stage": "physically-trimmed-to-outer-contour",
        "source_report_sha256": source_sha, "source_host_report_sha256": host_sha,
        "provenance_status": "sha256_recorded", "crop_policy_id": POLICY,
        "radius_sized_edge_axis_nudge_enabled": True,
        "source_to_revit_xy_mm": [0, 0], "binding_source": BINDING,
        "coverage_policy": "physical-main-leg-presence-NOT-anchorage",
        "original_FE_geometry_changed": False, "source_demand_removed": False,
        "checks": {"anchorage_40d": checks["coverage_with_control_40d"]["status"],
                   "coverage": checks["geometric_presence"]["status"],
                   "stock_cutting": checks["stock_cutting"]["status"],
                   "outer_boundary": {"status": "fail" if checks["external_boundary_failures_after"] else "pass",
                                      "failure_count": checks["external_boundary_failures_after"]},
                   "collisions_3d": {"status": "fail" if (checks["collisions"]["proven_collision_pair_count"] or
                        checks["collisions"]["uncertain_pair_count"]) else "pass",
                        "proven_pair_count": checks["collisions"]["proven_collision_pair_count"],
                        "uncertain_pair_count": checks["collisions"]["uncertain_pair_count"]}},
        "before": {"physical_bar_count": checks["physical_metrics_before"]["physical_bar_count"],
                   "additional_mass_kg": checks["physical_metrics_before"]["mass_kg"],
                   "directions": before_directions},
        "after": {"physical_bar_count": checks["physical_metrics"]["physical_bar_count"],
                  "additional_mass_kg": checks["physical_metrics"]["mass_kg"],
                  "position_count": checks["physical_metrics"]["position_count"], "source_zone_count": zone_count},
        "piece_mapping": deepcopy(checks["piece_mapping"]), "directions": directions,
        "removed_wholly_external_bars": [{k: row[k] for k in ("direction", "bar_id", "reason")}
                                        for row in checks.get("removed_wholly_external_bars", ())],
        "placement_eligible": False, "engineering_approval": False}


def boundary_trim_web_report(problem, recovery, working_host_bytes, *, confirm_identity_xy,
                             stock_time_limit_s=30):
    if confirm_identity_xy is not True:
        raise ValueError("Подтвердите совпадение XY исходных DXF и плиты; автоматической привязки нет")
    snapshot = load_working_host_json(working_host_bytes, maximum_bytes=MAX_WORKING_REPORT_BYTES)
    host, host_check = inspect_working_solid(snapshot)
    raw, source_sha = _fresh_recovery(recovery, problem, stock_time_limit_s)
    lanes = source_service_lanes(recovery.patterned_report, problem,
                                candidate_index=recovery.patterned_report["selected_index"])
    profile = ResearchLayerProfile()
    physical = tuple(PhysicalBar(row["id"], direction, row["steel_class"], row["diameter_mm"],
        row["coordinate_mm"], tuple(row["longitudinal_mm"]), tuple(row["source_bar_ids"]))
        for direction in PLATE_DIRECTIONS for row in raw[str(direction)])
    before = tuple(straight_bar_from_physical(bar,
        axis_z_mm=layer_elevations(host, bar.direction, bar.diameter_mm, profile)[0],
        placement_profile_id=profile.id) for bar in physical)
    after, mapping = trim_straight_bars_to_outer_boundary(before, host, lanes=lanes, nudge_edge_axis=True,
                                                        discard_empty_intersections=True)
    checks = check_boundary_trim(before, after, mapping, lanes, problem, host,
                                 stock_time_limit_s=stock_time_limit_s, nudge_edge_axis=True,
                                 discard_empty_intersections=True)
    report = physical_web_report(problem, recovery)
    host_sha = hashlib.sha256(working_host_bytes).hexdigest()
    zone_count = report["front"][0]["zone_count"]
    graphic = _graphic_packet(problem, before, after, checks, source_sha, host_sha, zone_count)
    # Keep all section outlines, not the bounding box or a fabricated hole-free rectangle.
    contours = []
    for section in outer_scope_domain(host).sections:
        parts = [section.footprint] if section.footprint.geom_type == "Polygon" else section.footprint.geoms
        contours.extend(tuple(p.exterior.coords) for p in parts)
    coverage = {row["direction"]: row for row in checks["coverage_with_control_40d"]["directions"]}
    presence = {row["direction"]: row for row in checks["geometric_presence"]["directions"]}
    for direction, original, graphical in zip(report["directions"], problem.direction_problems, graphic["directions"]):
        key = str(original.demand.direction)
        bars = tuple(b for b in after if b.direction == original.demand.direction)
        schedule = shaped_cutting_schedule(bars) if bars else ()
        candidate = direction["candidates"][0]
        candidate.update(output_kind=OUTPUT_KIND, physical_bars=deepcopy(graphical["bars"]),
            coverage=deepcopy(coverage[key]), geometric_presence=deepcopy(presence[key]),
            bar_schedule=to_jsonable(schedule), host_preflight=None,
            svg=render_composite_svg(original.demand, (), physical_bars=graphical["bars"],
                                     host_rings_mm=tuple(contours)),
            metrics={"zone_count": candidate["metrics"]["zone_count"], "physical_bar_count": len(bars),
                     "position_count": len(schedule), "additional_mass_kg": math.fsum(p.total_mass_kg for p in schedule)})
    schedule = shaped_cutting_schedule(after) if after else ()
    metrics = checks["physical_metrics"]
    if (len(schedule) != metrics["position_count"] or
            abs(math.fsum(p.total_mass_kg for p in schedule)-metrics["mass_kg"]) > 1e-6):
        raise ValueError("Физическая ведомость после обрезки не совпадает с независимыми метриками")
    report["front"] = [{"direction_candidate_indexes": [0]*4, "zone_count": zone_count,
        "physical_bar_count": metrics["physical_bar_count"], "position_count": metrics["position_count"],
        "additional_mass_kg": metrics["mass_kg"], "bar_schedule": to_jsonable(schedule),
        "stock_cutting": deepcopy(checks["stock_cutting"])}]
    for key in ("physical_trial_packet", "physical_review", "same_plane_conflicts", "normalization_status"):
        report.pop(key, None)
    report.update(output_kind=OUTPUT_KIND, status="boundary_trim_requires_engineering_review",
        physical_placement_status="physically_trimmed_new_checks_not_engineering_approved",
        coverage_policy="separate-original-FE-presence-and-actual-main-leg-control-40d",
        placement_eligible=False, engineering_approval=False, boundary_trim=checks,
        graphic_bar_plan_draft=graphic if after else None,
        blocking_check_ids=["boundary-trim-engineering-review", *checks["blockers"]],
        working_host={"source_report_sha256": host_sha, "host_id": host_check["host_id"],
            "geometry": host_check["geometry"], "source_to_revit_xy_mm": [0, 0], "binding_source": BINDING},
        placement_profile={**to_jsonable(profile), "measured_in_Revit": False,
            "engineering_approval": False, "note": "X ближе к обеим граням; Y с отступом 16 мм. "
            "Исследовательское назначение высот, не измеренная арматура или подтверждённый профиль Revit."},
        warning="Стержни физически укорочены/разделены по внешнему контуру рабочего снимка, не скрыты на рисунке. "
            "Исходные КЭ и зоны не изменены. Наличие стали не доказывает анкеровку: проверка прежних 40d "
            "и новый раскрой показаны отдельно и могут не выполняться. Проёмы и защитный слой исключены "
            "из этой операции, внешний контур с вырезами сохранён. Высоты исследовательские; "
            "Если тело на краевой оси не помещалось, разрешён явный сдвиг оси ровно на радиус внутрь, "
            "с повторной проверкой исходного окна и фона. "
            "Полностью внешние стержни не оставляют отрезков; их полный список сохранён отдельно, "
            "а исходная потребность проверена без удаления КЭ. "
            "существующий фон Revit и инженерная пригодность не подтверждены. Старый пакет размещения не применяется.")
    return report
