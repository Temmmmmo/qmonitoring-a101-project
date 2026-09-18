"""PNG scales must be complete for occupied DXF colours and direction-specific."""

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from rebar.application.analyze_direction import _validate_png_recipe_bounds, load_direction_mosaic
from rebar.legend import parse_recipe
from rebar.models import Axis, Cell, Direction, Layer, Mosaic
from rebar.png_legend import _legend_bars, _legend_ocr, apply_png_legend


def _synthetic_gray_legend(*, framed=True, interior=True):
    image = np.full((80, 370, 3), 192, dtype=np.uint8)
    image[16, 29:337] = 0  # horizontal top border, not a gray canvas edge
    image[17:36, 30:130] = (159, 127, 255)  # DXF ACI 181
    image[17:36, 236:336] = (191, 0, 255)  # DXF ACI 200
    if interior:
        image[17:36, 133:233] = (192, 192, 192)  # DXF ACI 9
        if framed:
            image[17:36, (130, 132, 233, 235)] = 0
    return image


def test_framed_interior_gray_band_is_distinct_from_gray_canvas():
    y, bars = _legend_bars(_synthetic_gray_legend(), [181, 9, 200])
    assert y == 17
    assert [(start, end, rgb) for start, end, rgb in bars] == [
        (30, 130, (159, 127, 255)), (133, 233, (192, 192, 192)),
        (236, 336, (191, 0, 255)),
    ]


@pytest.mark.parametrize("framed,interior", ((False, True), (True, False)))
def test_missing_or_unframed_gray_gap_cannot_be_invented(framed, interior):
    _, bars = _legend_bars(_synthetic_gray_legend(framed=framed, interior=interior), [181, 9, 200])
    assert [rgb for _, _, rgb in bars] == [(159, 127, 255), (191, 0, 255)]


def test_gray_segment_only_outside_legend_is_not_a_band():
    image = _synthetic_gray_legend(interior=False)
    image[17:36, 0:26] = (192, 192, 192)  # outside left chromatic anchor
    _, bars = _legend_bars(image, [181, 9, 200])
    assert len(bars) == 2


def test_interior_gray_must_match_the_expected_dxf_aci():
    _, bars = _legend_bars(_synthetic_gray_legend(), [181, 8, 200])
    assert len(bars) == 2


@pytest.mark.parametrize("framed,interior", ((False, True), (True, False)))
def test_missing_or_misplaced_gray_is_rejected_for_occupied_dxf(tmp_path, framed, interior):
    png = tmp_path / 'legend.png'
    Image.fromarray(_synthetic_gray_legend(framed=framed, interior=interior)).save(png)
    cells = [Cell([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)],
                  (i + 0.5, 0.5), aci)
             for i, aci in enumerate((181, 9, 200))]
    mosaic = Mosaic(Direction(Layer.BOTTOM, Axis.X), cells, [], (0, 0, 3, 1),
                    meta={'scale_aci_order': [181, 9, 200],
                          'scale_bounds_as': [0, 6.7, 13.4, 20.1]})
    with pytest.raises(ValueError, match='не содержит используемые цвета'):
        apply_png_legend(mosaic, png)


@pytest.mark.parametrize("gray_label", ("s300d16+s300d16", "invalid"))
def test_gray_band_receives_own_ocr_recipe_or_is_rejected(tmp_path, monkeypatch, gray_label):
    png = tmp_path / 'legend.png'
    Image.fromarray(_synthetic_gray_legend()).save(png)
    labels = iter(("s300d16", gray_label, "s300d16+s150d16")
                  if gray_label != 'invalid' else ("s300d16",))

    def ocr(_crop):
        label = next(labels, gray_label)
        return [(label, 0.99)], 0.001

    monkeypatch.setattr('rebar.png_legend._legend_ocr', lambda: ocr)
    cells = [Cell([(i, 0), (i + 1, 0), (i + 1, 1), (i, 1)],
                  (i + 0.5, 0.5), aci)
             for i, aci in enumerate((181, 9, 200))]
    mosaic = Mosaic(Direction(Layer.BOTTOM, Axis.X), cells, [], (0, 0, 3, 1),
                    meta={'scale_aci_order': [181, 9, 200],
                          'scale_bounds_as': [0, 6.7, 13.4, 20.1]})
    if gray_label == 'invalid':
        with pytest.raises(ValueError, match='подпись полосы PNG 2'):
            apply_png_legend(mosaic, png)
    else:
        assigned = _validate_png_recipe_bounds(apply_png_legend(mosaic, png))
        assert [cell.band.label for cell in assigned.cells] == [
            's300d16', 's300d16+s300d16', 's300d16+s150d16']
        assert [band.aci for band in assigned.legend] == [181, 9, 200]


def _foundation() -> Path:
    root = Path(__file__).resolve().parents[1] / "Для верификации изополей 2"
    matches = list(root.rglob("2024.12.17_фундаментная плита/Изополя/Нижняя по У.dxf"))
    if not matches:
        pytest.skip("локальные материалы фундаментной плиты отсутствуют")
    return matches[0].parent


