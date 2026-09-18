"""PNG scales must be complete for occupied DXF colours and direction-specific."""

from pathlib import Path

import pytest

from rebar.application.analyze_direction import load_direction_mosaic


def _foundation() -> Path:
    root = Path(__file__).resolve().parents[1] / "Для верификации изополей 2"
    matches = list(root.rglob("2024.12.17_фундаментная плита/Изополя/Нижняя по У.dxf"))
    if not matches:
        pytest.skip("локальные материалы фундаментной плиты отсутствуют")
    return matches[0].parent


@pytest.mark.parametrize(
    ("dxf_name", "axis", "layer", "expected_count"),
    [
        ("Нижняя по Х.dxf", "X", "ниж", 9),
        ("Нижняя по У.dxf", "Y", "ниж", 6),
        ("Верхняя по Х.dxf", "X", "верх", 6),
        ("Верхняя по У.dxf", "Y", "верх", 5),
    ],
)
def test_foundation_png_scales(dxf_name, axis, layer, expected_count):
    pytest.importorskip("rapidocr_onnxruntime")
    folder = _foundation()
    png = next(path for path in folder.glob("*.png") if f"оси_{axis}_у_{layer}" in path.name)
    mosaic = load_direction_mosaic(folder / dxf_name, png_path=png)

    assert len(mosaic.legend) == expected_count
    assert all(cell.band is not None for cell in mosaic.cells)
    assert mosaic.meta["legend_source"] == "png"
    assert mosaic.legend[0].label == "s300d18"


def test_png_without_used_high_bands_is_rejected(tmp_path):
    pytest.importorskip("rapidocr_onnxruntime")
    folder = _foundation()
    top_x = next(path for path in folder.glob("*.png") if "оси_X_у_верх" in path.name)
    renamed = tmp_path / "scale.png"
    renamed.write_bytes(top_x.read_bytes())
    with pytest.raises(ValueError, match="не содержит используемые цвета"):
        load_direction_mosaic(folder / "Нижняя по Х.dxf", png_path=renamed)


def test_png_with_wrong_direction_name_is_rejected():
    folder = _foundation()
    top_x = next(path for path in folder.glob("*.png") if "оси_X_у_верх" in path.name)
    with pytest.raises(ValueError, match="направление PNG"):
        load_direction_mosaic(folder / "Нижняя по Х.dxf", png_path=top_x)


def test_png_cannot_be_combined_with_shk_or_mapping():
    with pytest.raises(ValueError, match="одновременно"):
        load_direction_mosaic("Нижняя по Х.dxf", png_path="scale.png", shk_path="scale.shk")
