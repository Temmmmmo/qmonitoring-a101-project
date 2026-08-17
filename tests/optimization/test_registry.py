"""Тесты явного выбора взаимозаменяемых алгоритмов."""

import pytest

from rebar.optimization import (
    AlgorithmRequest,
    LayoutMetrics,
    LayoutSolution,
    OptimizerRegistry,
    SolutionStatus,
    build_layout_problem,
)


class _DummyOptimizer:
    name = "dummy"

    def solve(self, problem, request=None):
        request = request or AlgorithmRequest()
        return LayoutSolution(
            algorithm=self.name,
            status=SolutionStatus.FEASIBLE,
            zones=(),
            metrics=LayoutMetrics(0, 0, 0, 0, 0, 0, 0, 0),
            request=request,
        )


def test_registry_switches_implementations_by_name(mosaic_with_legend):
    registry = OptimizerRegistry()
    registry.register("dummy", _DummyOptimizer)

    solution = registry.create("DUMMY").solve(build_layout_problem(mosaic_with_legend))

    assert registry.names() == ("dummy",)
    assert solution.algorithm == "dummy"
    assert solution.status is SolutionStatus.FEASIBLE


def test_registry_rejects_duplicates_and_unknown_names():
    registry = OptimizerRegistry()
    registry.register("dummy", _DummyOptimizer)

    with pytest.raises(ValueError, match="уже зарегистрирован"):
        registry.register("DUMMY", _DummyOptimizer)
    with pytest.raises(KeyError, match="доступны: dummy"):
        registry.create("bsp")
