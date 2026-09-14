"""Позиции не подменяются зонами, физическими стержнями или рядами Revit."""
from dataclasses import replace
from itertools import product

import pytest

from rebar.optimization import (
    PLATE_DIRECTIONS, AlgorithmRequest, BspOptimizer, ComplexityAxis,
    LayoutConstraints, PlateDirectionSolution, StrongestBBoxOptimizer,
    build_direction_pareto_front, build_layout_problem, build_plate_pareto_front,
    build_plate_problem, build_plate_solution, measure_constructability,
    measure_plate_constructability,
)
from rebar.optimization.services.bar_schedule import (
    BarScheduleGroup, build_bar_schedule, layout_schedule_groups, straight_bar_key,
)
from rebar.optimization.services.position_combinations import combine_position_candidates


def test_schedule_groups_across_directions_without_changing_quantities_or_mass():
    groups = (BarScheduleGroup('top-X:z1', 18, 3900, 9, 'A500'),
              BarScheduleGroup('bottom-Y:z2', 18, 3900, 3, 'A500'),
              BarScheduleGroup('top-Y:z3', 18, 3900, 2, 'A500C'),
              BarScheduleGroup('top-X:z4', 25, 3900, 4, 'A500'),
              BarScheduleGroup('top-X:z5', 18, 4000, 5, 'A500'))
    schedule = build_bar_schedule(groups)
    assert len(schedule) == 4
    assert sum(p.physical_bar_count for p in schedule) == 23
    assert schedule == build_bar_schedule(reversed(groups))
    shared = next(p for p in schedule if len(p.source_ids) == 2)
    assert shared.physical_bar_count == 12
    # Общая принятая в ядре удельная масса 0.006165 * d² кг/м не меняется.
    assert shared.total_mass_kg == pytest.approx(.006165 * 18**2 * 3.9 * 12)
    assert straight_bar_key(18, 3900 + 1e-9) == straight_bar_key(18, 3900)
    assert straight_bar_key(18, 3900.01) != straight_bar_key(18, 3900)


@pytest.mark.parametrize('group', [
    BarScheduleGroup('', 18, 3900, 9), BarScheduleGroup('a', 0, 3900, 9),
    BarScheduleGroup('a', True, 3900, 9), BarScheduleGroup('a', 18, float('nan'), 9),
    BarScheduleGroup('a', 18, 3900, 0), BarScheduleGroup('a', 18, 3900, True),
])
def test_invalid_schedule_group_is_rejected(group):
    with pytest.raises(ValueError):
        build_bar_schedule([group])


def test_duplicate_source_id_and_unknown_class():
    group = BarScheduleGroup('a', 18, 3900, 9)
    with pytest.raises(ValueError, match='уникальным'):
        build_bar_schedule([group, group])
    assert build_bar_schedule([group])[0].steel_class == ''
    assert build_bar_schedule([]) == ()


def test_position_union_keeps_locally_dominated_alternative(splittable_mosaic):
    problem = build_layout_problem(splittable_mosaic, LayoutConstraints(min_width_cells=1))
    solution = StrongestBBoxOptimizer().solve(problem, AlgorithmRequest(max_details=1))
    base = build_direction_pareto_front(problem, [solution]).candidates[0]
    first_key, shared_key = straight_bar_key(18, 3900), straight_bar_key(18, 4000)

    def candidate(name, key, mass):
        return replace(base, id=name, solution=replace(base.solution,
            metrics=replace(base.solution.metrics, total_mass_kg=mass)),
            constructability=replace(base.constructability, position_count=1, bar_position_keys=(key,)))

    first = candidate('cheap-alone', first_key, 1)
    second = candidate('shared-but-heavier', shared_key, 2)
    rest = tuple((candidate(str(i), shared_key, 1),) for i in range(3))
    result = combine_position_candidates(((first, second), *rest))
    assert {c[0].id for c in result} == {'cheap-alone', 'shared-but-heavier'}
    assert len(set().union(*(set(c.constructability.bar_position_keys) for c in result[0]))) == 1
    with pytest.raises(ValueError, match='лимит'):
        combine_position_candidates(((first, second), *rest), maximum_states=1)


def test_plate_position_front_matches_full_cartesian_enumeration(splittable_mosaic):
    pairs = []
    for direction in PLATE_DIRECTIONS:
        problem = build_layout_problem(replace(splittable_mosaic, direction=direction),
            LayoutConstraints(min_width_cells=1, enforce_zone_gap=False))
        request = AlgorithmRequest(max_details=2)
        pairs.append((problem, tuple(optimizer().solve(problem, request)
            for optimizer in (StrongestBBoxOptimizer, BspOptimizer))))
    problem = build_plate_problem(p for p, _ in pairs)
    front = build_plate_pareto_front(problem, {p.demand.direction: solutions for p, solutions in pairs},
                                    complexity_axis=ComplexityAxis.POSITION_COUNT)
    points = set()
    for combination in product(*(solutions for _, solutions in pairs)):
        solution = build_plate_solution(PlateDirectionSolution(direction, layout)
            for direction, layout in zip(PLATE_DIRECTIONS, combination))
        schedule = build_bar_schedule(group for item in solution.direction_solutions
            for group in layout_schedule_groups(item.solution.zones, prefix=f'{item.direction}:'))
        metrics = measure_plate_constructability(solution)
        assert metrics.position_count == len(schedule)
        assert not metrics.position_class_declared
        assert metrics.position_count <= sum(measure_constructability(s).position_count for s in combination)
        points.add((len(schedule), round(solution.metrics.total_mass_kg, 6)))
    expected = {p for p in points if not any(q != p and q[0] <= p[0] and q[1] <= p[1] for q in points)}
    assert {(c.constructability.position_count, round(c.solution.metrics.total_mass_kg, 6))
            for c in front.candidates} == expected
    assert all(len(f.combination_candidates) == 2 for f in front.direction_fronts)
