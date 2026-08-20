"""Общие фикстуры тестов. Пути к реальным данным (кириллица, пробелы) — только здесь."""

import unicodedata
from pathlib import Path

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "Дополнительные материалы"
MOSAIC_DIR = DATA_DIR / "Изополя(мозаики) армирования"


def _normalised(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _find_data_dir(name: str) -> Path:
    expected = _normalised(name)
    if not DATA_DIR.is_dir():
        return DATA_DIR / name
    for candidate in DATA_DIR.iterdir():
        if candidate.is_dir() and _normalised(candidate.name) == expected:
            return candidate
    return DATA_DIR / name


VERIFY_DIR = _find_data_dir("Для верификации изополей")
VERIFY_2_DIR = _find_data_dir("Для верификации изополей 2")


@pytest.fixture
def data_dir() -> Path:
    return DATA_DIR


@pytest.fixture
def direction_mosaic() -> Mosaic:
    """Минимальная мозаика с легендой для тестов прикладного и HTTP-слоя."""

    background = Rebar(step=300, diameter=18)
    levels = [
        Band(0, 181, "s300d18", 8.5, background, None),
        Band(1, 2, "s300d18+s100d25", 58.0, background, Rebar(100, 25)),
    ]
    return Mosaic(
        direction=Direction(Layer.BOTTOM, Axis.X),
        cells=[
            Cell([(0, 0), (500, 0), (500, 500), (0, 500)], (250, 250), 181, levels[0]),
            Cell(
                [(500, 0), (1000, 0), (1000, 500), (500, 500)],
                (750, 250),
                2,
                levels[1],
            ),
        ],
        legend=levels,
        bbox=(0, 0, 1000, 500),
        source_path="Нижнее армирование вдоль ОСИ Х.dxf",
        meta={
            "scale_intervals": [
                {"index": 0, "aci": 181, "lower_as": 7.2, "upper_as": 8.5},
                {"index": 1, "aci": 2, "lower_as": 40.0, "upper_as": 58.0},
            ]
        },
    )


@pytest.fixture
def dxf_bottom_x() -> Path:
    """DXF: Нижнее армирование вдоль ОСИ Х."""
    p = DATA_DIR / "Нижнее армирование вдоль ОСИ Х.dxf"
    if not p.exists():
        pytest.skip(f"нет файла данных: {p}")
    return p


@pytest.fixture
def shk_top_x() -> Path:
    """Файл шкалы .shk (эталон в spec)."""
    p = MOSAIC_DIR / "К09_фп_Вх.shk"
    if not p.exists():
        pytest.skip(f"нет файла данных: {p}")
    return p


@pytest.fixture
def shk_full() -> Path:
    p = MOSAIC_DIR / "2025-08-15_ППТ8-1-Д2-К09_М1.shk"
    if not p.exists():
        pytest.skip(f"нет файла данных: {p}")
    return p


@pytest.fixture
def c1_top_y_dxf() -> Path:
    """Проблемный C1: верхняя арматура по оси Y из инженерного комплекта."""

    p = VERIFY_DIR / "1-КЖ00.С1-2" / "С1_t_800_Верхняя по оси У.dxf"
    if not p.exists():
        pytest.skip(f"нет C1 DXF: {p}")
    return p


@pytest.fixture
def dxf_verify2_foundation_bottom_x() -> Path:
    matches = [
        path
        for path in VERIFY_2_DIR.rglob("Нижняя по Х.dxf")
        if "2024.12.17_фундаментная плита" in str(path)
    ]
    if not matches:
        pytest.skip(f"нет нового проверочного DXF в {VERIFY_2_DIR}")
    return matches[0]


@pytest.fixture
def verify2_dxf_files() -> list[Path]:
    paths = sorted(VERIFY_2_DIR.rglob("*.dxf"))
    if not paths:
        pytest.skip(f"нет новых проверочных DXF в {VERIFY_2_DIR}")
    return paths


@pytest.fixture
def verify_dxf_files() -> list[Path]:
    paths = sorted(VERIFY_DIR.rglob("*.dxf"))
    if not paths:
        pytest.skip(f"нет старых проверочных DXF в {VERIFY_DIR}")
    return paths


@pytest.fixture
def plate_zero_dxf_files() -> list[Path]:
    """Четыре входных DXF golden-case плиты нуля."""

    expected = _normalised("2025.02.19_плита нуля")
    task_dirs = [
        path
        for path in VERIFY_2_DIR.rglob("*")
        if path.is_dir() and _normalised(path.name) == expected
    ]
    if len(task_dirs) != 1:
        pytest.skip(
            f"ожидался один каталог задания плиты нуля в {VERIFY_2_DIR}, "
            f"найдено: {len(task_dirs)}"
        )
    paths = sorted(task_dirs[0].rglob("*.dxf"))
    if not paths:
        pytest.skip(f"нет входных DXF плиты нуля в {task_dirs[0]}")
    return paths
