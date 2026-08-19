"""Компактные синтетические задачи для тестов оптимизации."""

from __future__ import annotations

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar


@pytest.fixture
def mosaic_with_legend() -> Mosaic:
    background = Rebar(step=300, diameter=18)
    levels = [
        Band(0, 181, "s300d18", 8.5, background, None),
        Band(1, 2, "s300d18+s100d25", 58.0, background, Rebar(100, 25)),
    ]
    return Mosaic(
        direction=Direction(Layer.BOTTOM, Axis.X),
        cells=[
            Cell([(0, 0), (500, 0), (500, 500), (0, 500)], (250, 250), 181, levels[0]),
            Cell([(500, 0), (1000, 0), (1000, 500), (500, 500)], (750, 250), 2, levels[1]),
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
def splittable_mosaic() -> Mosaic:
    background = Rebar(step=300, diameter=18)
    levels = [
        Band(0, 181, "s300d18", 8.5, background, None),
        Band(1, 254, "s300d18+s300d18", 15.0, background, Rebar(300, 18)),
        Band(2, 2, "s300d18+s100d25", 58.0, background, Rebar(100, 25)),
    ]
    cells: list[Cell] = []
    for column in range(8):
        xmin = column * 500
        cells.append(
            Cell(
                [(xmin, 0), (xmin + 500, 0), (xmin + 500, 600), (xmin, 600)],
                (xmin + 250, 300),
                254,
                levels[1],
            )
        )
    for column in range(2):
        xmin = column * 500
        cells.append(
            Cell(
                [(xmin, 600), (xmin + 500, 600), (xmin + 500, 1200), (xmin, 1200)],
                (xmin + 250, 900),
                2,
                levels[2],
            )
        )
    return Mosaic(
        direction=Direction(Layer.BOTTOM, Axis.X),
        cells=cells,
        legend=levels,
        bbox=(0, 0, 4000, 1200),
        meta={},
    )
