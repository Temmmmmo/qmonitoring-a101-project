"""Present the accepted physical inventory without relabeling old zone geometry."""
from copy import deepcopy
import math

from rebar.optimization.services.bar_schedule import BarScheduleGroup, build_bar_schedule
from rebar.reporting.composite_svg import render_composite_svg
from rebar.reporting.serialization import to_jsonable
from rebar.reporting.source_graphics import build_source_graphics, render_source_graphics_svg


def physical_web_report(problem, recovery) -> dict:
    report = deepcopy(recovery.patterned_report)
    source_graphics = build_source_graphics(problem, report) if report.get("front") else None
    report["default_drawing_view"] = "source"
    report["source_graphics"] = source_graphics
    report["source_graphics_candidate_index"] = report.get("selected_index")
    if source_graphics:
        for direction, source in zip(report["directions"], source_graphics["directions"]):
            direction["source_svg"] = render_source_graphics_svg(source)
            direction["source_zone_drafts"] = deepcopy(source["zone_drafts"])
    report["warning"] = ("Исследовательская раскладка по исходным DXF. Никакие КЭ не отброшены. "
        "Приняты явные фазы и замена одиночной добавки на более сильную с повторной проверкой покрытия. "
        "Фактические границы, проёмы, высоты и 3D-коллизии в Revit не проверены; размещение не разрешено.")
    if recovery.packet is None:
        report["blocking_check_ids"] = sorted(set(report["blocking_check_ids"]) | set(recovery.review["permanent_blockers"]))
        report["warning"] += " Физическая обработка не завершена: показан только промежуточный расчёт зон."
        report["output_kind"] = "source-zones-only"
        return report
    review = recovery.review
    expected = review["expected"]
    accepted = recovery.normalization_report["accepted"]["raw_bars_by_direction"]
    coverage = {row["direction"]: row for row in review["source_original_coverage"]}
    if (not review["source_demand_preserved"] or review["source_axis_and_new40d_status"] != "pass"
            or any(row["status"] != "pass" or row["uncovered_cell_count"] for row in coverage.values())):
        raise ValueError("Cannot present physical bars without the original-demand certificate")
    count = 0
    masses = []
    for direction, original in zip(report["directions"], problem.direction_problems):
        key = str(original.demand.direction)
        bars = accepted[key]
        count += len(bars)
        schedule = build_bar_schedule(BarScheduleGroup(bar["id"], bar["diameter_mm"],
            bar["longitudinal_mm"][1] - bar["longitudinal_mm"][0], 1, bar["steel_class"]) for bar in bars)
        mass = math.fsum(row.total_mass_kg for row in schedule)
        masses.append(mass)
        source_candidate = direction["candidates"][report["front"][report["selected_index"]][
            "direction_candidate_indexes"][len(masses) - 1]]
        direction["source_zone_drafts"] = source_candidate["zone_drafts"]
        direction["candidates"] = [{"candidate_index": 0, "direction": direction["direction"],
            "metrics": {"zone_count": source_candidate["metrics"]["zone_count"], "physical_bar_count": len(bars),
                        "position_count": len(schedule), "additional_mass_kg": mass},
            "coverage": {**coverage[key], "physical_source_axis_and_new40d_status": "pass"},
            "svg": render_composite_svg(original.demand, (), physical_bars=bars),
            "overlay_svg": render_composite_svg(original.demand, (), physical_bars=bars,
                                                 source_zone_drafts=direction["source_zone_drafts"]),
            "host_preflight": None, "bar_schedule": to_jsonable(schedule),
            "physical_bars": deepcopy(bars), "installation_notes": source_candidate.get("installation_notes", []),
            # Retained parametric zones are source provenance, NOT normalized placement geometry.
            "zone_drafts": [], "output_kind": "normalized-physical-bars"}]
    if count != expected["physical_bar_count"] or abs(math.fsum(masses) - expected["additional_mass_kg"]) > 1e-6:
        raise ValueError("Displayed physical inventory differs from independent count/mass verification")
    report.update(output_kind="normalized-physical-bars", status=recovery.status,
        selected_index=0, default_drawing_view="combined", source_graphics_candidate_index=0,
        front=[{"direction_candidate_indexes": [0] * 4,
            "zone_count": expected["source_zone_count"], "physical_bar_count": expected["physical_bar_count"],
            "position_count": expected["position_count"], "additional_mass_kg": expected["additional_mass_kg"],
            "bar_schedule": deepcopy(review["bar_schedule"]), "stock_cutting": deepcopy(review["stock_cutting"])}],
        same_plane_conflicts=deepcopy(review["same_plane_conflicts"]),
        blocking_check_ids=deepcopy(review["permanent_blockers"]),
        source_physical_metrics_before_normalization=deepcopy(review["prior_metrics"]),
        physical_review=deepcopy(review),
        normalization_status=recovery.normalization_report["status"],
        diagnostic_front_before_cutting=[], length_balance_attempts=[])
    report["original_source_zones"] = deepcopy(recovery.packet["source_zones"])
    report["source_zone_metrics"] = {"source_zone_count": expected["source_zone_count"],
        "physical_bar_count_before_normalization": sum(c["bar_count"] for direction in source_graphics["directions"]
            for zone in direction["zone_drafts"] for c in zone["components"]),
        "additional_mass_kg_before_normalization": math.fsum(c["mass_kg"] for direction in source_graphics["directions"]
            for zone in direction["zone_drafts"] for c in zone["components"])}
    # The physical packet is diagnostic rollback-only, never an apply command.
    report["physical_trial_packet"] = deepcopy(recovery.packet)
    report["warning"] += " Физический вид и общая ведомость показывают одну и ту же партию после обработки. "\
        "Исходные изополя и зоны показаны отдельно и не заменяют физический результат."
    return report
