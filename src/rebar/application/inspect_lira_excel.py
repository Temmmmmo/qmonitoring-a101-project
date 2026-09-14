"""Прикладная проверка числового комплекта без подбора арматуры и изменения RVT."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from pathlib import Path

from rebar.lira import LiraPlate, read_lira_excel
from rebar.lira.models import AS_DIRECTIONS


def summarize_lira_plate(plate: LiraPlate, *, include_records: bool = False) -> dict:
    result = {
        "schema_version": "lira-numeric-source/v1", "status": "collected",
        "profile_id": plate.profile_id, "source_files": [asdict(s) for s in plate.sources],
        "node_count": len(plate.nodes), "element_count": len(plate.elements),
        "element_types": dict(sorted(Counter(e.element_type for e in plate.elements).items())),
        "unused_node_ids": list(plate.unused_node_ids),
        "bbox_xyz_mm": plate.bbox_xyz_mm, "input_coordinate_unit": plate.input_coordinate_unit,
        "units": "mm", "as_unit": "cm2/m", "demand_row_policy": "first_row_full_demand",
        "element_boundary_order": {"42": [1, 2, 3], "44": [1, 2, 4, 3]},
        "calculation_groups": [asdict(g) for g in plate.groups],
        "directions": [{
            "direction": {"layer": d.layer.value, "axis": d.axis.value},
            "export_column": f"AS{i + 1}", "cell_count": len(plate.elements),
            "minimum_as_cm2_m": min(e.required_as(d) for e in plate.elements),
            "maximum_as_cm2_m": max(e.required_as(d) for e in plate.elements),
            "different_second_row_count": sum(e.total_as_cm2_m[i] != e.second_row_as_cm2_m_unassigned[i]
                                              for e in plate.elements),
        } for i, d in enumerate(AS_DIRECTIONS)],
        "averaging": "not_applied", "axis_alignment": "not_confirmed",
        "revit_host_binding": "not_checked", "placement_eligible": False,
        "notes": [
            "Толщина группы — расчётная, не автоматически физическая толщина Revit-host.",
            "Вторая строка сохранена, но не назначена спросом по прочности.",
            "AS1–AS4 относятся к экспортным осям; их привязка к глобальным осям нужна перед подбором.",
            "Проверка входа не является разрешением на размещение арматуры.",
        ],
    }
    if include_records:
        result["nodes"] = [asdict(n) for n in plate.nodes]
        result["elements"] = [asdict(e) for e in plate.elements]
    return result


def inspect_lira_excel(nodes_path: str | Path, elements_path: str | Path,
                       reinforcement_path: str | Path, *, include_records: bool = False) -> dict:
    plate = read_lira_excel(nodes_path, elements_path, reinforcement_path)
    return summarize_lira_plate(plate, include_records=include_records)
