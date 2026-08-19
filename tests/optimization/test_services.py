"""Тесты общей детализации и независимого валидатора."""

from dataclasses import replace

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar
from rebar.optimization import (
    LayoutConstraints,
    SolutionStatus,
    StrongestBBoxOptimizer,
    build_layout_problem,
    build_zone,
    evaluate_layout,
)


def _transpose(mosaic: Mosaic) -> Mosaic:
    return Mosaic(
        direction=Direction(mosaic.direction.layer, Axis.Y),
        cells=[
            Cell(
                poly=[(y, x) for x, y in cell.poly],
                centroid=(cell.centroid[1], cell.centroid[0]),
                aci=cell.aci,
                band=cell.band,
            )
            for cell in mosaic.cells
        ],
        legend=mosaic.legend,
        bbox=(mosaic.bbox[1], mosaic.bbox[0], mosaic.bbox[3], mosaic.bbox[2]),
        source_path=mosaic.source_path,
        meta=mosaic.meta,
    )


def _gap_mosaic(
    axis: Axis,
    gap_mm: float,
    *,
    mixed_steps: bool = False,
) -> Mosaic:
    background = Rebar(step=300, diameter=18)
    weak = Band(0, 181, "s300d18", 8.5, background, None)
    first = Band(1, 100, "s300d18+s100d20", 15.0, background, Rebar(100, 20))
    second = (
        Band(2, 200, "s300d18+s300d18", 25.0, background, Rebar(300, 18))
        if mixed_steps
        else first
    )

    def polygon(transverse_start: float) -> list[tuple[float, float]]:
        if axis is Axis.X:
            return [
                (0, transverse_start),
                (1000, transverse_start),
                (1000, transverse_start + 300),
                (0, transverse_start + 300),
            ]
        return [
            (transverse_start, 0),
            (transverse_start + 300, 0),
            (transverse_start + 300, 1000),
            (transverse_start, 1000),
        ]

    second_start = 300 + gap_mm
    polygons = [polygon(0), polygon(second_start)]
    cells = [
        Cell(
            poly=poly,
            centroid=(
                sum(point[0] for point in poly) / 4,
                sum(point[1] for point in poly) / 4,
            ),
            aci=band.aci,
            band=band,
        )
        for poly, band in zip(polygons, (first, second))
    ]
    maximum = second_start + 300
    bbox = (0, 0, 1000, maximum) if axis is Axis.X else (0, 0, maximum, 1000)
    levels = [weak, first] if not mixed_steps else [weak, first, second]
    return Mosaic(
        direction=Direction(Layer.BOTTOM, axis),
        cells=cells,
        legend=levels,
        bbox=bbox,
    )


def _separate_zones(mosaic: Mosaic):
    problem = build_layout_problem(mosaic, LayoutConstraints(min_width_cells=1))
    zones = tuple(
        build_zone(
            problem,
            (cell_id,),
            cell.band.index,
            f"zone-{cell_id + 1}",
        )
        for cell_id, cell in enumerate(mosaic.cells)
        if cell.band is not None
    )
    return problem, zones


def test_validator_enforces_overcoverage_constraint(mosaic_with_legend):
    problem = build_layout_problem(
        mosaic_with_legend,
        LayoutConstraints(min_width_cells=1, allow_overcoverage=False),
    )

    solution = StrongestBBoxOptimizer().solve(problem)

    assert solution.status is SolutionStatus.ERROR
    assert solution.metrics.overcovered_cell_count == 1
    assert any("избыточно накрыты" in message for message in solution.diagnostics)


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_builder_quantizes_width_and_bar_count_for_both_axes(splittable_mosaic, axis):
    narrow = Mosaic(
        direction=splittable_mosaic.direction,
        cells=[
            Cell(
                poly=[(x, y * 5 / 6) for x, y in cell.poly],
                centroid=(cell.centroid[0], cell.centroid[1] * 5 / 6),
                aci=cell.aci,
                band=cell.band,
            )
            for cell in splittable_mosaic.cells[:8]
        ],
        legend=splittable_mosaic.legend,
        bbox=(0, 0, 4000, 500),
    )
    mosaic = narrow if axis is Axis.X else _transpose(narrow)
    problem = build_layout_problem(
        mosaic,
        LayoutConstraints(min_width_cells=1, enforce_zone_gap=False),
    )

    zone = build_zone(problem, range(8), 1, "quantized")

    assert zone.width_mm == 600
    assert zone.bar_count == 3
    assert zone.required_length_mm == 4000
    assert zone.installed_length_mm == 5440
    assert zone.meta["width_space_count"] == 2
    if axis is Axis.X:
        assert zone.bbox == (-720, -50, 4720, 550)
    else:
        assert zone.bbox == (-50, -720, 550, 4720)


def test_validator_recalculates_mass_instead_of_trusting_zone(mosaic_with_legend):
    problem = build_layout_problem(
        mosaic_with_legend,
        LayoutConstraints(min_width_cells=1),
    )
    zone = build_zone(problem, (1,), 1, "mass")

    evaluation = evaluate_layout(problem, (replace(zone, mass_kg=1.0),))

    assert evaluation.valid is False
    assert evaluation.metrics.total_mass_kg == pytest.approx(zone.mass_kg)
    assert any("mass_kg не совпадает" in message for message in evaluation.diagnostics)


def test_validator_checks_bar_count_and_anchorage(mosaic_with_legend):
    problem = build_layout_problem(
        mosaic_with_legend,
        LayoutConstraints(min_width_cells=1),
    )
    zone = build_zone(problem, (1,), 1, "fields")
    broken = replace(
        zone,
        bar_count=zone.bar_count + 1,
        required_length_mm=zone.required_length_mm + 10,
    )

    evaluation = evaluate_layout(problem, (broken,))

    assert evaluation.valid is False
    assert any("bar_count должен быть" in message for message in evaluation.diagnostics)
    assert any("не согласована с анкеровкой" in message for message in evaluation.diagnostics)


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_validator_requires_full_cell_geometry_not_only_centroid(axis):
    problem, zones = _separate_zones(_gap_mosaic(axis, 100))
    zone = zones[0]
    xmin, ymin, xmax, ymax = zone.bbox
    shifted_bbox = (
        (xmin, ymin + 100, xmax, ymax + 100)
        if axis is Axis.X
        else (xmin + 100, ymin, xmax + 100, ymax)
    )

    evaluation = evaluate_layout(problem, (replace(zone, bbox=shifted_bbox),))

    assert evaluation.valid is False
    assert evaluation.metrics.under_reinforced_cell_count == 2
    assert any("covered_cell_ids не совпадает" in message for message in evaluation.diagnostics)


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_validator_enforces_same_step_zone_gap_for_both_axes(axis):
    problem, too_close = _separate_zones(_gap_mosaic(axis, 50))
    _valid_problem, separated = _separate_zones(_gap_mosaic(axis, 100))

    invalid_evaluation = evaluate_layout(problem, too_close)
    valid_evaluation = evaluate_layout(_valid_problem, separated)

    assert invalid_evaluation.valid is False
    assert any("зазор между зонами" in message for message in invalid_evaluation.diagnostics)
    assert valid_evaluation.valid is True
    assert valid_evaluation.metrics.under_reinforced_cell_count == 0


def test_validator_marks_unconfirmed_mixed_step_gap_as_warning():
    problem, zones = _separate_zones(_gap_mosaic(Axis.X, 150, mixed_steps=True))

    evaluation = evaluate_layout(problem, zones)

    assert evaluation.valid is True
    assert any(
        message.startswith("WARNING: правило зазора для разных шагов")
        for message in evaluation.diagnostics
    )
