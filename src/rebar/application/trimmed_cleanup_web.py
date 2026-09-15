"""Deletion-only web stage, with the COMPLETE preceding trim packet as history.

The old exact-trim certificate is never applied to the smaller inventory. Source
FE and parametric zones remain untouched; current checks come from fresh cleanup.
"""
from copy import deepcopy
import hashlib
import math

from rebar.optimization.algorithms.trimmed_length_cleanup import prune_redundant_trimmed_bars
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS
from rebar.optimization.contracts.shaped_physical import Line3D, ShapedPhysicalBar
from rebar.optimization.services.shaped_geometry import shaped_batch_metrics, shaped_cutting_schedule
from rebar.optimization.services.tz_boundary_trim import trimming_domain
from rebar.reporting.composite_svg import render_composite_svg
from rebar.reporting.serialization import to_jsonable

from .physical_layout_recovery import _bytes

OUTPUT_KIND = "pruned-trimmed-physical-bars"


def pruned_trimmed_web_report(report, before, lanes, problem, host, *, stock_time_limit_s=10):
    """Fresh prune of a bound straight holes-trim output, not an arbitrary repair.

This application boundary accepts no replacement geometry from JSON: every input
graphic bar must equal the typed, freshly trimmed batch. Empty trim stays empty.
"""
    from .boundary_trim_web import _raw_bar

    if (report.get("output_kind") != "boundary-trimmed-physical-bars"
            or report.get("placement_eligible") is not False
            or report.get("engineering_approval") is not False
            or report["boundary_trim"]["respect_openings"] is not True):
        raise ValueError("Cleanup requires a separate fresh holes-trim result")
    if not before:
        if report.get("graphic_bar_plan_draft") is not None or report["front"][0]["physical_bar_count"] != 0:
            raise ValueError("Empty trim inventory disagrees with its report")
        return deepcopy(report)
    if any(not isinstance(b, ShapedPhysicalBar) or b.shape_kind != "straight" or len(b.segments) != 1
            or not isinstance(b.segments[0], Line3D) or b.demand_segment_indexes != (0,) for b in before):
        raise ValueError("Pruned graphic history supports straight single-main-leg bars only")
    source = report["graphic_bar_plan_draft"]
    if (source["schema_version"] != "graphic-bar-plan-draft/v1"
            or source["placement_eligible"] is not False or source["engineering_approval"] is not False
            or source["case_id"] != problem.case_id or source["respect_openings"] is not True):
        raise ValueError("Unsupported source trim packet for deletion-only cleanup")
    keyed = {}
    if [row["direction"] for row in source["directions"]] != [to_jsonable(d) for d in PLATE_DIRECTIONS]:
        raise ValueError("Source trim packet must retain all four ordered directions")
    for direction, rows in zip(PLATE_DIRECTIONS, source["directions"]):
        for raw in rows["bars"]:
            key = direction, raw["id"]
            if key in keyed:
                raise ValueError("Duplicate source trim bar")
            keyed[key] = raw
    if len(keyed) != len(before) or any(
            (bar.direction, bar.id) not in keyed or any(keyed[bar.direction, bar.id].get(k) != v
                for k, v in _raw_bar(bar).items()) for bar in before):
        raise ValueError("Source trim graphics differ from the fresh physical batch")
    baseline = shaped_batch_metrics(before)
    for field, metric in (("physical_bar_count", "physical_bar_count"),
                           ("additional_mass_kg", "mass_kg"), ("position_count", "position_count")):
        value = source["after"][field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value-baseline[metric]) > 1e-6:
            raise ValueError("Source trim metrics differ from its physical inventory")
    if source["after"]["source_zone_count"] != report["front"][0]["zone_count"]:
        raise ValueError("Source zone count changed between trim and cleanup")
    after, checks = prune_redundant_trimmed_bars(before, lanes, problem, host,
                                               stock_time_limit_s=stock_time_limit_s)
    if not checks["accepted_nonregression"]:
        raise ValueError("Cleanup proposal did not pass independent nonregression checks")
    result = deepcopy(report)
    metrics = checks["physical_metrics"]
    expected = {"physical_bar_count": metrics["physical_bar_count"],
        "additional_mass_kg": metrics["mass_kg"], "position_count": metrics["position_count"],
        "source_zone_count": source["after"]["source_zone_count"]}
    packet = {"schema_version": "graphic-bar-plan-pruned/v1", "units": "mm", "case_id": problem.case_id,
        "geometry_kind": "straight-bars-only", "source_stage": "redundant-trimmed-bars-pruned",
        "source_trim_packet": deepcopy(source), "source_trim_packet_json": _bytes(source).decode("utf-8"),
        "source_trim_packet_sha256": hashlib.sha256(_bytes(source)).hexdigest(),
        "retained_bar_ids": [{"direction": str(b.direction), "bar_id": b.id} for b in after],
        "expected": expected, "cleanup": deepcopy(checks),
        "placement_eligible": False, "engineering_approval": False}
    contours, holes = [], []
    for section in trimming_domain(host, respect_openings=True).sections:
        parts = [section.footprint] if section.footprint.geom_type == "Polygon" else section.footprint.geoms
        contours.extend(tuple(p.exterior.coords) for p in parts)
        holes.extend(tuple(ring.coords) for p in parts for ring in p.interiors)
    coverage = {r["direction"]: r for r in checks["coverage_after"]["control_40d"]["directions"]}
    presence = {r["direction"]: r for r in checks["coverage_after"]["geometric_presence"]["directions"]}
    for direction, original in zip(result["directions"], problem.direction_problems):
        key = original.demand.direction
        bars = tuple(b for b in after if b.direction == key)
        raw = [deepcopy(keyed[b.direction, b.id]) for b in bars]
        schedule = shaped_cutting_schedule(bars)
        candidate = direction["candidates"][0]
        candidate.update(output_kind=OUTPUT_KIND, physical_bars=raw,
            coverage=deepcopy(coverage[str(key)]), geometric_presence=deepcopy(presence[str(key)]),
            bar_schedule=to_jsonable(schedule),
            svg=render_composite_svg(original.demand, (), physical_bars=raw,
                host_rings_mm=tuple(contours), host_opening_rings_mm=tuple(holes)),
            overlay_svg=render_composite_svg(original.demand, (), physical_bars=raw,
                host_rings_mm=tuple(contours), host_opening_rings_mm=tuple(holes),
                source_zone_drafts=direction["source_zone_drafts"]),
            metrics={"zone_count": candidate["metrics"]["zone_count"], "physical_bar_count": len(bars),
                "position_count": len(schedule), "additional_mass_kg": math.fsum(p.total_mass_kg for p in schedule)})
    schedule = shaped_cutting_schedule(after)
    if (len(schedule) != expected["position_count"]
            or abs(math.fsum(p.total_mass_kg for p in schedule)-expected["additional_mass_kg"]) > 1e-6):
        raise ValueError("Pruned physical schedule and independently checked metrics disagree")
    result["front"] = [{"direction_candidate_indexes": [0]*4, "zone_count": expected["source_zone_count"],
        **{k: v for k, v in expected.items() if k != "source_zone_count"},
        "bar_schedule": to_jsonable(schedule), "stock_cutting": deepcopy(checks["stock_cutting"])}]
    result.pop("graphic_bar_plan_draft")
    result.update(output_kind=OUTPUT_KIND, trimmed_cleanup=checks, graphic_bar_plan_pruned=packet if after else None,
        status="trimmed_cleanup_requires_engineering_review",
        physical_placement_status="redundant_pieces_removed_new_checks_not_engineering_approved",
        blocking_check_ids=["trimmed-cleanup-engineering-review", "engineering-anchorage-and-Revit",
            *[name for name, value in checks["coverage_after"].items() if value["status"] != "pass"],
            *(["stock-cutting-zero-waste"] if checks["stock_cutting"]["status"] != "pass" else []),
            *(["physical-3d-collisions"] if checks["collisions"]["proven_collision_pair_count"]
              or checks["collisions"]["uncertain_pair_count"] else [])],
        warning=report["warning"] + f" Отдельно удалено резервных отрезков: {checks['removed_bar_count']}. "
            "Ни один оставшийся стержень не изменён; ранее покрытые части всех исходных КЭ сохранены "
            "для наличия стали и контрольных 40d. Это не устраняет уже существовавшие пробелы покрытия. "
            "Схема, масса и ведомость показывают очищенную партию; полный предыдущий состав обрезки сохранён "
            "в истории нового графического пакета. Длины не округлялись и не унифицировались.")
    return result
