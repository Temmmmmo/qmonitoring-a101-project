"""Однокнопочный публичный пример через настоящий четырёхнаправленный DXF-сценарий.

Файлы создаются только во временном каталоге запроса. Это не профиль реальной
плиты и не сохранённый удачный ответ: оптимизатор запускается заново.
"""
from pathlib import Path
import struct
from tempfile import TemporaryDirectory

import ezdxf

from rebar.legend import parse_recipe
from rebar.models import Layer
from rebar.optimization.contracts.plate import PLATE_DIRECTIONS
from rebar.optimization.services.reinforcement_area import recipe_area_cm2_m

from .analyze_composite_plate import CompositeDirectionSettings, analyze_composite_plate
from .analyze_plate import PlateDirectionSource
from .demo import IRREGULAR_PLATE_DEMO, _ACI_BY_LEVEL, _cell_level, write_demo_dxf

DEMO_ID = "demo-composite-four-directions-v1"


def _write_sources(folder: Path) -> tuple[PlateDirectionSource, ...]:
    original = folder / IRREGULAR_PLATE_DEMO.filename
    write_demo_dxf(IRREGULAR_PLATE_DEMO.id, original)
    labels = ["s300d18"] + [f"s300d18+s150d18+s300d{d}" for d in (20, 22, 25, 28, 32)]
    thresholds = [recipe_area_cm2_m(parse_recipe(label)) for label in labels]
    bounds = [*thresholds, thresholds[-1] + 1]
    sources = []
    for i, direction in enumerate(PLATE_DIRECTIONS):
        part = "Нижнее" if direction.layer is Layer.BOTTOM else "Верхнее"
        dxf_path = folder / f"DEMO_composite_{part} армирование {direction.axis.value}.dxf"
        shk_path = dxf_path.with_suffix(".shk")
        document = ezdxf.readfile(original)
        for face in document.modelspace().query('3DFACE[layer=="KLEENKA"]'):
            column, row = round(face.dxf.vtx0.x / 500), round(face.dxf.vtx0.y / 500)
            # Same FE mesh, different demand locations in each direction.
            level = _cell_level(11 - column if i % 2 else column, 7 - row if i >= 2 else row)
            face.dxf.color = _ACI_BY_LEVEL[level]
        attributes = list(document.blocks["KLEENKA"].query("ATTDEF"))
        for reference in document.modelspace().query('INSERT[name=="KLEENKA"]'):
            attributes.extend(reference.attribs)
        for attribute in attributes:
            attribute.dxf.text = f"{bounds[int(attribute.dxf.tag[:-1])]:.6f}"
        document.saveas(dxf_path)
        shk_path.write_bytes(b"".join(struct.pack("<ffHB", bounds[j], bounds[j + 1], j, len(label))
                                      + label.encode("ascii") for j, label in enumerate(labels)))
        sources.append(PlateDirectionSource(dxf_path, shk_path))
    return tuple(sources)


def analyze_composite_demo() -> dict:
    """Без пользовательских путей, внешних сервисов, БД и параметров реального проекта."""
    with TemporaryDirectory(prefix="rebar-composite-demo-") as folder:
        settings = tuple(CompositeDirectionSettings(direction, 0, 150, 50, "A500",
            "Синтетический DEMO-профиль: не параметры реального проекта") for direction in PLATE_DIRECTIONS)
        report = analyze_composite_plate(_write_sources(Path(folder)), settings,
            maximum_candidates=64, solver_time_limit_s=2, cutting_profile="plate-11700-batch",
            maximum_cutting_overhead_pct=5, case_id="Демо · плита 6 × 4 м · четыре направления")
    report["demo"] = {"id": DEMO_ID, "source_kind": "synthetic", "project_parameters": False,
        "description": "Синтетическая плита 6 × 4 м, по 96 КЭ в четырёх направлениях. "
                       "Начало фона 0 мм, смещения добавок +150/+50 мм, A500; "
                       "каталог 11700, предел прироста массы 5%. "
                       "Эти параметры заданы только для примера: высоты и границы настоящей плиты не проверены."}
    report["warning"] = "ДЕМОНСТРАЦИЯ НА СИНТЕТИЧЕСКИХ ДАННЫХ. " + report["warning"]
    return report
