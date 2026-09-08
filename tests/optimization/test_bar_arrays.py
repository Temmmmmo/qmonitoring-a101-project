"""Проверки фазовой геометрии параметрических групп стержней."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rebar import Axis, Band, Cell, Direction, Layer, Mosaic, Rebar
from rebar.optimization import (
    LayoutConstraints,
    bar_coordinates,
    build_layout_problem,
    build_zone,
    evaluate_layout,
)
from rebar.optimization.services import (
    bboxes_overlap,
    build_zone_from_bbox,
    resolve_zone_phases,
    transverse_axis_gap,
    zones_conflict,
)


def _overlapping_cells_mosaic(axis: Axis) -> Mosaic:
    background = Rebar(step=300, diameter=18)
    level = Band(
        1,
        2,
        "s300d18+s100d20",
        40.0,
        background,
        Rebar(step=100, diameter=20),
    )
    polygon = [(0, 0), (1000, 0), (1000, 300), (0, 300)]
    if axis is Axis.Y:
        polygon = [(y, x) for x, y in polygon]
    centroid = (
        sum(point[0] for point in polygon) / 4,
        sum(point[1] for point in polygon) / 4,
    )
    return Mosaic(
        direction=Direction(Layer.BOTTOM, axis),
        cells=[
            Cell(list(polygon), centroid, 2, level),
            Cell(list(polygon), centroid, 2, level),
        ],
        legend=[
            Band(0, 181, "s300d18", 8.5, background, None),
            level,
        ],
        bbox=(0, 0, 1000, 300) if axis is Axis.X else (0, 0, 300, 1000),
    )


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_overlapping_bboxes_are_allowed_when_bar_phases_do_not_conflict(axis):
    problem = build_layout_problem(
        _overlapping_cells_mosaic(axis),
        LayoutConstraints(min_width_cells=1),
    )
    first = build_zone(problem, (0,), 1, "first")
    second = build_zone(
        problem,
        (1,),
        1,
        "second",
        first_bar_coordinate_mm=50.0,
    )

    evaluation = evaluate_layout(problem, (first, second))

    assert bboxes_overlap(first.bbox, second.bbox)
    assert set(bar_coordinates(first)).isdisjoint(bar_coordinates(second))
    assert zones_conflict(problem, first, second) is False
    assert evaluation.valid is True
    assert evaluation.metrics.physical_bar_count == 8


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_coincident_bar_axes_are_postprocessing_warning(axis):
    problem = build_layout_problem(
        _overlapping_cells_mosaic(axis),
        LayoutConstraints(min_width_cells=1),
    )
    first = build_zone(problem, (0,), 1, "first")
    second = build_zone(problem, (1,), 1, "second")

    evaluation = evaluate_layout(problem, (first, second))

    assert zones_conflict(problem, first, second) is False
    assert evaluation.valid is True
    assert any("стержни зон first и second конфликтуют" in item for item in evaluation.diagnostics)


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_strict_research_profile_can_reject_overlapping_bboxes(axis):
    problem = build_layout_problem(
        _overlapping_cells_mosaic(axis),
        LayoutConstraints(min_width_cells=1, allow_overlaps=False),
    )
    first = build_zone(problem, (0,), 1, "first")
    second = build_zone(problem, (1,), 1, "second")

    evaluation = evaluate_layout(problem, (first, second))

    assert zones_conflict(problem, first, second) is True
    assert evaluation.valid is False
    assert any(
        "прямоугольники разбиения first и second пересекаются" in item
        for item in evaluation.diagnostics
    )


def test_phase_resolver_separates_touching_same_step_groups():
    mosaic = _overlapping_cells_mosaic(Axis.X)
    second_polygon = [(0, 300), (1000, 300), (1000, 600), (0, 600)]
    mosaic.cells[1] = replace(
        mosaic.cells[1],
        poly=second_polygon,
        centroid=(500, 450),
    )
    mosaic.bbox = (0, 0, 1000, 600)
    problem = build_layout_problem(mosaic, LayoutConstraints(min_width_cells=1))
    raw = [
        build_zone(problem, (0,), 1, "lower", collect_coverage=False),
        build_zone(problem, (1,), 1, "upper", collect_coverage=False),
    ]

    lower, upper = resolve_zone_phases(problem, raw)

    assert transverse_axis_gap(Axis.X, lower, upper) == pytest.approx(100.0)
    assert zones_conflict(problem, lower, upper) is False
    assert evaluate_layout(problem, (lower, upper)).valid is True


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
@pytest.mark.parametrize("collect_coverage", [False, True])
@pytest.mark.parametrize("cut_lengths", [(), (1600.0, 2200.0)])
@pytest.mark.parametrize("seed_cell_ids", [(0,), ()])
def test_phase_resolver_preserves_spatial_partition(
    axis, collect_coverage, cut_lengths, seed_cell_ids,
):
    background = Rebar(step=300, diameter=12)
    base = Band(0, 181, "s300d12", 3.77, background, None)
    extra = Band(1, 2, "s300d12+s100d12", 15.08, background, Rebar(100, 12))
    cells = [
        Cell([(0, 0), (1000, 0), (1000, 1000), (0, 1000)], (500, 500), 2, extra),
        Cell(
            [(1000, 0), (2000, 0), (2000, 1000), (1000, 1000)],
            (1500, 500), 181, base,
        ),
    ]
    boxes = [(0, 0, 500, 1000), (500, 0, 1000, 1000)]
    mosaic_bbox = (0, 0, 2000, 1000)
    if axis is Axis.Y:
        cells = [
            replace(cell, poly=[(y, x) for x, y in cell.poly], centroid=cell.centroid[::-1])
            for cell in cells
        ]
        boxes = [(ymin, xmin, ymax, xmax) for xmin, ymin, xmax, ymax in boxes]
        mosaic_bbox = (0, 0, 1000, 2000)
    problem = build_layout_problem(
        Mosaic(Direction(Layer.BOTTOM, axis), cells, [base, extra], mosaic_bbox),
        LayoutConstraints(
            min_width_cells=1,
            allow_overlaps=False,
            allowed_cut_lengths_mm=cut_lengths,
        ),
    )
    # Two rectangles cover parts of the same FE. Seed IDs describe provenance,
    # not the rectangle boundaries chosen by the optimizer.
    raw = [
        build_zone_from_bbox(
            problem, box, 1, f"part-{index}",
            seed_cell_ids=seed_cell_ids,
        )
        for index, box in enumerate(boxes)
    ][::-1]  # The phase solver must also preserve the caller's ordering.
    before = evaluate_layout(problem, raw)

    resolved = resolve_zone_phases(problem, raw, collect_coverage=collect_coverage)

    for original, zone in zip(raw, resolved):
        assert zone.id == original.id
        assert zone.demand_bbox == original.demand_bbox
        assert zone.level_index == original.level_index
        assert zone.rebar == original.rebar
        assert zone.required_length_mm == original.required_length_mm
        assert zone.anchored_length_mm == original.anchored_length_mm
        assert zone.installed_length_mm == original.installed_length_mm
        assert zone.width_mm == original.width_mm
        assert zone.bar_count == original.bar_count
        assert zone.mass_kg == original.mass_kg
        assert zone.meta == original.meta
        assert zone.covered_cell_ids == ((0,) if collect_coverage else ())
    assert any(
        zone.first_bar_coordinate_mm != original.first_bar_coordinate_mm
        for original, zone in zip(raw, resolved)
    )
    # Fast-path zones intentionally omit cached coverage. Collect it before
    # independent validation, as the optimizers do for their final candidates.
    final = resolve_zone_phases(problem, resolved)
    after = evaluate_layout(problem, final)
    assert before.valid and after.valid
    assert after.metrics.under_reinforced_cell_count == 0
    assert after.metrics.total_mass_kg == pytest.approx(before.metrics.total_mass_kg)
    assert after.metrics.physical_bar_count == before.metrics.physical_bar_count
    assert resolve_zone_phases(
        problem, resolved, collect_coverage=collect_coverage,
    ) == resolved


@pytest.mark.parametrize("axis", [Axis.X, Axis.Y])
def test_anchorage_overlap_is_only_a_detailing_warning(axis):
    mosaic = _overlapping_cells_mosaic(axis)
    if axis is Axis.X:
        second_polygon = [(1000, 0), (2000, 0), (2000, 300), (1000, 300)]
        mosaic.cells[1] = replace(
            mosaic.cells[1], poly=second_polygon, centroid=(1500, 150)
        )
        mosaic.bbox = (0, 0, 2000, 300)
    else:
        second_polygon = [(0, 1000), (300, 1000), (300, 2000), (0, 2000)]
        mosaic.cells[1] = replace(
            mosaic.cells[1], poly=second_polygon, centroid=(150, 1500)
        )
        mosaic.bbox = (0, 0, 300, 2000)

    problem = build_layout_problem(mosaic, LayoutConstraints(min_width_cells=1))
    first = build_zone(problem, (0,), 1, "first")
    second = build_zone(problem, (1,), 1, "second")

    evaluation = evaluate_layout(problem, (first, second))

    assert not bboxes_overlap(first.demand_bbox, second.demand_bbox)
    assert bboxes_overlap(first.bbox, second.bbox)
    assert zones_conflict(problem, first, second) is False
    assert evaluation.valid is True
    assert any(
        message.startswith("WARNING: стержни зон first и second конфликтуют")
        for message in evaluation.diagnostics
    )


def test_validator_rejects_tampered_first_bar_coordinate(mosaic_with_legend):
    problem = build_layout_problem(
        mosaic_with_legend,
        LayoutConstraints(min_width_cells=1),
    )
    zone = build_zone(problem, (1,), 1, "phase")

    evaluation = evaluate_layout(
        problem,
        (replace(zone, first_bar_coordinate_mm=zone.first_bar_coordinate_mm + 10),),
    )

    assert evaluation.valid is False
    assert any("первая ось не совпадает" in item for item in evaluation.diagnostics)
