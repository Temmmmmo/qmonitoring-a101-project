"""Общие фикстуры тестов. Пути к реальным данным (кириллица, пробелы) — только здесь."""

from pathlib import Path
import unicodedata

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "Дополнительные материалы"
MOSAIC_DIR = DATA_DIR / "Изополя(мозаики) армирования"


def _normalised(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _find_data_dir(name: str) -> Path:
    expected = _normalised(name)
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
