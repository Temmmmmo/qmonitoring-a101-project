"""Стабильная маркировка зон и человекочитаемые выноски."""

from dataclasses import replace

from rebar import Axis, Direction, Layer
from rebar.optimization import AlgorithmRequest, LayoutConstraints, build_layout_problem
from rebar.optimization.registry import built_in_optimizer_registry
from rebar.reporting.svg import render_solution_svg
from rebar.reporting.zone_schedule import build_zone_schedule


def test_zone_schedule_contains_required_callout_fields(direction_mosaic):
    problem = build_layout_problem(
        direction_mosaic,
        LayoutConstraints(min_width_cells=1, enforce_zone_gap=False),
    )
    solution = built_in_optimizer_registry().create("bbox").solve(
        problem,
        AlgorithmRequest(max_details=1),
    )

    (row,) = build_zone_schedule(direction_mosaic.direction, solution)

    assert row.mark == "Н-X-001"
    assert row.zone_id == solution.zones[0].id
    assert row.callout == (
        f"⌀{row.diameter_mm}-{row.installed_length_mm:g}, "
        f"шаг {row.step_mm} ({row.bar_count} шт.)"
    )
    svg = render_solution_svg(problem, solution)
    assert row.mark in svg
    assert row.callout in svg


def test_zone_marks_encode_all_four_directions(direction_mosaic):
    expected = {
        Direction(Layer.BOTTOM, Axis.X): "Н-X-001",
        Direction(Layer.BOTTOM, Axis.Y): "Н-Y-001",
        Direction(Layer.TOP, Axis.X): "В-X-001",
        Direction(Layer.TOP, Axis.Y): "В-Y-001",
    }
    for direction, mark in expected.items():
        mosaic = replace(direction_mosaic, direction=direction)
        problem = build_layout_problem(
            mosaic,
            LayoutConstraints(min_width_cells=1, enforce_zone_gap=False),
        )
        solution = built_in_optimizer_registry().create("bbox").solve(
            problem,
            AlgorithmRequest(max_details=1),
        )

        assert build_zone_schedule(direction, solution)[0].mark == mark
