"""Independent complete variant reports; never mix geometry or checks across choices."""
from copy import deepcopy
import hashlib
import json


def _geometry_key(report):
    point = report["front"][report["selected_index"]]
    if len(report.get('directions', ())) != 4 or len(point.get('direction_candidate_indexes', ())) != 4:
        return None
    geometry = []
    for direction, index in zip(report["directions"], point["direction_candidate_indexes"]):
        candidate = direction["candidates"][index]
        bars = candidate.get("physical_bars")
        if bars is None:
            return None
        geometry.append(sorted(json.dumps({k: v for k, v in bar.items()
            if k in ("diameter_mm", "steel_class", "coordinate_mm", "longitudinal_mm", "start_mm", "end_mm", "axis_z_mm", "shape", "segments_mm")},
            sort_keys=True, allow_nan=False) for bar in bars))
    return hashlib.sha256(json.dumps(geometry, sort_keys=True).encode()).hexdigest()


def combine_layout_variants(reports):
    """Keep complete per-choice payloads (including source graphics and Revit JSON).

    First choice lives at the root for backwards compatibility; additional reports
    live only once. The client switches the whole report, not just its metric row.
    """
    accepted, seen = [], set()
    for report in reports:
        if not report.get("front") or report.get("selected_index") is None:
            continue
        key = _geometry_key(report)
        if key is None or key in seen:
            continue
        seen.add(key)
        accepted.append(report)
    if not accepted:
        return deepcopy(reports[0])
    result = deepcopy(accepted[0])
    points = [r["front"][r["selected_index"]] for r in accepted]
    labels = [[] for _ in points]
    for field, label in (("additional_mass_kg", "Меньше массы"),
                         ("physical_bar_count", "Меньше стержней"), ("position_count", "Меньше позиций")):
        best = min(range(len(points)), key=lambda i: (points[i][field], i))
        labels[best].append(label)
    result["layout_variants"] = [{"label": " · ".join(labels[i]) or "Другой баланс",
        "geometry_sha256": _geometry_key(report), "metrics": deepcopy(points[i]),
        "report": None if i == 0 else deepcopy(report)} for i, report in enumerate(accepted)]
    result["variant_selection_scope"] = "Distinct independently checked candidates, not an engineering-approved or global Pareto front"
    return result


def build_layout_variants(selections, evaluate, *, maximum_variants=3):
    """Backfill after geometry dedup; failed candidates never erase good choices.

    Source ingestion and request validation happen before this bounded processing
    loop. If every candidate fails, propagate a failure rather than invent a result.
    """
    reports, rejections, seen = [], [], set()
    for selection in selections:
        try:
            report = evaluate(selection)
        except ValueError as error:
            rejections.append({'candidate_id': selection.provenance['candidate_id'], 'reason': str(error)[:2000]})
            continue
        key = _geometry_key(report) if report.get('front') and report.get('selected_index') is not None else None
        if key is None:
            rejections.append({'candidate_id': selection.provenance['candidate_id'],
                               'reason': 'Complete physical geometry not returned'})
            continue
        if key in seen:
            continue
        seen.add(key)
        reports.append(report)
        if len(seen) >= maximum_variants:
            break
    if not reports:
        raise ValueError('No complete layout variant survived processing: ' + '; '.join(r['reason'] for r in rejections))
    result = combine_layout_variants(reports)
    result['variant_rejections'] = rejections
    result['requested_variant_count'] = maximum_variants
    result['source_variant_budget'] = len(selections)
    return result
