"""Разрешение переносимых путей полного инженерного manifest."""

import unicodedata

from rebar.golden import (
    EngineerInputSetDefinition,
    EngineerMetricStatus,
    EngineerReferenceCase,
    resolve_engineer_reference_files,
)
from rebar.models import Axis, Direction, Layer


def _nfd(value: str) -> str:
    return unicodedata.normalize("NFD", value)


def test_resolve_engineer_reference_accepts_unicode_normalisation(tmp_path):
    directions = (
        Direction(Layer.BOTTOM, Axis.X),
        Direction(Layer.BOTTOM, Axis.Y),
        Direction(Layer.TOP, Axis.X),
        Direction(Layer.TOP, Axis.Y),
    )
    case = EngineerReferenceCase(
        id="case",
        title="case",
        dataset_dir_name="Датасет",
        engineer_pdf_parts=("Выдача", "Решение.pdf"),
        input_sets=(
            EngineerInputSetDefinition(
                id="input",
                title="input",
                relative_directory=("Задание",),
                dxf_by_direction=tuple(
                    (direction, f"Направление {index}.dxf")
                    for index, direction in enumerate(directions)
                ),
            ),
        ),
        metric_status=EngineerMetricStatus.PDF_TEXT_UNREADABLE,
    )
    dataset = tmp_path / _nfd("Датасет")
    output = dataset / _nfd("Выдача")
    task = dataset / _nfd("Задание")
    output.mkdir(parents=True)
    task.mkdir()
    pdf = output / _nfd("Решение.pdf")
    pdf.write_bytes(b"pdf")
    for index in range(4):
        (task / _nfd(f"Направление {index}.dxf")).write_bytes(b"dxf")

    files = resolve_engineer_reference_files(case, tmp_path)

    assert files.engineer_pdf == pdf
    assert set(files.dxf_by_input_set["input"]) == set(directions)
