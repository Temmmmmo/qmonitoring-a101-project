"""Публичные синтетические DXF-примеры для web-MVP."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import ezdxf
from ezdxf import units


@dataclass(frozen=True)
class DemoCase:
    """Описание воспроизводимого примера без материалов организаторов."""

    id: str
    title: str
    description: str
    filename: str
    mapping_id: str


IRREGULAR_PLATE_DEMO = DemoCase(
    id="irregular-plate-x",
    title="Плита с несколькими уровнями",
    description="96 КЭ · нижнее армирование · ось X · шесть уровней As",
    filename="Демо_Нижняя по оси X.dxf",
    mapping_id="plate-zero-d12-v1",
)

_DEMO_CASES = {IRREGULAR_PLATE_DEMO.id: IRREGULAR_PLATE_DEMO}
_ACI_BY_LEVEL = (181, 3, 2, 30, 1, 6)
_SCALE_BOUNDS_AS = (1.9, 3.8, 7.5, 11.0, 17.0, 24.0, 35.0)


def available_demo_cases() -> tuple[DemoCase, ...]:
    """Вернуть стабильный каталог встроенных демонстрационных задач."""

    return tuple(_DEMO_CASES.values())


def get_demo_case(case_id: str) -> DemoCase:
    """Найти пример по публичному идентификатору."""

    try:
        return _DEMO_CASES[case_id.strip().casefold()]
    except KeyError as error:
        raise ValueError(
            f"неизвестный демонстрационный пример {case_id!r}; "
            f"доступны: {sorted(_DEMO_CASES)}"
        ) from error


def _cell_level(column: int, row: int) -> int:
    """Задать несколько вложенных и разорванных цветовых областей."""

    level = 0
    if 1 <= column <= 10 and 1 <= row <= 6:
        level = 1
    if (2 <= column <= 6 and 2 <= row <= 4) or (
        7 <= column <= 10 and 4 <= row <= 6
    ):
        level = 2
    if (3 <= column <= 5 and 2 <= row <= 3) or (
        8 <= column <= 10 and 5 <= row <= 6
    ):
        level = 3
    if (4 <= column <= 5 and 2 <= row <= 4) or (
        8 <= column <= 9 and 4 <= row <= 5
    ):
        level = 4
    if (column == 5 and row in {2, 3}) or (column == 8 and row == 5):
        level = 5
    return level


def write_demo_dxf(case_id: str, destination: str | Path) -> DemoCase:
    """Создать небольшой настоящий DXF, проходящий через основной ingest."""

    case = get_demo_case(case_id)
    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)

    document = ezdxf.new("R2010")
    document.header["$INSUNITS"] = units.MM
    document.layers.add("KLEENKA", color=7)
    modelspace = document.modelspace()

    cell_size_mm = 500.0
    for row in range(8):
        for column in range(12):
            xmin = column * cell_size_mm
            ymin = row * cell_size_mm
            modelspace.add_3dface(
                [
                    (xmin, ymin, 0.0),
                    (xmin + cell_size_mm, ymin, 0.0),
                    (xmin + cell_size_mm, ymin + cell_size_mm, 0.0),
                    (xmin, ymin + cell_size_mm, 0.0),
                ],
                dxfattribs={
                    "layer": "KLEENKA",
                    "color": _ACI_BY_LEVEL[_cell_level(column, row)],
                },
            )

    scale = document.blocks.new("KLEENKA")
    for index, aci in enumerate(_ACI_BY_LEVEL):
        xmin = index * 100.0
        scale.add_solid(
            [
                (xmin, 0.0),
                (xmin + 80.0, 0.0),
                (xmin, 80.0),
                (xmin + 80.0, 80.0),
            ],
            dxfattribs={"color": aci},
        )
    for index, bound in enumerate(_SCALE_BOUNDS_AS):
        scale.add_attdef(
            tag=f"{index}S",
            insert=(index * 100.0, 100.0),
            text=f"{bound:g}",
            height=10.0,
        )
    reference = modelspace.add_blockref("KLEENKA", (0.0, -1000.0))
    reference.add_auto_attribs(
        {f"{index}S": f"{bound:g}" for index, bound in enumerate(_SCALE_BOUNDS_AS)}
    )

    document.saveas(output)
    return case
