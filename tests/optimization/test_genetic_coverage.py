"""Уровень фрагмента определяется площадью пересечения, а не центроидом КЭ."""

from dataclasses import replace

from rebar.application.genetic_oracle import small_oracle_problems
from rebar.optimization import AlgorithmRequest
from rebar.optimization.algorithms.genetic.coverage import demand_fragments
from rebar.optimization.algorithms.spatial_partition_greedy import _build_grid


def test_fragment_levels_and_sources_follow_triangles_not_bounding_boxes():
    template = small_oracle_problems()[0]
    cells = (
        replace(template.demand.cells[0], id=0, poly=((0, 0), (1000, 0), (0, 1000)), centroid=(1000 / 3, 1000 / 3)),
        replace(template.demand.cells[1], id=1, poly=((1000, 0), (1000, 1000), (0, 1000)), centroid=(2000 / 3, 2000 / 3)),
    )
    problem = replace(template, demand=replace(template.demand, cells=cells, bbox=(0, 0, 1000, 1000)))
    grid = _build_grid(problem, AlgorithmRequest())
    atoms = demand_fragments(problem, grid, ((0, 0, 500, 500),))
    by_bbox = {atom.bbox: atom for atom in atoms}

    assert len(atoms) == 4
    assert by_bbox[(0, 0, 500, 500)].level_index == 1
    assert by_bbox[(0, 0, 500, 500)].source_cell_ids == (0,)
    assert by_bbox[(500, 500, 1000, 1000)].source_cell_ids == (1,)
    assert by_bbox[(0, 500, 500, 1000)].source_cell_ids == (0, 1)
    assert by_bbox[(0, 500, 500, 1000)].level_index == 2


def test_empty_part_of_triangle_is_not_a_demand_atom():
    template = small_oracle_problems()[0]
    cell = replace(template.demand.cells[0], poly=((0, 0), (1000, 0), (0, 1000)), centroid=(1000 / 3, 1000 / 3))
    problem = replace(template, demand=replace(template.demand, cells=(cell,), bbox=(0, 0, 1000, 1000)))
    grid = _build_grid(problem, AlgorithmRequest())

    atoms = demand_fragments(problem, grid, ((0, 0, 500, 500),))

    assert len(atoms) == 3
    assert (500, 500, 1000, 1000) not in {atom.bbox for atom in atoms}
