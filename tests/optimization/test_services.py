"""Тесты общей детализации и независимого валидатора."""

from rebar.optimization import (
    LayoutConstraints,
    SolutionStatus,
    StrongestBBoxOptimizer,
    build_layout_problem,
)


def test_validator_enforces_overcoverage_constraint(mosaic_with_legend):
    problem = build_layout_problem(
        mosaic_with_legend,
        LayoutConstraints(min_width_cells=1, allow_overcoverage=False),
    )

    solution = StrongestBBoxOptimizer().solve(problem)

    assert solution.status is SolutionStatus.ERROR
    assert solution.metrics.overcovered_cell_count == 1
    assert any("избыточно накрыты" in message for message in solution.diagnostics)
