"""Тесты стадии A на реальных DXF/.shk организаторов."""

from pathlib import Path

import ezdxf
import pytest
from PIL import Image

from rebar import Axis, Direction, Layer, Rebar
from rebar.dxf_ingest import (
    detect_units_scale,
    direction_from_filename,
    extract_scale_aci_order,
    read_mosaic,
)
from rebar.legend import build_legend, parse_label, parse_shk, parse_spec
from rebar.render import render_mosaic

# --- уже реализовано: разбор подписи -----------------------------------------

def test_parse_spec():
    assert parse_spec("s300d18") == Rebar(step=300, diameter=18)


def test_parse_label_background_only():
    bg, add = parse_label("s300d18")
    assert bg == Rebar(300, 18)
    assert add is None


def test_parse_label_with_additional():
    bg, add = parse_label("s300d18+s150d20")
    assert bg == Rebar(300, 18)
    assert add == Rebar(150, 20)


# --- TODO Codex: .shk --------------------------------------------------------

EXPECTED_SHK = [
    (8.48, "s300d18"),
    (16.97, "s300d18+s300d18"),
    (25.45, "s300d18+s150d18"),
    (29.43, "s300d18+s150d20"),
    (39.90, "s300d18+s100d20"),
    (57.57, "s300d18+s100d25"),
]


def test_parse_shk(shk_top_x):
    bands = parse_shk(str(shk_top_x))
    assert len(bands) == len(EXPECTED_SHK)
    for (thr, label), (exp_thr, exp_label) in zip(bands, EXPECTED_SHK):
        assert label == exp_label
        assert thr == pytest.approx(exp_thr, abs=0.05)


def test_parse_shk_full_scale(shk_full):
    bands = parse_shk(str(shk_full))
    assert len(bands) == 9
    assert bands[0] == pytest.approx((8.483, "s300d18"), abs=0.001)
    assert bands[-1][0] == pytest.approx(110.30, abs=0.05)
    assert bands[-1][1] == "s300d18+s100d36"


def test_build_legend(shk_top_x):
    aci = [181, 254, 6, 41, 23, 2]
    legend = build_legend(str(shk_top_x), aci)
    assert [band.index for band in legend] == list(range(6))
    assert [band.aci for band in legend] == aci
    assert legend[0].additional is None
    assert legend[3].additional == Rebar(step=150, diameter=20)


def test_build_legend_rejects_mismatched_scale(shk_top_x):
    with pytest.raises(ValueError, match="число полос не совпадает"):
        build_legend(str(shk_top_x), [181, 254])


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Нижнее армирование вдоль ОСИ Х.dxf", Direction(Layer.BOTTOM, Axis.X)),
        ("Верхнее армирование вдоль ОСИ У.dxf", Direction(Layer.TOP, Axis.Y)),
        ("Нижняя ар-ра_по_оси_X.dxf", Direction(Layer.BOTTOM, Axis.X)),
        ("Y_верх.dxf", Direction(Layer.TOP, Axis.Y)),
        ("Х Низ.dxf", Direction(Layer.BOTTOM, Axis.X)),
        ("Низ вдоль буквенных осей.dxf", Direction(Layer.BOTTOM, Axis.X)),
        ("ВЕРХ вдоль цифровых осей.dxf", Direction(Layer.TOP, Axis.Y)),
    ],
)
def test_direction_from_filename(filename, expected):
    assert direction_from_filename(filename) == expected


def test_direction_from_filename_rejects_unknown_axis():
    with pytest.raises(ValueError, match="ось X/Y"):
        direction_from_filename("Нижнее без направления.dxf")


# --- TODO Codex: DXF ingest --------------------------------------------------

def test_read_mosaic_bottom_x(dxf_bottom_x):
    m = read_mosaic(str(dxf_bottom_x))
    # В DXF 2132 результата KLEENKA + 2132 точных служебных копии PLAST.
    assert len(m.cells) == 2132
    assert m.meta["raw_3dface_count"] == 4264
    assert m.meta["ignored_plast_count"] == 2132
    assert m.meta["triangle_count"] == 39
    assert m.meta["quad_count"] == 2093
    xmin, ymin, xmax, ymax = m.bbox
    assert (xmax - xmin) == pytest.approx(23600, rel=0.05)  # мм
    assert (ymax - ymin) == pytest.approx(14000, rel=0.05)
    assert m.direction == Direction(Layer.BOTTOM, Axis.X)
    assert all(isinstance(c.aci, int) for c in m.cells)
    assert {len(cell.poly) for cell in m.cells} == {3, 4}
    assert len(m.legend) == 9
    assert any(c.needs_extra for c in m.cells)
    assert sum(not c.needs_extra for c in m.cells) > len(m.cells) // 2
    assert m.meta["unit_scale_to_mm"] == 1000.0
    assert m.meta["unit_detection"] == "heuristic_edge_length"


