"""Публичные маленькие XLSX-фикстуры и необязательная сверка полного локального входа.

Workbook используется только для временных тестовых файлов, не авторинга документов пользователя.
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import subprocess
import sys
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook
from shapely.geometry import Polygon
from shapely.ops import unary_union

from rebar.application.inspect_lira_excel import inspect_lira_excel
from rebar.legend import build_legend, parse_recipe
from rebar.lira import read_lira_excel
from rebar.lira.models import AS_DIRECTIONS
from rebar.models import Band
from rebar.optimization.adapters.lira import build_lira_demand_map, build_lira_plate_demands
from rebar.optimization.contracts.problem import LayoutProblem
from rebar.optimization.services.reinforcement_area import recipe_area_cm2_m

GROUP = (
    "1 - Оболочка / h= 80.00 см/ Бетон B30/ Арматура: продольная Ax: A500, "
    "Ay: A500/ поперечная A500/ Шаг арматурных стержней 300 мм"
)
HEADERS = ["ГР", "Элемент", "AS1", "AS2", "AS3", "AS4", "ASW1", "ASW2", "Кратк.", "Длит."]


@pytest.fixture
def excel_rows():
    return [
        [
            ["Таблица узлов"],
            [None, "Координаты"],
            ["№ узла", "X\n(м)", "Y\n(м)", "Z\n(м)"],
            [1, 0, 0, -8.97],
            [2, 1, 0, -8.97],
            [3, 0, 1, -8.97],
            [4, 1, 1, -8.97],
            [5, 2, 0, -8.97],
        ],
        [
            ["Таблица элементов"],
            [None],
            [
                "№ элем",
                "Тип элем",
                "Кол.сечений",
                "Тип жестк",
                "Угол м.осей",
                "AX н",
                "AX к",
                "№№ узлов",
            ],
            [101, 44, 1, 2, "-", "-", "-", "1,2,3,4"],
            [205, 42, 1, 3, "-", "-", "-", "2,5,4"],
        ],
        [
            HEADERS.copy(),
            [GROUP],
            [1, 101, 7.2, 8.82, 9.0, 10.0],
            [1, 101, 7.2, 8.0, 7.2, 7.2],
            [GROUP.replace("1 -", "2 -").replace("80.00", "220.00")],
            [2, 205, 7.2, 12.3, 8.0, 7.2],
            [2, 205, 7.2, 11.0, 7.2, 7.2],
        ],
    ]


def _xlsx(rows, *, sheet=" ", dimensions=True, extra_sheet=False):
    book = Workbook()
    book.active.title = sheet
    for row in rows:
        book.active.append(row)
    if extra_sheet:
        book.create_sheet("extra")
    buffer = BytesIO()
    book.save(buffer)
    book.close()
    if dimensions:
        return buffer.getvalue()
    output = BytesIO()
    with (
        ZipFile(BytesIO(buffer.getvalue())) as source,
        ZipFile(output, "w", ZIP_DEFLATED) as target,
    ):
        import re

        for name in source.namelist():
            data = source.read(name)
            if name == "xl/worksheets/sheet1.xml":
                data = re.sub(rb"<dimension[^>]*/>", b"", data)
            target.writestr(name, data)
    return output.getvalue()


def _write_sources(tmp_path, rows, *, dimensions=True):
    paths = tuple(tmp_path / name for name in ("узлы.xlsx", "элементы.xlsx", "арматура.xlsx"))
    for i, (path, data) in enumerate(zip(paths, rows)):
        path.write_bytes(_xlsx(data, sheet="Таблица" if i == 2 else " ", dimensions=dimensions))
    return paths


@pytest.mark.parametrize("dimensions", [True, False])
def test_raw_import_preserves_ids_groups_both_rows_units_and_source_files(
    tmp_path, excel_rows, dimensions
):
    paths = _write_sources(tmp_path, excel_rows, dimensions=dimensions)
    before = [p.read_bytes() for p in paths]
    plate = read_lira_excel(*paths)
    assert [e.id for e in plate.elements] == [101, 205]
    assert plate.elements[0].total_as_cm2_m == (7.2, 8.82, 9, 10)
    assert plate.elements[0].second_row_as_cm2_m_unassigned == (7.2, 8, 7.2, 7.2)
    assert plate.elements[0].reinforcement_rows == (3, 4)
    assert plate.elements[1].reinforcement_rows == (6, 7)  # Not global odd/even rows.
    assert plate.elements[0].node_ids == (1, 2, 3, 4)
    assert Polygon(plate.elements[0].polygon_xy_mm).area == 1_000_000
    assert plate.bbox_xyz_mm == ((0, 0, -8970), (2000, 1000, -8970))
    assert [g.calculation_thickness_mm for g in plate.groups] == [800, 2200]
    assert [s.sheet for s in plate.sources] == [" ", " ", "Таблица"]
    assert [s.sha256 for s in plate.sources] == [hashlib.sha256(b).hexdigest() for b in before]
    assert [p.read_bytes() for p in paths] == before


def test_explicit_mm_units_and_unused_nodes(tmp_path, excel_rows):
    for row in excel_rows[0][3:]:
        row[1:] = [x * 1000 for x in row[1:]]
    excel_rows[0][2] = ["№ узла", "X (мм)", "Y (мм)", "Z (мм)"]
    excel_rows[0].append([99, 10_000_000, 0, 100])
    plate = read_lira_excel(*_write_sources(tmp_path, excel_rows))
    assert plate.input_coordinate_unit == "mm" and plate.unused_node_ids == (99,)
    assert plate.bbox_xyz_mm == ((0, 0, -8970), (2000, 1000, -8970))


@pytest.mark.parametrize(
    "case",
    [
        "duplicate-node",
        "duplicate-element",
        "missing-node",
        "missing-demand",
        "extra-demand",
        "missing-second",
        "mismatched-pair",
        "third-row",
        "group-change-mid-pair",
        "group-mismatch",
        "negative-as",
        "formula",
        "excel-error",
        "blank-as",
        "string-as",
        "boolean-as",
        "swapped-rows",
        "unknown-unit",
        "mixed-unit",
        "wrong-column",
        "nonplanar",
        "wrong-node-count",
        "duplicate-vertex",
        "wrong-node-order",
        "overlapping-elements",
        "wrong-type",
        "wrong-group-units",
        "empty-group",
        "missing-group",
        "bad-angle",
    ],
)
def test_malformed_source_never_silently_drops_or_reduces_demand(tmp_path, excel_rows, case):
    n, e, r = excel_rows
    if case == "duplicate-node":
        n.append(n[3].copy())
    elif case == "duplicate-element":
        e.append(e[3].copy())
    elif case == "missing-node":
        e[3][7] = "1,2,3,99"
    elif case == "missing-demand":
        r[:] = r[:4]
    elif case == "extra-demand":
        r[5][1] = r[6][1] = 999
    elif case == "missing-second":
        r.pop()
    elif case == "mismatched-pair":
        r[3][1] = 205
    elif case == "third-row":
        r.insert(4, r[3].copy())
    elif case == "group-change-mid-pair":
        r.pop(3)
    elif case == "group-mismatch":
        r[5][0] = 1
    elif case == "negative-as":
        r[2][2] = -1
    elif case == "formula":
        r[2][2] = "=1+2"
    elif case == "excel-error":
        r[2][2] = "#VALUE!"
    elif case == "blank-as":
        r[2][2] = None
    elif case == "string-as":
        r[2][2] = "7.2"
    elif case == "boolean-as":
        r[2][2] = True
    elif case == "swapped-rows":
        r[2], r[3] = r[3], r[2]
    elif case == "unknown-unit":
        n[2][1] = "X (см)"
    elif case == "mixed-unit":
        n[2][1] = "X (мм)"
    elif case == "wrong-column":
        r[0][2], r[0][3] = r[0][3], r[0][2]
    elif case == "nonplanar":
        n[3][3] = 0
    elif case == "wrong-node-count":
        e[3][7] = "1,2,3"
    elif case == "duplicate-vertex":
        n[6][1:] = n[5][1:]
    elif case == "wrong-node-order":
        e[3][7] = "1,2,4,3"
    elif case == "overlapping-elements":
        e[4][7] = "1,2,3"
    elif case == "wrong-type":
        e[3][1] = 41
    elif case == "wrong-group-units":
        r[1][0] = GROUP.replace("см", "мм")
    elif case == "empty-group":
        r.append([GROUP.replace("1 -", "3 -")])
    elif case == "missing-group":
        r.pop(1)
    elif case == "bad-angle":
        e[3][4] = "?"
    with pytest.raises(ValueError):
        read_lira_excel(*_write_sources(tmp_path, excel_rows))


def _bands():
    result = []
    for i, (threshold, label) in enumerate(
        ((8.483, "s300d18"), (25.45, "s300d18+s150d18"), (41.81, "s300d18+s150d18+s300d25"))
    ):
        recipe = parse_recipe(label)
        result.append(
            Band(
                i,
                None,
                label,
                threshold,
                recipe.background,
                recipe.additions[0] if len(recipe.additions) == 1 else None,
                recipe,
            )
        )
    return result


def test_numeric_adapter_preserves_actual_values_and_real_ids(tmp_path, excel_rows):
    plate = read_lira_excel(*_write_sources(tmp_path, excel_rows))
    direction = AS_DIRECTIONS[1]
    with pytest.raises(ValueError, match="Подтвердите"):
        build_lira_demand_map(plate, direction, _bands(), mapping_source="explicit-test")
    demand = build_lira_demand_map(
        plate, direction, _bands(), mapping_source="explicit-test", export_axes_are_global_xy=True
    )
    assert [c.id for c in demand.cells] == [101, 205]
    assert [c.level_index for c in demand.cells] == [1, 1]
    assert demand.meta["numeric_source_cells"][0]["required_as_cm2_m"] == 8.82
    assert all(c.aci == 0 for c in demand.cells) and all(
        level.aci is None for level in demand.levels
    )
    assert not demand.meta["placement_eligible"]
    assert demand.meta["averaging"] == "not_applied"
    assert [c.centroid for c in demand.cells] == [(500, 500), (4000 / 3, 1000 / 3)]
    assert demand.levels[2].recipe.additions == parse_recipe("s300d18+s150d18+s300d25").additions
    with pytest.raises(ValueError, match="несколько дополнительных"):
        LayoutProblem(demand)  # No loss of the middle component via legacy GA.


def test_threshold_is_not_proof_of_actual_recipe_capacity(tmp_path, excel_rows):
    plate = read_lira_excel(*_write_sources(tmp_path, excel_rows))
    bands = _bands()
    bands[0].threshold_as = 10  # 8.82 fits the label threshold but Ø18@300 cannot supply it.
    demand = build_lira_demand_map(
        plate,
        AS_DIRECTIONS[1],
        bands,
        mapping_source="explicit-test",
        export_axes_are_global_xy=True,
    )
    assert demand.cells[0].level_index == 1
    assert recipe_area_cm2_m(bands[0].reinforcement_recipe) == pytest.approx(math.pi * 18**2 / 120)


def test_uncovered_value_rotated_axis_and_incomplete_plate_are_rejected(tmp_path, excel_rows):
    plate = read_lira_excel(*_write_sources(tmp_path, excel_rows))
    with pytest.raises(ValueError, match="нет достаточного"):
        build_lira_demand_map(
            plate,
            AS_DIRECTIONS[1],
            _bands()[:1],
            mapping_source="test",
            export_axes_are_global_xy=True,
        )
    rotated = replace(
        plate, elements=(replace(plate.elements[0], local_axis_angle_deg=90), *plate.elements[1:])
    )
    with pytest.raises(ValueError, match="Поворот"):
        build_lira_demand_map(
            rotated,
            AS_DIRECTIONS[1],
            _bands(),
            mapping_source="test",
            export_axes_are_global_xy=True,
        )
    with pytest.raises(ValueError, match="четырёх"):
        build_lira_plate_demands(plate, {AS_DIRECTIONS[0]: _bands()}, mapping_sources={})
    maps = build_lira_plate_demands(
        plate,
        {d: _bands() for d in AS_DIRECTIONS},
        mapping_sources={d: "explicit-test" for d in AS_DIRECTIONS},
        export_axes_are_global_xy=True,
    )
    assert tuple(m.direction for m in maps) == AS_DIRECTIONS


def test_multiple_sheets_corrupt_archive_and_resource_limits(tmp_path, excel_rows, monkeypatch):
    from rebar.lira import excel

    paths = _write_sources(tmp_path, excel_rows)
    paths[0].write_bytes(_xlsx(excel_rows[0], extra_sheet=True))
    with pytest.raises(ValueError, match="один"):
        read_lira_excel(*paths)
    paths[0].write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="повреждённый"):
        read_lira_excel(*paths)
    paths = _write_sources(tmp_path, excel_rows)
    monkeypatch.setattr(excel, "MAX_ROWS", 3)
    with pytest.raises(ValueError, match="лимит"):
        read_lira_excel(*paths)


def test_cli_writes_new_report_without_overwriting_input_or_existing_output(tmp_path, excel_rows):
    paths = _write_sources(tmp_path, excel_rows)
    output = tmp_path / "report.json"
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "scripts/inspect_lira_excel.py"),
        "--nodes",
        str(paths[0]),
        "--elements",
        str(paths[1]),
        "--reinforcement",
        str(paths[2]),
        "--output",
        str(output),
        "--include-records",
    ]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    payload = json.loads(output.read_text())
    assert payload["status"] == "collected" and len(payload["directions"]) == 4
    assert len(payload["elements"]) == 2 and not payload["placement_eligible"]
    assert payload == json.loads(json.dumps(inspect_lira_excel(*paths, include_records=True)))
    before = output.read_bytes()
    again = subprocess.run(command, capture_output=True, text=True)
    assert again.returncode != 0 and output.read_bytes() == before


def test_real_local_source_reproduces_review_and_all_four_maps(
    lira_excel_sources, shk_full, two_background_top_x_sources
):
    plate = read_lira_excel(*lira_excel_sources)
    assert len(plate.nodes) == 2209 and len(plate.elements) == 2132
    assert [g.calculation_thickness_mm for g in plate.groups] == [800, 2200, 2030, 1680, 1330, 980]
    assert plate.bbox_xyz_mm == ((0, 0, -8970), (23600, 14000, -8970))
    assert not plate.unused_node_ids
    polygons = [Polygon(e.polygon_xy_mm) for e in plate.elements]
    assert all(p.is_valid for p in polygons)
    assert sum(p.area for p in polygons) == pytest.approx(unary_union(polygons).area, abs=1e-5)
    assert [max(e.required_as(d) for e in plate.elements) for d in AS_DIRECTIONS] == [
        89.1,
        57.78,
        50.59,
        37.79,
    ]
    report = inspect_lira_excel(*lira_excel_sources)
    assert [d["different_second_row_count"] for d in report["directions"]] == [160, 799, 126, 752]
    # Only the explicitly paired scale files for this actual input are used.
    top_x = two_background_top_x_sources[1]
    top_y = top_x.with_name(top_x.name.replace("Вх", "Ву"))
    if not top_y.exists():
        pytest.skip("нет явной шкалы Top Y")
    sources = dict(zip(AS_DIRECTIONS, (shk_full, top_x, shk_full, top_y)))
    maps = build_lira_plate_demands(
        plate,
        {d: build_legend(str(p)) for d, p in sources.items()},
        mapping_sources={d: str(p) for d, p in sources.items()},
        export_axes_are_global_xy=True,
    )
    assert all(len(m.cells) == 2132 for m in maps)
    top = maps[1]
    assert {c.id: c.level_index for c in top.cells if c.id in (5281, 5292)} == {5281: 1, 5292: 1}


def test_http_numeric_import_same_filenames_cannot_overwrite_roles(excel_rows, monkeypatch):
    import importlib
    from fastapi.testclient import TestClient

    module = importlib.import_module("rebar.web.app")
    paths_read = []
    inspect = module.inspect_lira_excel

    def track(*paths, **kwargs):
        paths_read.extend(paths)
        return inspect(*paths, **kwargs)

    monkeypatch.setattr(module, "inspect_lira_excel", track)
    response = TestClient(module.app).post(
        "/api/inspect-lira-excel",
        files={
            role: (
                "../../same.xlsx",
                _xlsx(rows),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            for role, rows in zip(("nodes", "elements", "reinforcement"), excel_rows)
        },
        data={"include_records": "true"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["element_count"] == 2 and len(payload["directions"]) == 4
    assert payload["elements"][0]["total_as_cm2_m"] == [7.2, 8.82, 9, 10]
    assert not payload["placement_eligible"]
    assert len(set(paths_read)) == 3 and all(not p.exists() for p in paths_read)
    assert all(s["filename"] == "same.xlsx" for s in payload["source_files"])


@pytest.mark.parametrize("bad", ["extension", "empty", "malformed", "too-large", "missing-role"])
def test_http_numeric_import_errors_are_explicit(excel_rows, monkeypatch, bad):
    import importlib
    from fastapi.testclient import TestClient

    module = importlib.import_module("rebar.web.app")
    files = {
        role: (f"{role}.xlsx", _xlsx(rows))
        for role, rows in zip(("nodes", "elements", "reinforcement"), excel_rows)
    }
    if bad == "extension":
        files["nodes"] = ("nodes.xls", b"abc")
    elif bad == "empty":
        files["nodes"] = ("nodes.xlsx", b"")
    elif bad == "malformed":
        files["nodes"] = ("nodes.xlsx", b"abc")
    elif bad == "too-large":
        monkeypatch.setattr(module, "MAX_UPLOAD_BYTES", 1)
    else:
        files.pop("nodes")
    response = TestClient(module.app).post("/api/inspect-lira-excel", files=files)
    assert response.status_code == {"empty": 400, "too-large": 413}.get(bad, 422)


def test_actual_imported_polygons_match_dxf_not_just_unordered_vertices(
    lira_excel_sources, two_background_top_x_sources
):
    from rebar.dxf_ingest import read_mosaic

    plate = read_lira_excel(*lira_excel_sources)
    dxf, shk = two_background_top_x_sources
    mosaic = read_mosaic(str(dxf), shk_path=str(shk))
    from shapely.strtree import STRtree

    polys = [Polygon(c.poly) for c in mosaic.cells]
    tree = STRtree(polys)
    matched = set()
    for element in plate.elements:
        poly = Polygon(element.polygon_xy_mm)
        i = int(tree.nearest(poly.centroid))
        assert i not in matched
        matched.add(i)
        assert poly.hausdorff_distance(polys[i]) < 1e-6
        assert poly.symmetric_difference(polys[i]).area < 1e-5
    assert len(matched) == len(mosaic.cells) == len(plate.elements) == 2132


def test_single_component_numeric_map_uses_existing_optimizer_and_validator(tmp_path, excel_rows):
    from rebar.optimization import AlgorithmRequest, LayoutConstraints, StrongestBBoxOptimizer
    from rebar.optimization.services import evaluate_layout
    plate = read_lira_excel(*_write_sources(tmp_path, excel_rows))
    demand = build_lira_demand_map(plate, AS_DIRECTIONS[1], _bands()[:2],
                                  mapping_source="synthetic-xy", export_axes_are_global_xy=True)
    problem = LayoutProblem(demand, LayoutConstraints(min_width_cells=1, enforce_zone_gap=False))
    request = AlgorithmRequest(max_details=1)
    solution = StrongestBBoxOptimizer().solve(problem, request)
    checked = evaluate_layout(problem, solution.zones, request)
    assert checked.valid and checked.metrics.under_reinforced_cell_count == 0
    assert checked.metrics.physical_bar_count > 0
    assert {c.id for c in demand.cells} == {101, 205}


@pytest.mark.parametrize("part", ["xl/workbook.xml", "xl/worksheets/sheet1.xml"])
def test_corrupt_xml_is_a_validation_error(tmp_path, excel_rows, part):
    paths = _write_sources(tmp_path, excel_rows)
    buffer = BytesIO()
    with ZipFile(paths[0]) as source, ZipFile(buffer, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name, b"<broken" if name == part else source.read(name))
    paths[0].write_bytes(buffer.getvalue())
    with pytest.raises(ValueError, match="повреждённый"):
        read_lira_excel(*paths)


@pytest.mark.parametrize("part", ["xl/vbaProject.bin", "xl/externalLinks/externalLink1.xml"])
def test_active_content_is_not_accepted(tmp_path, excel_rows, part):
    paths = _write_sources(tmp_path, excel_rows)
    with ZipFile(paths[0], "a") as archive:
        archive.writestr(part, b"unused")
    with pytest.raises(ValueError, match="макросы/внешние связи"):
        read_lira_excel(*paths)
