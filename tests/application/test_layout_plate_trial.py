"""Ordinary full candidates reach the existing trial without changing their bars."""
from __future__ import annotations

import copy
from dataclasses import replace
import importlib
from pathlib import Path

import pytest

from rebar import Band, Cell, Mosaic, Rebar
from rebar.application.layout_plate_trial import (
    build_layout_plate_trial, build_layout_plate_trial_bundle,
)
from rebar.optimization import (
    PLATE_DIRECTIONS, LayoutConstraints, PlateDirectionSolution, StrongestBBoxOptimizer,
    build_layout_problem, build_plate_problem, build_plate_solution,
)
from rebar.optimization.services.bar_geometry import bar_segments
from rebar.optimization.services.evaluation import evaluate_layout

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "a" * 64


def make_case(step=150):
    background, extra = Rebar(300, 18), Rebar(step, 18)
    base = Band(0, 181, "s300d18", 8.5, background, None)
    level = Band(1, 2, f"s300d18+s{step}d18", 34.0, background, extra)
    cells = []
    for x in (1000, 1500):
        for y in (1000, 1500):
            cells.append(Cell([(x, y), (x+500, y), (x+500, y+500), (x, y+500)],
                              (x+250, y+250), 2, level))
    problems = tuple(build_layout_problem(Mosaic(direction, cells, [base, level],
        (1000, 1000, 2000, 2000), source_path=f"Плита {direction}.dxf"),
        LayoutConstraints(min_width_cells=1)) for direction in PLATE_DIRECTIONS)
    problem = build_plate_problem(problems, case_id="Рабочая плита")
    solution = build_plate_solution(PlateDirectionSolution(p.demand.direction,
        StrongestBBoxOptimizer().solve(p)) for p in problems)
    return problem, solution


@pytest.fixture
def packet_validator(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "integrations/pyrevit/QMonitoring.extension/lib"))
    return importlib.import_module("qm_plate_packet").validate_packet


def build(problem, solution, **kwargs):
    return build_layout_plate_trial_bundle(problem, solution,
        source_sha256=DIGEST, steel_class="A500", **kwargs)


def update_direction(solution, index, modify):
    items = list(solution.direction_solutions)
    items[index] = replace(items[index], solution=modify(items[index].solution))
    return build_plate_solution(items)


def test_existing_full_packet_preserves_all_axes_mass_and_counts(packet_validator):
    problem, solution = make_case()
    before = copy.deepcopy((problem, solution))
    result = build(problem, solution)
    packet, review = result["packet"], result["review"]
    assert packet_validator(packet) == packet
    assert [d["direction"] for d in packet["directions"]] == list(map(str, PLATE_DIRECTIONS))
    assert packet["expected"]["physical_bar_count"] == solution.metrics.physical_bar_count
    assert packet["expected"]["additional_mass_kg"] == pytest.approx(solution.metrics.total_mass_kg)
    assert packet["expected"]["zone_count"] == 4
    for direction, item in zip(PLATE_DIRECTIONS, packet["directions"]):
        zone, = solution.solution(direction).zones
        run, = item["runs"]
        segments = bar_segments(direction.axis, zone)
        longitudinal = 0 if direction.axis.value == "X" else 1
        assert run["start_xy_mm"][longitudinal] == segments[0].longitudinal_start_mm
        assert run["end_xy_mm"][longitudinal] == segments[0].longitudinal_end_mm
        actual = [run["start_xy_mm"][1-longitudinal] + i*run["spacing_mm"]
                  for i in range(run["bar_count"])]
        assert actual == [segment.coordinate_mm for segment in segments]
        assert run["spacing_mm"] == 150  # Never changed to the confirmed 100/200 pattern.
    assert "legacy-uniform-150-not-a101-100-200" in packet["source_blockers"]
    assert review["original_demand_coverage_checked"] is True
    assert review["original_under_reinforced_cell_count"] == 0
    assert review["placement_eligible"] is packet["placement_eligible"] is False
    assert (problem, solution) == before
    assert build_layout_plate_trial(problem, solution, source_sha256=DIGEST,
                                   steel_class="A500") == packet


def test_uniform_100_has_explicit_sto_contact_issue(packet_validator):
    problem, solution = make_case(100)
    result = build(problem, solution)
    packet_validator(result["packet"])
    assert "legacy-uniform-100-not-sto-2.7.9-contact" in result["packet"]["source_blockers"]
    assert all(d["runs"][0]["spacing_mm"] == 100 for d in result["packet"]["directions"])


def test_uniform_300_not_mislabeled_as_150_or_100():
    problem, solution = make_case(300)
    packet = build(problem, solution)["packet"]
    assert not any("legacy-uniform-1" in issue for issue in packet["source_blockers"])
    assert "background-phase-compatibility" in packet["source_blockers"]


@pytest.mark.parametrize("change", ["length", "first_axis", "last_axis", "mass", "quantity", "nan", "diameter"])
def test_corrupt_zone_rejected_not_repaired(change):
    problem, solution = make_case()

    def modify(item):
        zone = item.zones[0]
        if change == "length":
            zone = replace(zone, installed_length_mm=zone.installed_length_mm + 1)
        elif change == "first_axis":
            zone = replace(zone, first_bar_coordinate_mm=zone.first_bar_coordinate_mm + 1)
        elif change == "last_axis":
            zone = replace(zone, bbox=(*zone.bbox[:3], zone.bbox[3] + 1))
        elif change == "mass":
            zone = replace(zone, mass_kg=zone.mass_kg + 1)
        elif change == "quantity":
            zone = replace(zone, bar_count=True)
        elif change == "nan":
            zone = replace(zone, bbox=(float("nan"), *zone.bbox[1:]))
        else:
            zone = replace(zone, rebar=Rebar(150, 50))
        return replace(item, zones=(zone,))

    with pytest.raises(ValueError):
        build(problem, update_direction(solution, 0, modify))