def test_extract_scale_aci_order(dxf_bottom_x):
    doc = ezdxf.readfile(dxf_bottom_x)
    assert extract_scale_aci_order(doc) == [181, 254, 6, 41, 23, 2, 190, 211, 1]
    assert detect_units_scale(doc, doc.modelspace()) == 1000.0


def test_read_new_dxf_without_shk(dxf_verify2_foundation_bottom_x):
    m = read_mosaic(str(dxf_verify2_foundation_bottom_x))
    assert len(m.cells) == 2132
    assert m.legend == []
    assert all(cell.band is None for cell in m.cells)
    assert m.meta["scale_aci_order"] == [181, 51, 200, 31, 13, 2, 190, 211, 1]
    assert m.meta["scale_bounds_as"] == [7.2, 8.5, 17, 25, 29, 40, 58, 70, 89, 169]
    assert len(m.meta["scale_intervals"]) == 9


def test_all_new_dxf_files_are_parseable(verify2_dxf_files):
    mosaics = [read_mosaic(str(path)) for path in verify2_dxf_files]
    assert len(mosaics) == 24
    assert all(1800 < len(mosaic.cells) < 2200 for mosaic in mosaics)
    assert all(mosaic.meta["raw_3dface_count"] == 2 * len(mosaic.cells) for mosaic in mosaics)
    assert all(mosaic.meta["ignored_plast_count"] == len(mosaic.cells) for mosaic in mosaics)
    assert all(mosaic.meta["scale_intervals"] for mosaic in mosaics)
    assert {mosaic.direction for mosaic in mosaics} == {
        Direction(Layer.BOTTOM, Axis.X),
        Direction(Layer.BOTTOM, Axis.Y),
        Direction(Layer.TOP, Axis.X),
        Direction(Layer.TOP, Axis.Y),
    }


def test_all_previous_verification_dxf_files_are_parseable(verify_dxf_files):
    mosaics = [read_mosaic(str(path)) for path in verify_dxf_files]
    assert len(mosaics) >= 30
    assert all(mosaic.cells for mosaic in mosaics)
    assert all(
        mosaic.meta["raw_3dface_count"]
        == len(mosaic.cells) + mosaic.meta["ignored_plast_count"]
        for mosaic in mosaics
    )
    assert all(mosaic.meta["units"] == "mm" for mosaic in mosaics)


def _scale_modelspace_faces(doc, factor: float) -> None:
    for face in doc.modelspace().query("3DFACE"):
        for index in range(4):
            vertex = getattr(face.dxf, f"vtx{index}")
            setattr(
                face.dxf,
                f"vtx{index}",
                (vertex.x * factor, vertex.y * factor, vertex.z * factor),
            )


@pytest.mark.parametrize("header_units", [0, 4])
def test_meter_and_millimeter_inputs_produce_same_geometry(
    dxf_bottom_x: Path, tmp_path: Path, header_units: int
):
    source = read_mosaic(str(dxf_bottom_x))
    doc = ezdxf.readfile(dxf_bottom_x)
    _scale_modelspace_faces(doc, 1000.0)
    doc.header["$INSUNITS"] = header_units
    converted = tmp_path / "Нижнее армирование вдоль ОСИ Х_mm.dxf"
    doc.saveas(converted)

    result = read_mosaic(str(converted))
    assert result.bbox == pytest.approx(source.bbox, abs=0.01)
    assert len(result.cells) == len(source.cells)
    assert result.cells[0].poly == pytest.approx(source.cells[0].poly, abs=0.01)
    assert result.meta["unit_scale_to_mm"] == 1.0
    expected_detection = "heuristic_edge_length" if header_units == 0 else "dxf_header"
    assert result.meta["unit_detection"] == expected_detection


def test_render_mosaic_creates_real_png(dxf_bottom_x, tmp_path):
    mosaic = read_mosaic(str(dxf_bottom_x))
    destination = tmp_path / "mosaic.png"
    returned = render_mosaic(mosaic, str(destination))

    assert returned == str(destination)
    assert destination.stat().st_size > 1000
    with Image.open(destination) as image:
        assert image.format == "PNG"
        assert image.width > 1000
        assert image.height > 600
        assert len(image.getcolors(maxcolors=256)) > 3