def test_png_ocr_reuses_one_recognition_engine_without_detection(monkeypatch):
    rapidocr = pytest.importorskip("rapidocr_onnxruntime")
    ocr = _legend_ocr()
    assert ocr is _legend_ocr()
    options = {}

    def record(**kwargs):
        options.update(kwargs)
        return object()

    monkeypatch.setattr(rapidocr, "RapidOCR", record)
    _legend_ocr.__wrapped__()
    assert options["use_det"] is False and options["use_cls"] is False
    assert options["use_rec"] is True


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


@pytest.mark.parametrize(
    ("dxf_name", "axis", "layer", "expected_count"),
    [
        ("Нижняя по Х.dxf", "X", "ниж", 3),
        ("Нижняя по У.dxf", "Y", "ниж", 4),
        ("Верхняя по Х.dxf", "X", "верх", 6),
        ("Верхняя по У.dxf", "Y", "верх", 6),
    ],
)
def test_minus_two_plate_png_scales(dxf_name, axis, layer, expected_count):
    pytest.importorskip("rapidocr_onnxruntime")
    root = Path(__file__).resolve().parents[1] / "Для верификации изополей 2"
    folders = list(root.rglob("2025.02.04_плита над минус 2 этажом/Изополя"))
    if not folders:
        pytest.skip("локальные материалы плиты над −2 отсутствуют")
    folder = folders[0]
    png = next(path for path in folder.glob("*.png") if f"оси_{axis}_у_{layer}" in path.name)
    mosaic = load_direction_mosaic(folder / dxf_name, png_path=png)
    assert len(mosaic.legend) == expected_count
    assert all(cell.band is not None for cell in mosaic.cells)


def test_png_recipe_that_disagrees_with_dxf_area_is_rejected():
    pytest.importorskip("rapidocr_onnxruntime")
    root = Path(__file__).resolve().parents[1] / "Для верификации изополей 2"
    folders = list(root.rglob("2025.02.04_плита над минус 2 этажом/Изополя"))
    if not folders:
        pytest.skip("локальные материалы плиты над −2 отсутствуют")
    folder = folders[0]
    png = next(path for path in folder.glob("*.png") if "оси_X_у_ниж" in path.name)
    mosaic = load_direction_mosaic(folder / "Нижняя по Х.dxf", png_path=png)
    mosaic.legend[1].recipe = parse_recipe("s300d18+s300d18")
    with pytest.raises(ValueError, match="интервал DXF"):
        _validate_png_recipe_bounds(mosaic)


def test_third_floor_long_png_labels_are_read():
    pytest.importorskip("rapidocr_onnxruntime")
    root = Path(__file__).resolve().parents[1] / "Для верификации изополей 2"
    folders = list(root.rglob("Плита над 3 этажом/Изополя"))
    if not folders:
        pytest.skip("локальные материалы плиты над 3 этажом отсутствуют")
    folder = folders[0]
    png = next(path for path in folder.glob("*.png") if "оси_X_у_верх" in path.name)
    mosaic = load_direction_mosaic(folder / "Верхняя по Х.dxf", png_path=png)
    assert mosaic.legend[1].label == "s300d10+s300d10"
    assert len(mosaic.legend) == 7


def test_ninth_floor_unlabelled_last_band_is_rejected():
    pytest.importorskip("rapidocr_onnxruntime")
    root = Path(__file__).resolve().parents[1] / "Для верификации изополей 2"
    folders = list(root.rglob("Плита над 9 этажом/Изополя"))
    if not folders:
        pytest.skip("локальные материалы плиты над 9 этажом отсутствуют")
    folder = folders[0]
    png = next(path for path in folder.glob("*.png") if "оси_X_у_верх" in path.name)
    with pytest.raises(ValueError, match="подпись полосы PNG 8"):
        load_direction_mosaic(folder / "Верхняя по Х.dxf", png_path=png)


def _above_first_floor() -> Path:
    root = Path(__file__).resolve().parents[1] / "Для верификации изополей 2"
    folders = list(root.rglob("2025.05.05_плита над 1 этажом/Допка плит"))
    if not folders:
        pytest.skip("локальный комплект плиты над 1 этажом отсутствует")
    return folders[0]


@pytest.mark.parametrize(
    ("dxf_name", "png_pattern", "expected_count"),
    [
        ("Х Низ.dxf", "X_у_ниж*.png", 3),
        ("У Низ.dxf", "Y_у_ниж*.png", 3),
        ("У Верх.dxf", "Y_у_верх*.png", 6),
    ],
)
def test_above_first_floor_small_png_labels_are_read(dxf_name, png_pattern, expected_count):
    pytest.importorskip("rapidocr_onnxruntime")
    folder = _above_first_floor()
    png = next(folder.glob(png_pattern))
    mosaic = load_direction_mosaic(folder / dxf_name, png_path=png)
    assert len(mosaic.legend) == expected_count
    assert mosaic.legend[0].label == "s300d10"
    assert all(cell.band is not None for cell in mosaic.cells)


def test_above_first_floor_occupied_unlabelled_eighth_band_is_rejected():
    pytest.importorskip("rapidocr_onnxruntime")
    folder = _above_first_floor()
    png = next(folder.glob("X_у_верх*.png"))
    with pytest.raises(ValueError, match="подпись полосы PNG 8"):
        load_direction_mosaic(folder / "Х Верх.dxf", png_path=png)