def test_long_bar_rejects_entire_packet():
    problem, solution = make_case()
    bad = update_direction(solution, 3, lambda item: replace(item,
        zones=(replace(item.zones[0], bbox=(0, 0, 1000, 11701)),)))
    with pytest.raises(ValueError, match="100..11700"):
        build(problem, bad)


@pytest.mark.parametrize("field", ["total_mass_kg", "physical_bar_count", "zone_count", "total_bar_length_mm"])
def test_stale_plate_metrics_rejected(field):
    problem, solution = make_case()
    bad = replace(solution, metrics=replace(solution.metrics,
        **{field: getattr(solution.metrics, field) + 1}))
    with pytest.raises(ValueError, match="plate"):
        build(problem, bad)


def lowered_case():
    original, solution = make_case()
    first = original.direction_problems[0]
    changes = {"policy": "legacy-research", "changed_count": 4,
        "changes": [{"cell_id": c.id, "from_level_index": 1, "to_level_index": 0}
                    for c in first.demand.cells]}
    lowered = replace(first, demand=replace(first.demand,
        cells=tuple(replace(c, level_index=0) for c in first.demand.cells),
        meta={**first.demand.meta, "single_cell_preprocessing": changes}),
        meta={"single_cell_preprocessing": changes})
    problem = replace(original, direction_problems=(lowered, *original.direction_problems[1:]))
    solution = update_direction(solution, 0, lambda item: replace(item, zones=(),
        metrics=evaluate_layout(lowered, ()).metrics))
    return original, problem, solution


def test_lowered_candidate_kept_whole_and_original_undercoverage_visible(packet_validator):
    original, problem, solution = lowered_case()
    result = build(problem, solution, original_problem=original)
    packet_validator(result["packet"])
    assert len(result["packet"]["directions"]) == 4
    assert result["packet"]["directions"][0]["runs"] == []
    assert result["packet"]["expected"]["physical_bar_count"] == solution.metrics.physical_bar_count
    assert result["review"]["original_under_reinforced_cell_count"] == 4
    assert "original-demand-undercoverage" in result["packet"]["source_blockers"]
    assert "source-demand-lowered-in-analysis" in result["packet"]["source_blockers"]
    assert result["review"]["directions"][0]["preprocessing"]["problem"]["changed_count"] == 4
    result["review"]["directions"][0]["preprocessing"]["problem"]["changes"].clear()
    assert len(problem.direction_problems[0].meta["single_cell_preprocessing"]["changes"]) == 4


def test_lowering_without_original_is_not_zero_undercoverage():
    _, problem, solution = lowered_case()
    result = build(problem, solution)
    assert result["review"]["original_demand_coverage_checked"] is False
    assert result["review"]["original_under_reinforced_cell_count"] is None
    assert "original-demand-coverage-not-checked" in result["packet"]["source_blockers"]


def test_independent_validation_detects_gap_despite_stale_zero_coverage_count():
    problem, solution = make_case()
    # Make an honest zero-bar direction but retain misleading old coverage counters.
    first = solution.direction_solutions[0].solution
    metrics = replace(first.metrics, total_mass_kg=0, total_bar_length_mm=0,
                      physical_bar_count=0, detail_count=0)
    bad = update_direction(solution, 0, lambda item: replace(item, zones=(), metrics=metrics))
    result = build(problem, bad)
    assert result["review"]["original_under_reinforced_cell_count"] == 4
    assert "original-demand-undercoverage" in result["packet"]["source_blockers"]


def test_original_must_be_same_geometry_not_a_smaller_demand_map():
    original, problem, solution = lowered_case()
    first = original.direction_problems[0]
    smaller = replace(original, direction_problems=(replace(first,
        demand=replace(first.demand, cells=first.demand.cells[:-1])), *original.direction_problems[1:]))
    with pytest.raises(ValueError, match="same complete cells"):
        build(problem, solution, original_problem=smaller)
    with pytest.raises(ValueError, match="recorded demand lowering"):
        build(problem, solution, original_problem=problem)


def test_known_source_class_cannot_be_reassigned():
    problem, solution = make_case()
    solution = update_direction(solution, 0, lambda item: replace(item, meta={"steel_class": "A400"}))
    with pytest.raises(ValueError, match="steel_class differs"):
        build(problem, solution)


@pytest.mark.parametrize("digest", ["", "a" * 63, "g" * 64, "A" * 64, None])
def test_source_digest_required(digest):
    problem, solution = make_case()
    with pytest.raises(ValueError, match="SHA256"):
        build_layout_plate_trial(problem, solution, source_sha256=digest, steel_class="A500")


def test_existing_host_preflight_is_not_part_of_export_permission(packet_validator):
    problem, solution = make_case()
    packet = build(problem, solution)["packet"]
    packet_validator(packet)
    assert packet["mode"] == "commit-readback-rollback"
    assert packet["placement_eligible"] is False
    assert "host-boundary-cover-openings" in packet["source_blockers"]
    assert "background-and-additions-3d-collisions" in packet["source_blockers"]
