"""Тесты адаптивного выбора предметных операторов GA."""

from __future__ import annotations

import random

import pytest

from rebar.optimization.algorithms.genetic.policy import build_operator_policy


def test_ucb1_explores_every_operator_then_prefers_positive_reward():
    operators = ("split", "merge", "shift")
    policy = build_operator_policy("ucb1", operators, exploration=0.0)
    rng = random.Random(17)

    first_round = []
    for _index in range(len(operators)):
        selected = policy.choose(operators, rng)
        first_round.append(selected)
        policy.update(selected, 1.0 if selected == "merge" else -1.0)

    assert set(first_round) == set(operators)
    assert policy.choose(operators, rng) == "merge"
    snapshot = policy.snapshot()
    assert snapshot["name"] == "ucb1"
    assert snapshot["total_selections"] == 3
    assert snapshot["exploration"] == 0.0


def test_operator_policy_rejects_unknown_mode_and_non_finite_reward():
    with pytest.raises(ValueError, match="operator_policy"):
        build_operator_policy("neural-magic", ("split",))

    policy = build_operator_policy("uniform", ("split",))
    with pytest.raises(ValueError, match="конечным"):
        policy.update("split", float("nan"))
